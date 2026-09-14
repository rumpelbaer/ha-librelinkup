from __future__ import annotations

import logging
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime

from aiohttp import ClientResponseError

from homeassistant.config_entries import ConfigEntry, ConfigEntryAuthFailed
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import LibreLinkUpApi, LibreLinkUpAuthenticationError
from .const import CONF_PATIENT_ID

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
    contains the patient ID, and its repr() contains the bearer token.
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


class LibreLinkUpCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            name="LibreLinkUp",
            update_interval=DEFAULT_UPDATE_INTERVAL,
        )

        session = async_get_clientsession(hass)

        self.api = LibreLinkUpApi(
            session,
            entry.data[CONF_EMAIL],
            entry.data[CONF_PASSWORD],
        )

        # Required by async_setup_entry, which rejects entries without it.
        # Never derived from the API: picking a connection here could silently
        # attach the entry to a different person.
        self.patient_id: str = entry.data[CONF_PATIENT_ID]

        # Stays None until a fetch actually succeeded, so a restart can never
        # look like "last success just now" and hand out a grace period that
        # was never earned.
        self._last_successful_update: datetime | None = None
        self._failure_label: str | None = None
        self._server_error_count = 0

    async def _async_update_data(self) -> dict:
        try:
            measurement = await self.api.async_get_glucose_measurement(
                self.patient_id
            )

        except LibreLinkUpAuthenticationError as err:
            # Only raised when the login itself rejected the credentials.
            raise ConfigEntryAuthFailed(
                "LibreLinkUp authentication failed"
            ) from err

        except Exception as err:
            return self._handle_failure(err)

        self._handle_success()
        return measurement

    def _handle_success(self) -> None:
        self._last_successful_update = dt_util.utcnow()
        self._server_error_count = 0

        if self.update_interval != DEFAULT_UPDATE_INTERVAL:
            self.update_interval = DEFAULT_UPDATE_INTERVAL

        if self._failure_label is not None:
            _LOGGER.info("LibreLinkUp updates recovered")
            self._failure_label = None

    def _handle_failure(self, err: Exception) -> dict:
        label = _error_label(err)
        backoff = self._apply_backoff(err)
        self._log_failure(label)

        if self.data and self._within_grace_period():
            return self.data

        # Static message, and "from None" so the original exception cannot
        # reach a traceback with the token or the patient ID in it.
        raise UpdateFailed(
            "LibreLinkUp data update failed",
            retry_after=backoff.total_seconds() if backoff else None,
        ) from None

    def _within_grace_period(self) -> bool:
        if self._last_successful_update is None:
            return False

        age = dt_util.utcnow() - self._last_successful_update
        return age < STALE_FAILURE_GRACE_PERIOD

    def _apply_backoff(self, err: Exception) -> timedelta | None:
        """Slow polling down for server-side failures. Returns the new delay.

        Applied only to rate limits and server errors: a malformed payload or
        a missing measurement is not something the server needs relief from.
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
