"""Coordinator failure handling: grace period, backoff, logging, auth (H1-H4)."""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientResponseError, RequestInfo
from homeassistant.config_entries import ConfigEntryAuthFailed
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util
from multidict import CIMultiDict, CIMultiDictProxy
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import measurement

from custom_components.librelinkup.api import (
    LibreLinkUpAccountStateError,
    LibreLinkUpApi,
    LibreLinkUpAuthenticationError,
    LibreLinkUpAuthorizationError,
    LibreLinkUpResponseError,
)
from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)
from custom_components.librelinkup.coordinator import (
    DEFAULT_UPDATE_INTERVAL,
    MAX_MEASUREMENT_AGE,
    MAX_RATE_LIMIT_BACKOFF,
    MAX_SERVER_ERROR_BACKOFF,
    MIN_BACKOFF,
    RATE_LIMIT_DEFAULT_DELAY,
    STALE_FAILURE_GRACE_PERIOD,
    LibreLinkUpAccountCoordinator,
)

EMAIL = "user@example.com"
PASSWORD = "super-secret-password"
PATIENT_ID = "patient-uuid-0001"



def http_error(status: int, retry_after: str | None = None) -> ClientResponseError:
    """An HTTP error carrying the sensitive data a real one would carry."""
    url = f"https://api-de.libreview.io/llu/connections/{PATIENT_ID}/graph"
    headers = CIMultiDict()

    if retry_after is not None:
        headers["Retry-After"] = retry_after

    return ClientResponseError(
        RequestInfo(
            url=url,
            method="GET",
            headers=CIMultiDictProxy(
                CIMultiDict({"Authorization": "Bearer super-secret-token"})
            ),
            real_url=url,
        ),
        (),
        status=status,
        message="Test error",
        headers=CIMultiDictProxy(headers),
    )


@pytest.fixture
async def coordinator(hass):
    api = LibreLinkUpApi(async_get_clientsession(hass), EMAIL, PASSWORD)
    coordinator = LibreLinkUpAccountCoordinator(hass, api)
    coordinator.async_set_configured_patients([PATIENT_ID])
    return coordinator


def configure(coordinator, *patient_ids):
    """Register the patients Home Assistant has config entries for."""
    coordinator.async_set_configured_patients(patient_ids)
    return coordinator


def with_previous_success(coordinator, *, minutes_ago: float):
    """Give the coordinator a snapshot fetched some minutes ago."""
    seen = dt_util.utcnow() - timedelta(minutes=minutes_ago)
    coordinator.data = {PATIENT_ID: measurement(minutes_ago=minutes_ago)}
    coordinator._last_successful_update = seen
    coordinator._measured_at[PATIENT_ID] = seen
    return coordinator


def fail_with(coordinator, error):
    coordinator.api.async_get_measurements = AsyncMock(side_effect=error)


def succeed(coordinator, measurements=None):
    coordinator.api.async_get_measurements = AsyncMock(
        return_value=(
            measurements
            if measurements is not None
            else {PATIENT_ID: measurement()}
        )
    )


TRANSIENT_ERRORS = [
    pytest.param(TimeoutError(), id="timeout"),
    pytest.param(http_error(503), id="http-503"),
    pytest.param(http_error(429), id="http-429"),
    pytest.param(LibreLinkUpResponseError("malformed"), id="response"),
    pytest.param(LibreLinkUpAuthorizationError("rejected"), id="authorization"),
]


# --- H1: grace period --------------------------------------------------------


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
async def test_keeps_last_measurement_inside_grace_period(coordinator, error) -> None:
    with_previous_success(coordinator, minutes_ago=5)
    fail_with(coordinator, error)

    assert await coordinator._async_update_data() is coordinator.data


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
async def test_raises_update_failed_after_grace_period(coordinator, error) -> None:
    with_previous_success(coordinator, minutes_ago=20)
    fail_with(coordinator, error)

    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()

    assert str(caught.value) == "LibreLinkUp data update failed"
    # The original error must not be reachable from a traceback.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_grace_period_boundary(coordinator) -> None:
    grace_minutes = STALE_FAILURE_GRACE_PERIOD.total_seconds() / 60

    with_previous_success(coordinator, minutes_ago=grace_minutes - 1)
    fail_with(coordinator, TimeoutError())
    assert await coordinator._async_update_data() is coordinator.data

    with_previous_success(coordinator, minutes_ago=grace_minutes + 1)
    fail_with(coordinator, TimeoutError())
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_initial_failure_raises_without_inventing_a_grace_period(
    coordinator,
) -> None:
    """A fresh coordinator (or a restart) has no earned grace period."""
    assert coordinator._last_successful_update is None

    fail_with(coordinator, TimeoutError())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_grace_period_is_measured_from_last_success_not_from_restart(
    coordinator,
) -> None:
    """Stale data present but no recorded success must not be served."""
    coordinator.data = measurement()
    coordinator._last_successful_update = None

    fail_with(coordinator, TimeoutError())

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


