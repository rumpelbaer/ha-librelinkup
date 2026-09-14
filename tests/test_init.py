"""Setup-level tests: device identity per config entry and required patient_id."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)

MEASUREMENT = {
    "Value": 6.4,
    "ValueInMgPerDl": 115,
    "TrendArrow": 3,
    "Timestamp": "9/14/2026 1:12:35 PM",
    "FactoryTimestamp": "9/14/2026 11:12:35 AM",
    "isHigh": False,
    "isLow": False,
}


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


async def setup_entries(hass, *entries, connections=None):
    """Set the given entries up with the API mocked out.

    Returns the async_get_connections mock so tests can assert it stayed
    untouched. Entries already loaded as a side effect of loading the component
    are skipped.
    """
    connections_mock = AsyncMock(return_value=connections or [])

    with (
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_connections",
            new=connections_mock,
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_glucose_measurement",
            new=AsyncMock(return_value=MEASUREMENT),
        ),
    ):
        for entry in entries:
            if entry.state is ConfigEntryState.NOT_LOADED:
                await hass.config_entries.async_setup(entry.entry_id)
                await hass.async_block_till_done()

        await hass.async_block_till_done()

    return connections_mock


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
    assert device.name == "LibreLinkUp"


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

    connections_mock = await setup_entries(
        hass,
        entry,
        connections=[
            {"patientId": "someone-else", "firstName": "Someone", "lastName": "Else"},
            {"patientId": "patient-1", "firstName": "Test", "lastName": "User"},
        ],
    )

    connections_mock.assert_not_awaited()
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

    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.patient_id == "patient-1"

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED


# --- Availability during outages (H1) ----------------------------------------


async def _advance(hass, seconds=90):
    from homeassistant.util import dt as dt_util
    from pytest_homeassistant_custom_component.common import async_fire_time_changed

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def test_entity_stays_available_during_short_outage(hass) -> None:
    from custom_components.librelinkup.api import LibreLinkUpResponseError

    entry = make_entry(
        hass, unique_id="a", patient_id="patient-1", patient_name="Test User A"
    )
    await setup_entries(hass, entry)

    assert hass.states.get("sensor.test_user_a_glucose").state == "6.4"

    coordinator = hass.data[DOMAIN][entry.entry_id]

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_glucose_measurement",
        new=AsyncMock(side_effect=LibreLinkUpResponseError("malformed")),
    ):
        await _advance(hass)

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

    coordinator = hass.data[DOMAIN][entry.entry_id]

    # Age the last success past the grace period instead of waiting 15 minutes.
    coordinator._last_successful_update = (
        dt_util.utcnow() - STALE_FAILURE_GRACE_PERIOD - timedelta(minutes=1)
    )

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_glucose_measurement",
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
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_glucose_measurement",
        new=AsyncMock(return_value=MEASUREMENT),
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

    coordinator = hass.data[DOMAIN][entry.entry_id]

    assert coordinator.update_interval == timedelta(seconds=60)

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_glucose_measurement",
        new=AsyncMock(side_effect=error),
    ):
        await _advance(hass)

    assert coordinator.update_interval == timedelta(seconds=300)
    assert not hass.config_entries.flow.async_progress()
    assert hass.states.get("sensor.test_user_a_glucose").state == "6.4"
