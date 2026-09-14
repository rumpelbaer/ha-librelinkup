from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from aiohttp import ClientResponseError

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    LibreLinkUpAccountStateError,
    LibreLinkUpApi,
    LibreLinkUpAuthenticationError,
    LibreLinkUpRegionError,
)
from .utils import parse_libre_timestamp

_LOGGER = logging.getLogger(__name__)

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)

# How long a previously valid snapshot may keep being served while updates fail.
# Data Stale already flags a reading after 5 minutes, so three stale windows give
# a short outage room to recover before the update itself is reported as failed.
STALE_FAILURE_GRACE_PERIOD = timedelta(minutes=15)

# How old a patient's own measurement may be before their entities go
# unavailable. Derived from FactoryTimestamp, not from the time of the last
# successful request: LibreLinkUp keeps answering with the last known reading
# when a sensor stops delivering, and an hours-old value must never keep being
# served as a current one. Matched to the failure grace period so an outage and a
# silent sensor expire a patient after the same time.
MAX_MEASUREMENT_AGE = timedelta(minutes=15)

RATE_LIMIT_DEFAULT_DELAY = timedelta(minutes=5)
MIN_BACKOFF = timedelta(seconds=60)
MAX_RATE_LIMIT_BACKOFF = timedelta(minutes=30)
MAX_SERVER_ERROR_BACKOFF = timedelta(minutes=15)


def _error_label(err: Exception) -> str:
    """Describe an error for logging without leaking anything.

    Never the exception message: a ClientResponseError renders its URL, which
    contains a patient ID, and its repr() contains the bearer token.
    """
    if isinstance(err, ClientResponseError):
        return f"HTTP {err.status}"

    return type(err).__name__


def _parse_retry_after(value: str | None) -> timedelta | None:
    """Parse a Retry-After header in either delay-seconds or HTTP-date form."""
    if not value:
        return None

    try:
        return timedelta(seconds=float(value))
    except (TypeError, ValueError):
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=dt_util.UTC)

    return retry_at - dt_util.utcnow()


class LibreLinkUpAccountCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Polls /llu/connections once per LibreLinkUp account.

    One instance serves every config entry of the same account, so three shared
    patients cost one login and one request per interval instead of three. Its
    data maps patient ID to that patient's latest known measurement -- but only
    for patients that actually have a config entry: a LibreLinkUp account may
    share more people than Home Assistant is configured for, and the readings of
    everybody else must not be stored, logged or exposed at all.
    """

    def __init__(self, hass: HomeAssistant, api: LibreLinkUpApi) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name="LibreLinkUp",
            update_interval=DEFAULT_UPDATE_INTERVAL,
            # Deliberately not bound to a config entry: the coordinator outlives
            # any single entry, and binding it would make Home Assistant shut it
            # down as soon as the first of several entries is unloaded. The
            # things Home Assistant then skips -- reauth, the polling preference
            # and the shutdown hook -- are handled explicitly instead.
            config_entry=None,
        )

        self.api = api

        # Set by the runtime so an account-wide auth failure can raise reauth
        # for exactly one entry instead of one per patient.
        self.async_request_reauth: Callable[[], None] | None = None

        # Set by the runtime to turn per-patient and account-wide problems into
        # repair issues, without the coordinator knowing about registries.
        self.async_report_missing_patients: Callable[[frozenset[str]], None] | None = (
            None
        )
        self.async_report_account_state: Callable[[bool], None] | None = None

        # Stays None until a fetch actually succeeded, so a restart can never
        # look like "last success just now" and hand out a grace period that
        # was never earned.
        self._last_successful_update: datetime | None = None
        self._failure_label: str | None = None
        self._server_error_count = 0

        # The patients Home Assistant has a config entry for. Everything else
        # the account shares is dropped on arrival.
        self._patients: frozenset[str] = frozenset()

        # Per patient, so one person without a reading never drags the rest of
        # the account down with them. Holds the measurement time, not the poll
        # time (see MAX_MEASUREMENT_AGE).
        self._measured_at: dict[str, datetime] = {}
        self._patient_problem_logged: set[str] = set()
        self._patient_missing_logged: set[str] = set()

    # -- entry facing ------------------------------------------------------

    @callback
    def async_set_configured_patients(self, patient_ids: Iterable[str]) -> None:
        """Limit the account to the patients that have a config entry.

        Called whenever an entry is added or removed. Anything the coordinator
        still holds for a patient that is no longer configured is dropped right
        away, so an unloaded entry leaves no glucose value behind in memory.
        """
        self._patients = frozenset(patient_ids)

        if self.data:
            self.data = {
                patient_id: measurement
                for patient_id, measurement in self.data.items()
                if patient_id in self._patients
            }

        self._measured_at = {
            patient_id: measured_at
            for patient_id, measured_at in self._measured_at.items()
            if patient_id in self._patients
        }
        self._patient_problem_logged &= self._patients
        self._patient_missing_logged &= self._patients

    @callback
    def async_set_polling_enabled(self, enabled: bool) -> None:
        """Honour the "Enable polling for updates" system option per account.

        Home Assistant applies that option inside its own scheduling, which only
        runs for coordinators bound to a config entry. Several entries share this
        coordinator, so the account keeps polling as long as at least one of its
        entries allows it, and stops automatic polling once every entry opted
        out. Manual refreshes keep working either way.
        """
        if not enabled:
            if self.update_interval is None:
                return

            self.update_interval = None

            if self.data is not None and self.last_update_success:
                # Cancels the pending refresh; with no interval left to schedule
                # it is not armed again.
                self.async_set_updated_data(self.data)

            return

        if self.update_interval is not None:
            # Already polling -- and a running backoff interval stays intact.
            return

        self.update_interval = DEFAULT_UPDATE_INTERVAL

        if self.data is not None and self.last_update_success:
            # Re-arms the timer with the restored interval.
            self.async_set_updated_data(self.data)

    def measurement_for(self, patient_id: str) -> dict | None:
        """The latest known measurement of exactly this patient."""
        return (self.data or {}).get(patient_id)

    def measured_at_for(self, patient_id: str) -> datetime | None:
        """When exactly this patient's latest known measurement was taken.

        Returns what was derived from FactoryTimestamp when the reading
        arrived; it never parses or recomputes anything. This is the same
        value the freshness rules below run on, so the entity layer cannot
        drift apart from availability by deriving its own.
        """
        return self._measured_at.get(patient_id)

    def is_patient_available(self, patient_id: str) -> bool:
        """Whether this patient's own measurement is recent enough to serve."""
        measured_at = self.measured_at_for(patient_id)

        if measured_at is None:
            return False

        return dt_util.utcnow() - measured_at < MAX_MEASUREMENT_AGE

    @property
    def configured_patient_count(self) -> int:
        """How many patients this account polls for. For diagnostics."""
        return len(self._patients)

    @property
    def failure_label(self) -> str | None:
        """The kind of the ongoing failure, or None. For diagnostics."""
        return self._failure_label

    @property
    def seconds_since_last_success(self) -> float | None:
        """Age of the last successful update in seconds. For diagnostics."""
        if self._last_successful_update is None:
            return None

        return (dt_util.utcnow() - self._last_successful_update).total_seconds()

    # -- polling -----------------------------------------------------------

    async def _async_update_data(self) -> dict[str, dict]:
        try:
            measurements = await self.api.async_get_measurements()

        except LibreLinkUpAuthenticationError as err:
            if self.async_request_reauth is not None:
                self.async_request_reauth()

            raise ConfigEntryAuthFailed(
                "LibreLinkUp authentication failed"
            ) from err

        except LibreLinkUpRegionError as err:
            # Permanent: no amount of retrying makes an unknown region known.
            self._report_account_state(True)

            raise ConfigEntryError(
                "LibreLinkUp region is not supported"
            ) from err

        except LibreLinkUpAccountStateError as err:
            # The credentials are not the problem, so no reauth dialog -- the
            # runtime raises a repair issue instead.
            self._report_account_state(True)

            return self._handle_failure(err)

        except Exception as err:
            return self._handle_failure(err)

        return self._handle_success(measurements)

    def _handle_success(self, measurements: dict[str, dict | None]) -> dict[str, dict]:
        now = dt_util.utcnow()

        self._last_successful_update = now
        self._server_error_count = 0
        self._report_account_state(False)

        if self.update_interval not in (None, DEFAULT_UPDATE_INTERVAL):
            # Back to normal after a backoff. "is None" is left alone: that is
            # polling being switched off by the user, not a backoff.
            self.update_interval = DEFAULT_UPDATE_INTERVAL

        if self._failure_label is not None:
            _LOGGER.info("LibreLinkUp updates recovered")
            self._failure_label = None

        # Start from what we already knew, restricted to configured patients: a
        # patient who is momentarily without a reading keeps their previous one
        # until its own age limit is reached.
        snapshot = {
            patient_id: measurement
            for patient_id, measurement in (self.data or {}).items()
            if patient_id in self._patients
        }

        missing: set[str] = set()

        for patient_id in self._patients:
            if patient_id not in measurements:
                # The account no longer shares this person at all.
                missing.add(patient_id)
                self._log_patient_missing(patient_id)
                continue

            self._patient_missing_logged.discard(patient_id)
            measurement = measurements[patient_id]

            if measurement is None:
                self._log_patient_problem(patient_id)
                continue

            measured_at = parse_libre_timestamp(measurement.get("FactoryTimestamp"))

            if measured_at is None:  # pragma: no cover
                # The API layer rejects unparsable timestamps already.
                self._log_patient_problem(patient_id)
                continue

            snapshot[patient_id] = measurement
            self._measured_at[patient_id] = measured_at
            self._patient_problem_logged.discard(patient_id)

        if self.async_report_missing_patients is not None:
            self.async_report_missing_patients(frozenset(missing))

        return snapshot

    def _handle_failure(self, err: Exception) -> dict[str, dict]:
        label = _error_label(err)
        backoff = self._apply_backoff(err)
        self._log_failure(label)

        if self.data and self._within_grace_period():
            return self.data

        # Static message, and "from None" so the original exception cannot
        # reach a traceback with the token or a patient ID in it.
        raise UpdateFailed(
            "LibreLinkUp data update failed",
            retry_after=backoff.total_seconds() if backoff else None,
        ) from None

    def _within_grace_period(self) -> bool:
        if self._last_successful_update is None:
            return False

        return dt_util.utcnow() - self._last_successful_update < (
            STALE_FAILURE_GRACE_PERIOD
        )

    def _apply_backoff(self, err: Exception) -> timedelta | None:
        """Slow polling down for server-side failures. Returns the new delay.

        Applied only to rate limits and server errors: a malformed payload is
        not something the server needs relief from. Because the coordinator is
        shared, one 429 slows the whole account down exactly once.
        """
        if not isinstance(err, ClientResponseError):
            return None

        if err.status == 429:
            self._server_error_count = 0
            delay = _parse_retry_after(
                (err.headers or {}).get("Retry-After")
            ) or RATE_LIMIT_DEFAULT_DELAY
            delay = max(MIN_BACKOFF, min(delay, MAX_RATE_LIMIT_BACKOFF))

        elif err.status >= 500:
            self._server_error_count += 1
            delay = min(
                DEFAULT_UPDATE_INTERVAL * 2**self._server_error_count,
                MAX_SERVER_ERROR_BACKOFF,
            )

        else:
            return None

        if self.update_interval is not None:
            # Never through the "polling disabled" state: a backoff must not
            # switch automatic polling back on.
            self.update_interval = delay

        return delay

    def _report_account_state(self, problem: bool) -> None:
        if self.async_report_account_state is not None:
            self.async_report_account_state(problem)

    # -- logging -----------------------------------------------------------

    def _log_failure(self, label: str) -> None:
        """Warn once per failure phase, and again if the failure changes kind."""
        if self._failure_label == label:
            _LOGGER.debug("LibreLinkUp update still failing (%s)", label)
            return

        _LOGGER.warning(
            "LibreLinkUp update failed (%s); temporarily keeping the last measurement",
            label,
        )
        self._failure_label = label

    def _log_patient_problem(self, patient_id: str) -> None:
        """Warn once per patient, never identifying them in the log."""
        if patient_id in self._patient_problem_logged:
            return

        _LOGGER.warning(
            "LibreLinkUp reported no usable measurement for a configured person; "
            "their entities will go unavailable if this persists"
        )
        self._patient_problem_logged.add(patient_id)

    def _log_patient_missing(self, patient_id: str) -> None:
        """Warn once per patient whose share has disappeared."""
        if patient_id in self._patient_missing_logged:
            return

        _LOGGER.warning(
            "A person configured for LibreLinkUp is no longer shared with this "
            "account; their entities will become unavailable"
        )
        self._patient_missing_logged.add(patient_id)