# --- H1: recovery ------------------------------------------------------------


async def test_recovery_resets_failure_state(coordinator) -> None:
    with_previous_success(coordinator, minutes_ago=5)
    fail_with(coordinator, http_error(503))
    await coordinator._async_update_data()

    assert coordinator._failure_label == "HTTP 503"
    assert coordinator._server_error_count == 1
    assert coordinator.update_interval != DEFAULT_UPDATE_INTERVAL

    succeed(coordinator)
    before = dt_util.utcnow()
    recovered = await coordinator._async_update_data()

    assert recovered[PATIENT_ID]["ValueInMgPerDl"] == 115

    assert coordinator._failure_label is None
    assert coordinator._server_error_count == 0
    assert coordinator.update_interval == DEFAULT_UPDATE_INTERVAL
    assert coordinator._last_successful_update >= before


# --- H2: rate limiting -------------------------------------------------------


@pytest.mark.parametrize(
    ("retry_after", "expected"),
    [
        ("300", timedelta(seconds=300)),
        ("120", timedelta(seconds=120)),
        (None, RATE_LIMIT_DEFAULT_DELAY),
        ("nonsense", RATE_LIMIT_DEFAULT_DELAY),
        ("", RATE_LIMIT_DEFAULT_DELAY),
        ("1", MIN_BACKOFF),
        ("-5", MIN_BACKOFF),
        ("999999", MAX_RATE_LIMIT_BACKOFF),
    ],
)
async def test_rate_limit_backoff(coordinator, retry_after, expected) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(429, retry_after=retry_after))

    assert await coordinator._async_update_data() == coordinator.data
    assert coordinator.update_interval == expected


async def test_rate_limit_accepts_http_date(coordinator) -> None:
    retry_at = dt_util.utcnow() + timedelta(seconds=600)
    header = retry_at.strftime("%a, %d %b %Y %H:%M:%S GMT")

    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(429, retry_after=header))

    await coordinator._async_update_data()

    assert timedelta(seconds=540) <= coordinator.update_interval <= timedelta(
        seconds=660
    )


async def test_rate_limit_does_not_trigger_reauth(coordinator) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(429))

    await coordinator._async_update_data()  # must not raise ConfigEntryAuthFailed


async def test_rate_limit_passes_retry_after_to_home_assistant(coordinator) -> None:
    """Outside the grace period HA's own retry_after mechanism is used."""
    with_previous_success(coordinator, minutes_ago=20)
    fail_with(coordinator, http_error(429, retry_after="300"))

    with pytest.raises(UpdateFailed) as caught:
        await coordinator._async_update_data()

    assert caught.value.retry_after == 300.0


async def test_interval_returns_to_normal_after_rate_limit(coordinator) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(429, retry_after="300"))
    await coordinator._async_update_data()

    assert coordinator.update_interval == timedelta(seconds=300)

    succeed(coordinator)
    await coordinator._async_update_data()

    assert coordinator.update_interval == DEFAULT_UPDATE_INTERVAL


# --- H2: server error backoff ------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_error_backoff_grows_and_is_capped(coordinator, status) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(status))

    seen = []
    for _ in range(6):
        with_previous_success(coordinator, minutes_ago=1)
        await coordinator._async_update_data()
        seen.append(coordinator.update_interval)

    assert seen[:4] == [
        timedelta(seconds=120),
        timedelta(seconds=240),
        timedelta(seconds=480),
        MAX_SERVER_ERROR_BACKOFF,
    ]
    assert all(interval <= MAX_SERVER_ERROR_BACKOFF for interval in seen)


