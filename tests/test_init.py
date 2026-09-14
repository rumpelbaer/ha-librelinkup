"""Setup-level tests: device identity per config entry and required patient_id."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import measurement

from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)



def make_entry(hass, *, unique_id, patient_id, patient_name, email="user@example.com"):
    data = {
        CONF_EMAIL: email,
        CONF_PASSWORD: "secret",
        CONF_PATIENT_ID: patient_id,
    }

    if patient_name is not None:
        data[CONF_PATIENT_NAME] = patient_name

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"LibreLinkUp - {patient_name}",
        data=data,
        unique_id=unique_id,
    )
    entry.add_to_hass(hass)
    return entry


def snapshot_for(*entries, value=115):
    """The account snapshot the shared /connections poll would produce."""
    return {
        entry.data[CONF_PATIENT_ID]: measurement(value=value)
        for entry in entries
        if entry.data.get(CONF_PATIENT_ID)
    }


def account_api_patch(measurements=None, connections=None, side_effect=None):
    """Patch the shared client so no real request is ever made."""
    return (
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(return_value=connections or []),
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(
                return_value=measurements if measurements is not None else {},
                side_effect=side_effect,
            ),
        ),
    )


async def setup_entries(hass, *entries, measurements=None, connections=None):
    """Set the given entries up with the shared API mocked out.

    Returns the async_get_measurements mock so tests can count polls. Entries
    already loaded as a side effect of loading the component are skipped.
    """
    if measurements is None:
        measurements = snapshot_for(*entries)

    login_patch, connections_patch, measurements_patch = account_api_patch(
        measurements, connections
    )

    with login_patch, connections_patch as connections_mock, measurements_patch as measurements_mock:
        for entry in entries:
            if entry.state is ConfigEntryState.NOT_LOADED:
                await hass.config_entries.async_setup(entry.entry_id)
                await hass.async_block_till_done()

        await hass.async_block_till_done()

    setup_entries.last_connections_mock = connections_mock
    return measurements_mock


async def test_two_patients_get_separate_named_devices(hass) -> None:
    """Each config entry must become its own device, named after its patient."""
    first = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    second = make_entry(
        hass, unique_id="b", patient_id="patient-2", patient_name="Test User B"
    )

    await setup_entries(hass, first, second)

    registry = dr.async_get(hass)

    device_a = registry.async_get_device(identifiers={(DOMAIN, first.entry_id)})
    device_b = registry.async_get_device(identifiers={(DOMAIN, second.entry_id)})

    assert device_a is not None
    assert device_b is not None
    assert device_a.id != device_b.id
    assert device_a.name == "Test User A"
    assert device_b.name == "Test User B"


async def test_entity_names_and_ids_do_not_collide(hass) -> None:
    """Two patients must yield distinguishable entities, not Glucose / Glucose_2."""
    first = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    second = make_entry(
        hass, unique_id="b", patient_id="patient-2", patient_name="Test User B"
    )

    await setup_entries(hass, first, second)

    registry = er.async_get(hass)

    entries = {
        entry.unique_id: entry
        for entry in registry.entities.values()
        if entry.platform == DOMAIN
    }

    # 4 sensors + 3 binary sensors per config entry, each with its own unique ID.
    assert len(entries) == 14
    assert f"{first.entry_id}_glucose" in entries
    assert f"{second.entry_id}_glucose" in entries

    glucose_a = entries[f"{first.entry_id}_glucose"]
    glucose_b = entries[f"{second.entry_id}_glucose"]

    assert glucose_a.entity_id != glucose_b.entity_id
    assert glucose_a.device_id != glucose_b.device_id

    # has_entity_name makes Home Assistant prefix the device name.
    name_a = hass.states.get(glucose_a.entity_id).attributes["friendly_name"]
    name_b = hass.states.get(glucose_b.entity_id).attributes["friendly_name"]

    assert name_a == "Test User A Glucose"
    assert name_b == "Test User B Glucose"
    assert name_a != name_b


async def test_all_entities_of_an_entry_share_one_device(hass) -> None:
    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )

    await setup_entries(hass, entry)

    registry = er.async_get(hass)
    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )

    entity_entries = [
        item
        for item in registry.entities.values()
        if item.config_entry_id == entry.entry_id
    ]

    assert len(entity_entries) == 7
    assert {item.device_id for item in entity_entries} == {device.id}
    assert {item.domain for item in entity_entries} == {"sensor", "binary_sensor"}


async def test_device_name_falls_back_without_patient_name(hass) -> None:
    """A legacy entry without a stored name gets a generic device name."""
    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name=None
    )

    await setup_entries(hass, entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )

    assert entry.state is ConfigEntryState.LOADED
    assert device.name == "LibreLinkUp Patient"


async def test_missing_patient_id_fails_setup(hass) -> None:
    """A config entry without a patient must fail permanently, not retry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LibreLinkUp",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        unique_id="legacy",
    )
    entry.add_to_hass(hass)

    await setup_entries(hass, entry)

    # SETUP_ERROR, not SETUP_RETRY: retrying could never fix a missing patient.
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert not hass.states.async_entity_ids(DOMAIN)


