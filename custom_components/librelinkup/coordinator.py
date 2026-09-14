from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from aiohttp import ClientResponseError

from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import LibreLinkUpApi, LibreLinkUpAuthenticationError

_LOGGER = logging.getLogger(__name__)

DEFAULT_UPDATE_INTERVAL = timedelta(seconds=60)

# How long a previously valid measurement may keep being served while updates
# fail. Data Stale already flags the reading after 5 minutes, so three stale
# windows give a short outage room to recover before Home Assistant marks the
# entities unavailable.
STALE_FAILURE_GRACE_PERIOD = timedelta(minutes=15)

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
    patients cost one login and one request per interval instead of three.
    Its data maps patient ID to that patient's latest known measurement.
    """

    def __init__(self, hass: HomeAssistant, api: LibreLinkUpApi) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name="LibreLinkUp",
            update_interval=DEFAULT_UPDATE_INTERVAL,
            # Deliberately not bound to a config entry: the coordinator outlives
            # any single entry, and binding it would make Home Assistant shut it
            # down as soon as the first of several entries is unloaded.
            config_entry=None,
        )

        self.api = api

        # Set by the runtime so an account-wide auth failure can raise reauth
        # for exactly one entry instead of one per patient.
        self.async_request_reauth: Callable[[], None] | None = None

        # Stays None until a fetch actually succeeded, so a restart can never
        # look like "last success just now" and hand out a grace period that
        # was never earned.
        self._last_successful_update: datetime | None = None
        self._failure_label: str | None = None
        self._server_error_count = 0

        # Per patient, so one person without a reading never drags the rest of
        # the account down with them.
        self._patient_seen: dict[str, datetime] = {}
        self._patient_problem_logged: set[str] = set()

    # -- entry facing ------------------------------------------------------

    def measurement_for(self, patient_id: str) -> dict | None:
        """The latest known measurement of exactly this patient."""
        return (self.data or {}).get(patient_id)

    def is_patient_available(self, patient_id: str) -> bool:
        """Whether this patient's reading is recent enough to serve."""
        last_seen = self._patient_seen.get(patient_id)

        if last_seen is None:
            return False

        return dt_util.utcnow() - last_seen < STALE_FAILURE_GRACE_PERIOD

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

        except Exception as err:
            return self._handle_failure(err)

        return self._handle_success(measurements)

    def _handle_success(self, measurements: dict[str, dict | None]) -> dict[str, dict]:
        now = dt_util.utcnow()

        self._last_successful_update = now
        self._server_error_count = 0

        if self.update_interval != DEFAULT_UPDATE_INTERVAL:
            self.update_interval = DEFAULT_UPDATE_INTERVAL

        if self._failure_label is not None:
            _LOGGER.info("LibreLinkUp updates recovered")
            self._failure_label = None

        # Start from what we already knew: a patient who is momentarily without
        # a reading keeps their previous one until their own grace period ends.
        snapshot = dict(self.data or {})

        for patient_id, measurement in measurements.items():
            if measurement is None:
                self._log_patient_problem(patient_id)
                continue

            snapshot[patient_id] = measurement
            self._patient_seen[patient_id] = now
            self._patient_problem_logged.discard(patient_id)

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

        self.update_interval = delay
        return delay

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
            "LibreLinkUp reported no usable measurement for a shared person; "
            "their entities will go unavailable if this persists"
        )
        self._patient_problem_logged.add(patient_id)