async def test_server_error_backoff_resets_after_success(coordinator) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(503))
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    assert coordinator.update_interval == timedelta(seconds=240)

    succeed(coordinator)
    await coordinator._async_update_data()

    assert coordinator.update_interval == DEFAULT_UPDATE_INTERVAL

    fail_with(coordinator, http_error(503))
    with_previous_success(coordinator, minutes_ago=1)
    await coordinator._async_update_data()

    assert coordinator.update_interval == timedelta(seconds=120)


@pytest.mark.parametrize(
    "error",
    [
        LibreLinkUpResponseError("malformed"),
        LibreLinkUpAuthorizationError("rejected"),
        TimeoutError(),
    ],
)
async def test_no_backoff_for_non_server_errors(coordinator, error) -> None:
    """Only the server needs relief; a bad payload keeps normal polling."""
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, error)

    await coordinator._async_update_data()

    assert coordinator.update_interval == DEFAULT_UPDATE_INTERVAL


# --- H3: logging -------------------------------------------------------------


async def test_repeated_failures_log_one_warning(coordinator, caplog) -> None:
    fail_with(coordinator, http_error(503))
    caplog.set_level(logging.DEBUG)

    for _ in range(5):
        with_previous_success(coordinator, minutes_ago=1)
        await coordinator._async_update_data()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]

    assert len(warnings) == 1
    assert "HTTP 503" in warnings[0].getMessage()


async def test_changed_failure_kind_logs_again(coordinator, caplog) -> None:
    caplog.set_level(logging.DEBUG)

    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(503))
    await coordinator._async_update_data()

    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(429))
    await coordinator._async_update_data()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]

    assert len(warnings) == 2
    assert "HTTP 503" in warnings[0]
    assert "HTTP 429" in warnings[1]


async def test_recovery_logs_one_info(coordinator, caplog) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, http_error(503))
    await coordinator._async_update_data()

    caplog.clear()
    caplog.set_level(logging.DEBUG)

    succeed(coordinator)
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    infos = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "recovered" in r.getMessage()
    ]

    assert len(infos) == 1


@pytest.mark.parametrize("error", TRANSIENT_ERRORS)
async def test_logs_never_contain_sensitive_data(coordinator, caplog, error) -> None:
    caplog.set_level(logging.DEBUG)

    for _ in range(3):
        with_previous_success(coordinator, minutes_ago=1)
        await coordinator._async_update_data()

    fail_with(coordinator, error)
    with_previous_success(coordinator, minutes_ago=1)
    await coordinator._async_update_data()

    logged = caplog.text

    assert EMAIL not in logged
    assert PASSWORD not in logged
    assert PATIENT_ID not in logged
    assert "Bearer" not in logged
    assert "super-secret-token" not in logged
    assert "libreview.io" not in logged
    assert "115" not in logged


# --- H4: authentication ------------------------------------------------------


async def test_login_rejection_triggers_reauth(coordinator) -> None:
    """Only a rejected login means the stored password is wrong."""
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(
        coordinator,
        LibreLinkUpAuthenticationError("LibreLinkUp rejected the supplied credentials"),
    )

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


async def test_authentication_error_is_not_swallowed_by_grace_period(
    coordinator,
) -> None:
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, LibreLinkUpAuthenticationError("rejected"))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()


@pytest.mark.parametrize("error", [LibreLinkUpAuthorizationError("x"), http_error(401)])
async def test_transient_authorization_failure_does_not_trigger_reauth(
    coordinator, error
) -> None:
    """A 401/403 that survived a successful login is a server hiccup."""
    with_previous_success(coordinator, minutes_ago=1)
    fail_with(coordinator, error)

    assert await coordinator._async_update_data() is coordinator.data


@pytest.mark.parametrize("error", [LibreLinkUpAuthorizationError("x"), http_error(403)])
async def test_transient_authorization_failure_eventually_fails_update(
    coordinator, error
) -> None:
    with_previous_success(coordinator, minutes_ago=20)
    fail_with(coordinator, error)

    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_authorization_error_is_not_an_authentication_error() -> None:
    assert not issubclass(LibreLinkUpAuthorizationError, LibreLinkUpAuthenticationError)


