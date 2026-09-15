"""API tolerance, measurement freshness and trend typing.

Connections are read twice with different strictness on purpose: picking a
person must never happen from an incomplete list, while polling must never lose
every configured patient because some unrelated connection cannot be read.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from unittest.mock import MagicMock

from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from helpers import libre_timestamp, measurement

from custom_components.librelinkup.api import (
    DEFAULT_BASE_URL,
    LibreLinkUpApi,
    LibreLinkUpResponseError,
)
from custom_components.librelinkup.coordinator import (
    ALLOWED_CLOCK_SKEW,
    MAX_MEASUREMENT_AGE,
    LibreLinkUpAccountCoordinator,
)
from custom_components.librelinkup.sensor import TREND_MAP, trend_code

LOGIN_URL = f"{DEFAULT_BASE_URL}/llu/auth/login"
CONNECTIONS_URL = f"{DEFAULT_BASE_URL}/llu/connections"

LOGIN_PAYLOAD = {
    "status": 0,
    "data": {
        "user": {"id": "user-id", "country": "us"},
        "authTicket": {"token": "a-token"},
    },
}


def api_for(hass, aioclient_mock, connections) -> LibreLinkUpApi:
    aioclient_mock.post(LOGIN_URL, json=LOGIN_PAYLOAD)
    aioclient_mock.get(CONNECTIONS_URL, json={"status": 0, "data": connections})

    return LibreLinkUpApi(async_get_clientsession(hass), "user@example.com", "pw")


# --- M4: one unreadable connection must not cost the whole account ------------


BROKEN_CONNECTIONS = [
    pytest.param({"patientId": None}, id="null-patient-id"),
    pytest.param({"patientId": ""}, id="empty-patient-id"),
    pytest.param({"patientId": "   "}, id="blank-patient-id"),
    pytest.param({"patientId": 42}, id="numeric-patient-id"),
    pytest.param({"firstName": "Pending invitation"}, id="no-patient-id"),
    pytest.param("not-a-connection", id="not-a-dict"),
    pytest.param(None, id="null-connection"),
]


@pytest.mark.parametrize("broken", BROKEN_CONNECTIONS)
async def test_polling_skips_a_connection_it_cannot_identify(
    hass, aioclient_mock, broken
) -> None:
    api = api_for(
        hass,
        aioclient_mock,
        [
            broken,
            {"patientId": "patient-a", "glucoseMeasurement": measurement(value=76)},
        ],
    )

    measurements = await api.async_get_measurements()

    assert set(measurements) == {"patient-a"}
    assert measurements["patient-a"]["ValueInMgPerDl"] == 76


@pytest.mark.parametrize("broken", BROKEN_CONNECTIONS)
async def test_person_selection_still_fails_closed(
    hass, aioclient_mock, broken
) -> None:
    """The user must not pick from a list that silently lost an entry."""
    api = api_for(
        hass,
        aioclient_mock,
        [broken, {"patientId": "patient-a"}],
    )

    with pytest.raises(LibreLinkUpResponseError):
        await api.async_get_connections()


async def test_a_broken_reading_only_affects_its_own_patient(
    hass, aioclient_mock
) -> None:
    api = api_for(
        hass,
        aioclient_mock,
        [
            {"patientId": "patient-a", "glucoseMeasurement": measurement(value=76)},
            {"patientId": "patient-b", "glucoseMeasurement": {"nonsense": True}},
            {"patientId": "patient-c", "glucoseMeasurement": measurement(value=110)},
        ],
    )

    measurements = await api.async_get_measurements()

    assert measurements["patient-a"]["ValueInMgPerDl"] == 76
    assert measurements["patient-b"] is None
    assert measurements["patient-c"]["ValueInMgPerDl"] == 110


# --- M5: TrendArrow is not worth discarding a glucose reading over ------------


@pytest.mark.parametrize(
    "trend",
    [0, 1, 2, 3, 4, 5, None, "3", 9, "nonsense", True, [], {}, 2.0],
)
async def test_any_trend_keeps_the_glucose_reading(hass, aioclient_mock, trend) -> None:
    reading = measurement(trend=trend)

    if trend is None:
        del reading["TrendArrow"]

    api = api_for(
        hass,
        aioclient_mock,
        [{"patientId": "patient-a", "glucoseMeasurement": reading}],
    )

    measurements = await api.async_get_measurements()

    assert measurements["patient-a"] is not None
    assert measurements["patient-a"]["ValueInMgPerDl"] == 115


# --- L3: TrendArrow typing ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0, 0),
        (3, 3),
        (5, 5),
        # Numeric strings are accepted, just like the glucose value is.
        ("3", 3),
        ("0", 0),
        (3.0, 3),
        # bool is a subclass of int: True would otherwise read as code 1 and be
        # displayed as "falling rapidly", a trend the API never sent.
        (True, None),
        (False, None),
        (None, None),
        (9, None),
        (-1, None),
        (3.5, None),
        ("3.5", None),
        ("stable", None),
        ("", None),
        ([], None),
        ({}, None),
    ],
)
def test_trend_code_coercion(raw, expected) -> None:
    assert trend_code(raw) == expected


def test_every_trend_code_has_a_state_and_an_arrow() -> None:
    assert set(TREND_MAP) == {0, 1, 2, 3, 4, 5}

    for state, _arrow in TREND_MAP.values():
        assert isinstance(state, str)


# --- M3: availability follows the measurement, not the request ---------------


@pytest.fixture
async def coordinator(hass):
    api = LibreLinkUpApi(async_get_clientsession(hass), "user@example.com", "pw")
    coordinator = LibreLinkUpAccountCoordinator(hass, api)
    coordinator.async_set_configured_patients(["patient-a"])
    return coordinator


async def test_an_unchanged_reading_does_not_stay_available_forever(
    hass, coordinator, freezer
) -> None:
    """The API keeps answering with the last reading when a sensor goes quiet.

    Availability used to be refreshed by every successful request, so a reading
    from hours ago kept being served as the current glucose value with only the
    Data Stale flag hinting at it.
    """
    from unittest.mock import AsyncMock

    stuck = measurement()
    coordinator.api.async_get_measurements = AsyncMock(
        return_value={"patient-a": stuck}
    )

    await coordinator.async_refresh()

    assert coordinator.is_patient_available("patient-a") is True

    # Six hours later the API still returns that very same reading.
    freezer.move_to(dt_util.utcnow() + timedelta(hours=6))
    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.is_patient_available("patient-a") is False


@pytest.mark.parametrize(
    ("age_minutes", "available"),
    [
        (0, True),
        (5, True),
        (14, True),
        (16, False),
        (60, False),
    ],
)
async def test_availability_follows_the_measurement_age(
    hass, coordinator, age_minutes, available
) -> None:
    from unittest.mock import AsyncMock

    coordinator.api.async_get_measurements = AsyncMock(
        return_value={"patient-a": measurement(minutes_ago=age_minutes)}
    )

    await coordinator.async_refresh()

    assert coordinator.is_patient_available("patient-a") is available


@pytest.mark.parametrize(
    ("minutes_ahead", "usable"),
    [
        # Nothing ahead at all, and a skew well inside the tolerance.
        (0.0, True),
        (1.0, True),
        # Exactly on the tolerance is still accepted: the limit is what may not
        # be exceeded, not what may not be reached.
        (2.0, True),
        # Just past it, and far past it.
        (2.5, False),
        (120.0, False),
    ],
)
async def test_a_future_timestamp_is_refused_past_the_clock_skew(
    hass, coordinator, minutes_ahead, usable
) -> None:
    """A reading from the future would never age, so it is never trusted.

    Every age is "now minus the measurement", so a timestamp ahead of the host
    clock produces a negative age -- and a negative age is younger than any
    limit. Such a reading would stay available with Data Stale off for as long
    as it stands, which is exactly what the age limit exists to prevent. A
    little skew is normal and must not cost a reading; more than that is
    refused.
    """
    from unittest.mock import AsyncMock

    coordinator.api.async_get_measurements = AsyncMock(
        return_value={"patient-a": measurement(minutes_ago=-minutes_ahead)}
    )

    await coordinator.async_refresh()

    # The account itself polled fine either way.
    assert coordinator.last_update_success is True
    assert coordinator.is_patient_available("patient-a") is usable

    if usable:
        return

    # Refused means refused everywhere: the reading is not in the snapshot and
    # no timestamp was kept for it, so nothing downstream can call it fresh.
    assert coordinator.measurement_for("patient-a") is None
    assert coordinator.measured_at_for("patient-a") is None


async def test_a_future_timestamp_never_reads_as_available_and_fresh(
    hass, coordinator
) -> None:
    """The combination that would be actively misleading on a health entity."""
    from unittest.mock import AsyncMock

    from custom_components.librelinkup.binary_sensor import LibreLinkUpDataStaleSensor

    coordinator.api.async_get_measurements = AsyncMock(
        return_value={"patient-a": measurement(minutes_ago=-120)}
    )

    await coordinator.async_refresh()

    entry = MagicMock()
    entry.entry_id = "entry"
    entry.data = {"patient_id": "patient-a"}
    stale = LibreLinkUpDataStaleSensor(coordinator, entry)

    # Never "available, and explicitly not stale" -- the reading is refused, so
    # the entity is unavailable and its own answer is "stale" rather than "off".
    assert coordinator.is_patient_available("patient-a") is False
    assert stale.is_on is True


async def test_a_future_timestamp_only_costs_its_own_patient(hass, coordinator) -> None:
    """One implausible clock must not expire everybody else on the account."""
    from unittest.mock import AsyncMock

    coordinator.async_set_configured_patients(["patient-a", "patient-b"])
    coordinator.api.async_get_measurements = AsyncMock(
        return_value={
            "patient-a": measurement(),
            "patient-b": measurement(minutes_ago=-120),
        }
    )

    await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.is_patient_available("patient-a") is True
    assert coordinator.is_patient_available("patient-b") is False
    # And A's reading is really A's, not a fallback.
    assert coordinator.measurement_for("patient-a")["ValueInMgPerDl"] == 115


async def test_a_future_timestamp_does_not_replace_a_good_reading(
    hass, coordinator
) -> None:
    """A patient keeps the last reading that was plausible, and it ages."""
    from unittest.mock import AsyncMock

    coordinator.api.async_get_measurements = AsyncMock(
        return_value={"patient-a": measurement(value=115)}
    )
    await coordinator.async_refresh()

    coordinator.api.async_get_measurements = AsyncMock(
        return_value={"patient-a": measurement(value=200, minutes_ago=-120)}
    )
    await coordinator.async_refresh()

    # The implausible reading was dropped, not adopted...
    assert coordinator.measurement_for("patient-a")["ValueInMgPerDl"] == 115
    # ...and the kept one still expires on its own age.
    assert coordinator.is_patient_available("patient-a") is True


async def test_the_clock_skew_tolerance_is_its_own_value() -> None:
    """Not derived from the age limits: it answers a different question."""
    assert ALLOWED_CLOCK_SKEW == timedelta(minutes=2)
    assert ALLOWED_CLOCK_SKEW != MAX_MEASUREMENT_AGE


async def test_the_age_limit_matches_the_documented_value() -> None:
    assert MAX_MEASUREMENT_AGE == timedelta(minutes=15)


async def test_a_reading_without_a_readable_timestamp_is_unusable(
    hass, aioclient_mock
) -> None:
    """Freshness comes from FactoryTimestamp, so it has to be parsable."""
    api = api_for(
        hass,
        aioclient_mock,
        [
            {
                "patientId": "patient-a",
                "glucoseMeasurement": measurement(
                    FactoryTimestamp="2026-09-14T11:12:35Z"
                ),
            }
        ],
    )

    assert await api.async_get_measurements() == {"patient-a": None}


async def test_the_timestamp_helper_round_trips(hass) -> None:
    """Guards the test helper itself against drifting from the real format."""
    from custom_components.librelinkup.utils import parse_libre_timestamp

    moment = dt_util.utcnow().replace(microsecond=0)

    assert parse_libre_timestamp(libre_timestamp(moment)) == moment


# --- L1: a concurrent re-login must not break an in-flight request -----------


async def test_a_request_survives_a_token_swap_underneath_it(
    hass, aioclient_mock
) -> None:
    """The headers are built from a snapshot, not read back from the client.

    Reading them back turned a concurrent re-login -- which briefly clears token
    and account ID -- into a RuntimeError and cost a poll.
    """
    api = api_for(
        hass,
        aioclient_mock,
        [{"patientId": "patient-a", "glucoseMeasurement": measurement()}],
    )
    await api.async_login()

    token, account_id = api._token, api._account_id

    # Exactly what another task doing a re-login does to the shared client.
    api._token = None
    api._account_id = None

    headers = api._auth_headers(token, account_id)

    assert headers["Authorization"] == f"Bearer {token}"
    assert headers["Account-Id"] == account_id