async def test_missing_patient_id_never_picks_a_connection(hass) -> None:
    """The central M10 regression test.

    The old code fell back to connections[0], which could silently attach the
    entry to a different person. The API must not even be consulted.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LibreLinkUp",
        data={CONF_EMAIL: "user@example.com", CONF_PASSWORD: "secret"},
        unique_id="legacy",
    )
    entry.add_to_hass(hass)

    measurements_mock = await setup_entries(
        hass,
        entry,
        connections=[
            {"patientId": "someone-else", "firstName": "Someone", "lastName": "Else"},
            {"patientId": "patient-1", "firstName": "Test", "lastName": "User"},
        ],
    )

    measurements_mock.assert_not_awaited()
    setup_entries.last_connections_mock.assert_not_awaited()
    assert entry.state is ConfigEntryState.SETUP_ERROR

    devices = dr.async_get(hass).devices.get_devices_for_config_entry_id(
        entry.entry_id
    )

    assert list(devices) == []


@pytest.mark.parametrize("patient_name", ["Test User A", "Renamed Person"])
async def test_identifiers_are_independent_of_patient_name(hass, patient_name) -> None:
    """Renaming the patient must not move the device or the entity unique IDs."""
    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name=patient_name
    )

    await setup_entries(hass, entry)

    device = dr.async_get(hass).async_get_device(
        identifiers={(DOMAIN, entry.entry_id)}
    )

    assert device.identifiers == {(DOMAIN, entry.entry_id)}
    assert device.name == patient_name

    unique_ids = {
        item.unique_id
        for item in er.async_get(hass).entities.values()
        if item.config_entry_id == entry.entry_id
    }

    assert unique_ids == {
        f"{entry.entry_id}_glucose",
        f"{entry.entry_id}_trend",
        f"{entry.entry_id}_last_reading",
        f"{entry.entry_id}_reading_age",
        f"{entry.entry_id}_data_stale",
        f"{entry.entry_id}_low",
        f"{entry.entry_id}_high",
    }


async def test_normal_entry_still_loads(hass) -> None:
    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )

    await setup_entries(hass, entry)

    assert entry.state is ConfigEntryState.LOADED

    coordinator = entry.runtime_data

    assert coordinator.measurement_for("patient-1")["ValueInMgPerDl"] == 115
    assert coordinator.is_patient_available("patient-1") is True

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


# --- Availability during outages (H1) ----------------------------------------


async def _advance(hass, seconds=90, freezer=None):
    """Let the next scheduled poll happen.

    Pass the freezer whenever the test cares about measurement age: a patient's
    entities go unavailable once their own reading is older than
    MAX_MEASUREMENT_AGE, and that is measured against the real clock.
    """
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    target = dt_util.utcnow() + timedelta(seconds=seconds)

    if freezer is not None:
        freezer.move_to(target)

    async_fire_time_changed(hass, target)
    await hass.async_block_till_done()


async def test_entity_stays_available_during_short_outage(hass, freezer) -> None:
    from custom_components.librelinkup.api import LibreLinkUpResponseError

    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    await setup_entries(hass, entry)

    assert hass.states.get("sensor.test_user_a_glucose").state == "6.4"

    coordinator = entry.runtime_data

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=LibreLinkUpResponseError("malformed")),
    ):
        # Six minutes of failures: past Data Stale, well inside the age limit.
        await _advance(hass, seconds=360, freezer=freezer)

    glucose = hass.states.get("sensor.test_user_a_glucose")

    # Still the last valid reading, and still available.
    assert glucose.state == "6.4"
    assert coordinator.last_update_success is True
    # Data Stale reports the problem via the measurement timestamp.
    assert hass.states.get("binary_sensor.test_user_a_data_stale").state == "on"


async def test_entity_becomes_unavailable_after_grace_period(hass) -> None:
    from custom_components.librelinkup.api import LibreLinkUpResponseError
    from custom_components.librelinkup.coordinator import (
        STALE_FAILURE_GRACE_PERIOD,
    )
    from homeassistant.util import dt as dt_util

    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    await setup_entries(hass, entry)

    coordinator = entry.runtime_data

    # Age the last success past the grace period instead of waiting 15 minutes.
    coordinator._last_successful_update = (
        dt_util.utcnow() - STALE_FAILURE_GRACE_PERIOD - timedelta(minutes=1)
    )

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=LibreLinkUpResponseError("malformed")),
    ):
        await _advance(hass)

    assert coordinator.last_update_success is False

    for entity_id in (
        "sensor.test_user_a_glucose",
        "sensor.test_user_a_trend",
        "binary_sensor.test_user_a_data_stale",
    ):
        assert hass.states.get(entity_id).state == "unavailable"

    # And it recovers.
    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value={"patient-1": measurement()}),
    ):
        await _advance(hass)

    assert coordinator.last_update_success is True
    assert hass.states.get("sensor.test_user_a_glucose").state == "6.4"


async def test_rate_limit_slows_polling_without_reauth(hass) -> None:
    from aiohttp import ClientResponseError, RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy

    url = "https://api-de.libreview.io/llu/connections/patient-1/graph"
    error = ClientResponseError(
        RequestInfo(
            url=url,
            method="GET",
            headers=CIMultiDictProxy(CIMultiDict()),
            real_url=url,
        ),
        (),
        status=429,
        headers=CIMultiDictProxy(CIMultiDict({"Retry-After": "300"})),
    )

    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    await setup_entries(hass, entry)

    coordinator = entry.runtime_data

    assert coordinator.update_interval == timedelta(seconds=60)

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=error),
    ):
        await _advance(hass)

    assert coordinator.update_interval == timedelta(seconds=300)
    assert not hass.config_entries.flow.async_progress()
    assert hass.states.get("sensor.test_user_a_glucose").state == "6.4"


# --- Shared account runtime (M6) ---------------------------------------------


def account_runtimes(hass):
    return hass.data.get(DOMAIN, {})


async def test_same_account_shares_one_client_and_coordinator(hass) -> None:
    first = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    second = make_entry(
        hass, unique_id="b", patient_id="patient-2", patient_name="Test User B"
    )

    await setup_entries(hass, first, second)

    runtimes = account_runtimes(hass)

    assert len(runtimes) == 1

    runtime = next(iter(runtimes.values()))

    assert set(runtime.entries) == {first.entry_id, second.entry_id}
    assert first.runtime_data is second.runtime_data
    assert first.runtime_data is runtime.coordinator
    assert runtime.coordinator.api is runtime.api


async def test_one_poll_per_interval_regardless_of_patient_count(hass) -> None:
    """M6: three patients of one account cost one request, not three."""
    entries = [
        make_entry(
            hass, unique_id=key, patient_id=f"patient-{key}", patient_name=f"User {key}"
        )
        for key in ("a", "b", "c")
    ]

    measurements_mock = await setup_entries(hass, *entries)

    # An entry only polls when the snapshot holds nothing for its own patient
    # yet, so setup costs at most one request per entry -- and never one per
    # patient per interval afterwards.
    assert measurements_mock.await_count <= len(entries)

    coordinator = entries[0].runtime_data

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=snapshot_for(*entries)),
    ) as polled:
        await _advance(hass)

    assert polled.await_count == 1
    assert all(entry.runtime_data is coordinator for entry in entries)


async def test_different_accounts_stay_separate(hass) -> None:
    first = make_entry(
        hass,
        unique_id="a",
        patient_id="patient-1",
        patient_name="Test User A",
        email="one@example.com",
    )
    second = make_entry(
        hass,
        unique_id="b",
        patient_id="patient-2",
        patient_name="Test User B",
        email="two@example.com",
    )

    await setup_entries(hass, first, second)

    runtimes = account_runtimes(hass)

    assert len(runtimes) == 2

    api_objects = {id(runtime.api) for runtime in runtimes.values()}
    coordinators = {id(runtime.coordinator) for runtime in runtimes.values()}

    assert len(api_objects) == 2
    assert len(coordinators) == 2
    assert first.runtime_data is not second.runtime_data


async def test_email_case_and_padding_join_the_same_account(hass) -> None:
    first = make_entry(
        hass,
        unique_id="a",
        patient_id="patient-1",
        patient_name="Test User A",
        email="user@example.com",
    )
    second = make_entry(
        hass,
        unique_id="b",
        patient_id="patient-2",
        patient_name="Test User B",
        email="  User@Example.COM  ",
    )

    await setup_entries(hass, first, second)

    assert len(account_runtimes(hass)) == 1
    assert first.runtime_data is second.runtime_data


# --- Data isolation between patients -----------------------------------------


ISOLATION_MGDL = {
    "patient-a": 76,   # 4.2 mmol/L
    "patient-b": 249,  # 13.8 mmol/L
    "patient-c": 110,  # 6.1 mmol/L
}
ISOLATION_EXPECTED = {
    "sensor.user_a_glucose": "4.2",
    "sensor.user_b_glucose": "13.8",
    "sensor.user_c_glucose": "6.1",
}


def isolation_snapshot(order=("patient-a", "patient-b", "patient-c")):
    return {
        patient_id: measurement(value=ISOLATION_MGDL[patient_id])
        for patient_id in order
    }


async def setup_isolation_entries(hass):
    entries = [
        make_entry(
            hass,
            unique_id=letter,
            patient_id=f"patient-{letter}",
            patient_name=f"User {letter.upper()}",
        )
        for letter in ("a", "b", "c")
    ]
    await setup_entries(hass, *entries, measurements=isolation_snapshot())
    return entries


async def test_patients_never_see_each_others_readings(hass) -> None:
    await setup_isolation_entries(hass)

    for entity_id, expected in ISOLATION_EXPECTED.items():
        assert hass.states.get(entity_id).state == expected


@pytest.mark.parametrize(
    "order",
    [
        ("patient-c", "patient-a", "patient-b"),
        ("patient-b", "patient-c", "patient-a"),
        ("patient-c", "patient-b", "patient-a"),
    ],
)
async def test_connection_order_never_reassigns_readings(hass, order) -> None:
    """Selection is by patient ID; list position must be irrelevant."""
    await setup_isolation_entries(hass)

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=isolation_snapshot(order)),
    ):
        await _advance(hass)

    for entity_id, expected in ISOLATION_EXPECTED.items():
        assert hass.states.get(entity_id).state == expected


async def test_readings_survive_a_reload_without_mixing(hass) -> None:
    entries = await setup_isolation_entries(hass)

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=isolation_snapshot()),
    ):
        await hass.config_entries.async_reload(entries[1].entry_id)
        await hass.async_block_till_done()

    for entity_id, expected in ISOLATION_EXPECTED.items():
        assert hass.states.get(entity_id).state == expected

    assert len(account_runtimes(hass)) == 1


# --- Patient level failures do not spread ------------------------------------


async def test_one_patient_without_a_reading_does_not_affect_the_others(hass) -> None:
    entries = await setup_isolation_entries(hass)

    partial = isolation_snapshot()
    partial["patient-b"] = None  # shared, but no usable reading right now

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=partial),
    ):
        await _advance(hass)

    coordinator = entries[0].runtime_data

    # The account poll itself succeeded.
    assert coordinator.last_update_success is True

    # A and C keep updating, B holds its previous reading for now.
    assert hass.states.get("sensor.user_a_glucose").state == "4.2"
    assert hass.states.get("sensor.user_c_glucose").state == "6.1"
    assert hass.states.get("sensor.user_b_glucose").state == "13.8"


async def test_patient_without_a_reading_goes_unavailable_alone(hass) -> None:
    from custom_components.librelinkup.coordinator import (
        MAX_MEASUREMENT_AGE,
        STALE_FAILURE_GRACE_PERIOD,
    )
    from homeassistant.util import dt as dt_util

    entries = await setup_isolation_entries(hass)
    coordinator = entries[0].runtime_data

    partial = isolation_snapshot()
    partial["patient-b"] = None

    # Age only B's last sighting past its own grace period.
    coordinator._measured_at["patient-b"] = (
        dt_util.utcnow() - MAX_MEASUREMENT_AGE - timedelta(minutes=1)
    )

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=partial),
    ):
        await _advance(hass)

    assert hass.states.get("sensor.user_b_glucose").state == "unavailable"
    assert hass.states.get("binary_sensor.user_b_data_stale").state == "unavailable"

    # A and C are untouched.
    assert hass.states.get("sensor.user_a_glucose").state == "4.2"
    assert hass.states.get("sensor.user_c_glucose").state == "6.1"


async def test_revoked_share_never_falls_back_to_another_patient(hass) -> None:
    """The patient vanishes from /connections entirely."""
    from custom_components.librelinkup.coordinator import (
        MAX_MEASUREMENT_AGE,
        STALE_FAILURE_GRACE_PERIOD,
    )
    from homeassistant.util import dt as dt_util

    entries = await setup_isolation_entries(hass)
    coordinator = entries[0].runtime_data

    without_b = {
        patient_id: measurement
        for patient_id, measurement in isolation_snapshot().items()
        if patient_id != "patient-b"
    }
    coordinator._measured_at["patient-b"] = (
        dt_util.utcnow() - MAX_MEASUREMENT_AGE - timedelta(minutes=1)
    )

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=without_b),
    ):
        await _advance(hass)

    assert hass.states.get("sensor.user_b_glucose").state == "unavailable"
    # Never adopts someone else's reading.
    assert hass.states.get("sensor.user_a_glucose").state == "4.2"
    assert hass.states.get("sensor.user_c_glucose").state == "6.1"
    assert not hass.config_entries.flow.async_progress()


async def test_account_wide_failure_affects_every_patient(hass) -> None:
    from custom_components.librelinkup.api import LibreLinkUpResponseError
    from custom_components.librelinkup.coordinator import (
        MAX_MEASUREMENT_AGE,
        STALE_FAILURE_GRACE_PERIOD,
    )
    from homeassistant.util import dt as dt_util

    entries = await setup_isolation_entries(hass)
    coordinator = entries[0].runtime_data

    coordinator._last_successful_update = (
        dt_util.utcnow() - STALE_FAILURE_GRACE_PERIOD - timedelta(minutes=1)
    )

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=LibreLinkUpResponseError("malformed")),
    ):
        await _advance(hass)

    assert coordinator.last_update_success is False

    for entity_id in ISOLATION_EXPECTED:
        assert hass.states.get(entity_id).state == "unavailable"


async def test_one_backoff_for_the_whole_account(hass) -> None:
    from aiohttp import ClientResponseError, RequestInfo
    from multidict import CIMultiDict, CIMultiDictProxy

    url = "https://api-de.libreview.io/llu/connections"
    error = ClientResponseError(
        RequestInfo(
            url=url, method="GET", headers=CIMultiDictProxy(CIMultiDict()), real_url=url
        ),
        (),
        status=429,
        headers=CIMultiDictProxy(CIMultiDict({"Retry-After": "300"})),
    )

    entries = await setup_isolation_entries(hass)
    coordinator = entries[0].runtime_data

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=error),
    ) as polled:
        await _advance(hass)

    # One shared 429 slows the whole account once, not once per patient.
    assert polled.await_count == 1
    assert coordinator.update_interval == timedelta(seconds=300)
    assert coordinator._server_error_count == 0
    assert all(entry.runtime_data is coordinator for entry in entries)


# --- Lifecycle ---------------------------------------------------------------


async def test_unloading_one_entry_keeps_the_others_running(hass) -> None:
    entries = await setup_isolation_entries(hass)
    runtime = next(iter(account_runtimes(hass).values()))
    coordinator = runtime.coordinator

    # First entry.
    assert await hass.config_entries.async_unload(entries[0].entry_id)
    await hass.async_block_till_done()

    assert set(runtime.entries) == {entries[1].entry_id, entries[2].entry_id}
    assert account_runtimes(hass)
    assert hass.states.get("sensor.user_b_glucose").state == "13.8"

    # Middle entry.
    assert await hass.config_entries.async_unload(entries[1].entry_id)
    await hass.async_block_till_done()

    assert set(runtime.entries) == {entries[2].entry_id}
    assert hass.states.get("sensor.user_c_glucose").state == "6.1"

    # The shared coordinator is still polling for whoever is left.
    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=isolation_snapshot()),
    ) as polled:
        await _advance(hass)

    assert polled.await_count == 1
    assert coordinator.last_update_success is True


async def test_unloading_the_last_entry_tears_the_account_down(hass) -> None:
    entries = await setup_isolation_entries(hass)

    for entry in entries:
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()

    assert account_runtimes(hass) == {}

    # No further polling once the last entry is gone.
    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=isolation_snapshot()),
    ) as polled:
        await _advance(hass)

    assert polled.await_count == 0


async def test_reload_does_not_duplicate_the_account_runtime(hass) -> None:
    entries = await setup_isolation_entries(hass)
    before = next(iter(account_runtimes(hass).values())).coordinator

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=isolation_snapshot()),
    ):
        await hass.config_entries.async_reload(entries[0].entry_id)
        await hass.async_block_till_done()

    runtimes = account_runtimes(hass)

    assert len(runtimes) == 1

    runtime = next(iter(runtimes.values()))

    assert runtime.coordinator is before
    assert set(runtime.entries) == {entry.entry_id for entry in entries}

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=isolation_snapshot()),
    ) as polled:
        await _advance(hass)

    # Still exactly one poll loop, not two.
    assert polled.await_count == 1


async def test_reloading_the_only_entry_rebuilds_the_account(hass) -> None:
    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    await setup_entries(hass, entry)

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_login",
        new=AsyncMock(),
    ), patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value={"patient-1": measurement()}),
    ):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()

    assert len(account_runtimes(hass)) == 1
    assert entry.state is ConfigEntryState.LOADED


# --- Credentials -------------------------------------------------------------


async def test_newest_entry_password_wins_for_the_shared_client(hass) -> None:
    """A reauth reloads its entry, so the entry being set up is authoritative."""
    first = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    await setup_entries(hass, first)

    runtime = next(iter(account_runtimes(hass).values()))

    assert runtime.password == "secret"

    second = MockConfigEntry(
        domain=DOMAIN,
        title="LibreLinkUp - Test User B",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "new-password",
            CONF_PATIENT_ID: "patient-2",
            CONF_PATIENT_NAME: "Test User B",
        },
        unique_id="b",
    )
    second.add_to_hass(hass)

    await setup_entries(hass, second)

    assert runtime.password == "new-password"
    assert runtime.api._password == "new-password"
    # The token obtained with the old password is dropped.
    assert runtime.api._token is None


async def test_reauth_propagates_the_password_to_sibling_entries(hass) -> None:
    """Otherwise a restart would let load order pick the account password.

    The reload is deliberately not mocked out: stubbing it is what once hid the
    account never polling again after a successful reauth. What the reload then
    does is covered in test_lifecycle.py.
    """
    first = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    second = make_entry(
        hass, unique_id="b", patient_id="patient-2", patient_name="Test User B"
    )
    await setup_entries(hass, first, second)

    login_patch, connections_patch, measurements_patch = account_api_patch(
        snapshot_for(first, second)
    )

    with (
        login_patch,
        measurements_patch,
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(
                return_value=[
                    {"patientId": "patient-1"},
                    {"patientId": "patient-2"},
                ]
            ),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_REAUTH, "entry_id": first.entry_id},
            data=dict(first.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: "rotated-password"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert first.data[CONF_PASSWORD] == "rotated-password"
    assert second.data[CONF_PASSWORD] == "rotated-password"
    # Both entries survived the real reload.
    assert first.state is ConfigEntryState.LOADED
    assert second.state is ConfigEntryState.LOADED


async def test_account_auth_failure_raises_a_single_reauth_flow(hass) -> None:
    from custom_components.librelinkup.api import LibreLinkUpAuthenticationError

    entries = await setup_isolation_entries(hass)

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=LibreLinkUpAuthenticationError("rejected")),
    ):
        await _advance(hass)

    flows = [
        flow
        for flow in hass.config_entries.flow.async_progress()
        if flow["handler"] == DOMAIN
    ]

    # One wrong password must not produce one dialog per patient.
    assert len(flows) == 1
    assert flows[0]["context"]["entry_id"] in {entry.entry_id for entry in entries}