# --- Per patient state inside the shared snapshot -----------------------------


OTHER_PATIENT = "patient-other"


async def test_snapshot_keeps_patients_apart(coordinator) -> None:
    configure(coordinator, PATIENT_ID, OTHER_PATIENT)
    succeed(
        coordinator,
        {
            PATIENT_ID: measurement(value=76),
            OTHER_PATIENT: measurement(value=249),
        },
    )

    await coordinator.async_refresh()

    assert coordinator.measurement_for(PATIENT_ID)["ValueInMgPerDl"] == 76
    assert coordinator.measurement_for(OTHER_PATIENT)["ValueInMgPerDl"] == 249
    assert coordinator.measurement_for("nobody") is None


async def test_unknown_patient_is_never_available(coordinator) -> None:
    succeed(coordinator, {PATIENT_ID: measurement()})
    await coordinator.async_refresh()

    assert coordinator.is_patient_available(PATIENT_ID) is True
    assert coordinator.is_patient_available("nobody") is False


async def test_patient_without_a_reading_keeps_the_others_fresh(coordinator) -> None:
    configure(coordinator, PATIENT_ID, OTHER_PATIENT)
    succeed(
        coordinator,
        {
            PATIENT_ID: measurement(value=76),
            OTHER_PATIENT: measurement(value=249),
        },
    )
    await coordinator.async_refresh()

    succeed(
        coordinator,
        {
            PATIENT_ID: measurement(value=80),
            OTHER_PATIENT: None,
        },
    )
    await coordinator.async_refresh()

    # The account poll succeeded, so this is not an account level failure.
    assert coordinator.last_update_success is True
    assert coordinator.measurement_for(PATIENT_ID)["ValueInMgPerDl"] == 80
    # The other patient keeps their last reading for their own grace period.
    assert coordinator.measurement_for(OTHER_PATIENT)["ValueInMgPerDl"] == 249
    assert coordinator.is_patient_available(OTHER_PATIENT) is True


async def test_patient_grace_period_expires_individually(coordinator) -> None:
    configure(coordinator, PATIENT_ID, OTHER_PATIENT)
    succeed(
        coordinator,
        {PATIENT_ID: measurement(), OTHER_PATIENT: measurement()},
    )
    await coordinator.async_refresh()

    coordinator._measured_at[OTHER_PATIENT] = (
        dt_util.utcnow() - MAX_MEASUREMENT_AGE - timedelta(minutes=1)
    )

    assert coordinator.is_patient_available(PATIENT_ID) is True
    assert coordinator.is_patient_available(OTHER_PATIENT) is False


async def test_patient_problem_warns_only_once(coordinator, caplog) -> None:
    configure(coordinator, PATIENT_ID, OTHER_PATIENT)
    succeed(coordinator, {PATIENT_ID: measurement(), OTHER_PATIENT: None})
    caplog.set_level(logging.DEBUG)

    for _ in range(4):
        await coordinator.async_refresh()

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "no usable measurement" in record.getMessage()
    ]

    assert len(warnings) == 1
    assert OTHER_PATIENT not in caplog.text
    assert PATIENT_ID not in caplog.text


async def test_patient_problem_warns_again_after_recovery(coordinator, caplog) -> None:
    configure(coordinator, OTHER_PATIENT)
    succeed(coordinator, {OTHER_PATIENT: None})
    await coordinator.async_refresh()

    succeed(coordinator, {OTHER_PATIENT: measurement()})
    await coordinator.async_refresh()

    caplog.clear()
    caplog.set_level(logging.DEBUG)

    succeed(coordinator, {OTHER_PATIENT: None})
    await coordinator.async_refresh()

    warnings = [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and "no usable measurement" in record.getMessage()
    ]

    assert len(warnings) == 1


async def test_account_failure_reports_reauth_once(coordinator) -> None:
    calls = []
    coordinator.async_request_reauth = lambda: calls.append(1)

    fail_with(coordinator, LibreLinkUpAuthenticationError("rejected"))

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_update_data()

    assert len(calls) == 1


async def test_coordinator_is_not_bound_to_a_config_entry(coordinator) -> None:
    """Binding it would shut the shared poll down with the first entry."""
    assert coordinator.config_entry is None
