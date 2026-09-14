"""Data minimisation: only configured people, and only what is needed.

A LibreLinkUp account can share more people than Home Assistant is configured
for. Those people never agreed to anything here, so their glucose values must not
be stored, logged, exposed as attributes or carried into diagnostics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from helpers import measurement

from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
    ISSUE_ACCOUNT_STATE,
    ISSUE_LEGACY_ENTRY,
    ISSUE_SHARE_REVOKED,
)
from custom_components.librelinkup.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.librelinkup.runtime import account_key

EMAIL = "user@example.com"
PASSWORD = "super-secret-password"

# What the account shares; only "mine" has a config entry.
STRANGER_VALUES = {"stranger-1": 250, "stranger-2": 42}


def make_entry(hass, key="mine", *, patient_id=None, name=None, **kwargs):
    data = {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_PATIENT_ID: patient_id if patient_id is not None else key,
    }

    if name is not None:
        data[CONF_PATIENT_NAME] = name

    entry = MockConfigEntry(
        domain=DOMAIN,
        title=f"LibreLinkUp - {name or key}",
        data=data,
        unique_id=f"uid-{key}",
        **kwargs,
    )
    entry.add_to_hass(hass)
    return entry


def account_snapshot(*, mine=115, strangers=True, include_mine=True):
    snapshot = {}

    if include_mine:
        snapshot["mine"] = measurement(value=mine)

    if strangers:
        for patient_id, value in STRANGER_VALUES.items():
            snapshot[patient_id] = measurement(value=value)

    return snapshot


def account_patch(measurements):
    return (
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(return_value=measurements),
        ),
    )


async def setup_account(hass, *entries, measurements=None):
    if measurements is None:
        measurements = account_snapshot()

    login, poll = account_patch(measurements)

    with login, poll as poll_mock:
        for entry in entries:
            if entry.state is ConfigEntryState.NOT_LOADED:
                await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()

    return poll_mock


def runtime_of(hass, entry):
    return hass.data[DOMAIN][account_key(entry)]


# --- H2: people without a config entry stay out of the runtime ----------------


async def test_unconfigured_people_never_enter_the_snapshot(hass, caplog) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    coordinator = entry.runtime_data

    assert set(coordinator.data) == {"mine"}
    assert set(coordinator._measured_at) == {"mine"}
    assert coordinator.configured_patient_count == 1

    for patient_id in STRANGER_VALUES:
        assert coordinator.measurement_for(patient_id) is None
        assert coordinator.is_patient_available(patient_id) is False
        assert patient_id not in caplog.text

    # No glucose value of an unconfigured person anywhere in the runtime.
    #
    # Field by field, never against str(coordinator.data): a reading carries
    # its timestamps as well, so stranger-2's value of 42 matched every reading
    # taken in second :42 and failed this test roughly once in 35 runs without
    # a single stranger being anywhere near the snapshot.
    #
    # Both glucose fields are checked, built through the same helper the
    # strangers' own readings come from, so a changed payload shape cannot
    # quietly narrow what is being looked for.
    stranger_glucose = {
        field: {measurement(value=value)[field] for value in STRANGER_VALUES.values()}
        for field in ("ValueInMgPerDl", "Value")
    }

    for patient_id, reading in coordinator.data.items():
        for field, forbidden in stranger_glucose.items():
            assert reading[field] not in forbidden, (
                f"{patient_id}'s {field} is an unconfigured person's reading"
            )


async def test_a_stranger_without_a_reading_is_not_worth_a_warning(
    hass, caplog
) -> None:
    """Warning about people nobody configured is noise, and names them."""
    entry = make_entry(hass, name="Mine")
    measurements = account_snapshot()
    measurements["stranger-1"] = None
    measurements["stranger-2"] = None

    await setup_account(hass, entry, measurements=measurements)

    assert "no usable measurement" not in caplog.text
    assert "no longer shared" not in caplog.text


async def test_only_configured_people_get_entities(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    glucose_entities = [
        entity_id
        for entity_id in hass.states.async_entity_ids("sensor")
        if entity_id.endswith("_glucose")
    ]

    assert glucose_entities == ["sensor.mine_glucose"]
    assert hass.states.get("sensor.mine_glucose").state == "6.4"


# --- M9 + L4: a share that disappears ----------------------------------------


async def test_a_revoked_share_is_reported_and_cleaned_up(hass, caplog) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    coordinator = entry.runtime_data
    issues = ir.async_get(hass)
    issue_id = f"{ISSUE_SHARE_REVOKED}_{entry.entry_id}"

    assert issues.async_get_issue(DOMAIN, issue_id) is None

    # The person stops sharing: their connection is simply gone.
    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=account_snapshot(include_mine=False)),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert coordinator.last_update_success is True
    # Warned once, and without naming the person or their ID.
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.levelname == "WARNING" and "no longer shared" in record.getMessage()
    ]

    assert len(warnings) == 1
    assert "mine" not in warnings[0]
    assert "Mine" not in warnings[0]

    issue = issues.async_get_issue(DOMAIN, issue_id)

    assert issue is not None
    assert issue.translation_key == ISSUE_SHARE_REVOKED
    # The issue is keyed and described by the entry, never by a patient ID.
    assert "mine" not in issue_id.replace(entry.entry_id, "")
    assert issue.translation_placeholders == {"entry_title": entry.title}


async def test_a_returning_share_resolves_the_issue(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    coordinator = entry.runtime_data
    issues = ir.async_get(hass)
    issue_id = f"{ISSUE_SHARE_REVOKED}_{entry.entry_id}"

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=account_snapshot(include_mine=False)),
    ):
        await coordinator.async_refresh()

    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=account_snapshot()),
    ):
        await coordinator.async_refresh()

    assert issues.async_get_issue(DOMAIN, issue_id) is None


async def test_a_revoked_share_never_serves_somebody_elses_reading(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    coordinator = entry.runtime_data

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=account_snapshot(include_mine=False)),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    # The last own reading may still be served inside the age limit, but never a
    # reading that belongs to one of the other people on the account.
    served = coordinator.measurement_for("mine")

    assert served is None or served["ValueInMgPerDl"] == 115


async def test_removing_an_entry_removes_its_issues(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    coordinator = entry.runtime_data
    issues = ir.async_get(hass)
    issue_id = f"{ISSUE_SHARE_REVOKED}_{entry.entry_id}"

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=account_snapshot(include_mine=False)),
    ):
        await coordinator.async_refresh()

    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert issues.async_get_issue(DOMAIN, issue_id) is None


# --- Repairs: an entry from before patient IDs were stored --------------------


async def test_a_legacy_entry_raises_a_repair_issue(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LibreLinkUp",
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD},
        unique_id="legacy",
    )
    entry.add_to_hass(hass)

    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.SETUP_ERROR

    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"{ISSUE_LEGACY_ENTRY}_{entry.entry_id}"
    )

    assert issue is not None
    assert issue.translation_key == ISSUE_LEGACY_ENTRY


async def test_an_account_state_problem_raises_a_repair_issue(hass) -> None:
    from custom_components.librelinkup.api import LibreLinkUpAccountStateError

    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    coordinator = entry.runtime_data
    issues = ir.async_get(hass)
    issue_id = f"{ISSUE_ACCOUNT_STATE}_{entry.entry_id}"

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(side_effect=LibreLinkUpAccountStateError("terms of use")),
    ):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert issues.async_get_issue(DOMAIN, issue_id) is not None

    with patch(
        "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
        new=AsyncMock(return_value=account_snapshot()),
    ):
        await coordinator.async_refresh()

    assert issues.async_get_issue(DOMAIN, issue_id) is None


# --- M7: recorder footprint of the glucose entity -----------------------------


async def test_the_glucose_entity_publishes_only_the_raw_value(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    state = hass.states.get("sensor.mine_glucose")
    own = {
        key: value
        for key, value in state.attributes.items()
        if key
        not in (
            "state_class",
            "device_class",
            "unit_of_measurement",
            "friendly_name",
        )
    }

    # Everything else was either the state again or another entity's value, and
    # every attribute is written to the recorder with each new reading.
    assert own == {"glucose_mg_dl": 115}


async def test_the_reading_age_entity_is_disabled_by_default(hass) -> None:
    from homeassistant.helpers import entity_registry as er

    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    registry = er.async_get(hass)
    entity = registry.async_get("sensor.mine_reading_age")

    assert entity is not None
    assert entity.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get("sensor.mine_reading_age") is None


# --- Diagnostics --------------------------------------------------------------


FORBIDDEN_IN_DIAGNOSTICS = (
    EMAIL,
    PASSWORD,
    "mine",
    "Mine",
    "stranger-1",
    "stranger-2",
    "115",
    "250",
)


async def test_diagnostics_describe_the_problem_not_the_person(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    runtime = runtime_of(hass, entry)
    runtime.api._token = "a-token"
    runtime.api._account_id = "an-account-id"
    runtime.api._region = "de"

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["account_loaded"] is True
    assert diagnostics["region"] == "de"
    assert diagnostics["token_present"] is True
    assert diagnostics["last_update_success"] is True
    assert diagnostics["polling_enabled"] is True
    assert diagnostics["configured_entries"] == 1
    assert diagnostics["configured_patients"] == 1
    assert diagnostics["patients_with_data"] == 1
    assert diagnostics["seconds_since_last_success"] >= 0
    assert diagnostics["integration_version"] == "0.1.0"


async def test_diagnostics_contain_no_identifying_or_health_data(hass) -> None:
    entry = make_entry(hass, name="Mine")
    await setup_account(hass, entry)

    runtime = runtime_of(hass, entry)
    runtime.api._token = "super-secret-bearer-token"
    runtime.api._account_id = "hashed-account-id"

    rendered = repr(await async_get_config_entry_diagnostics(hass, entry))

    for secret in (
        "super-secret-bearer-token",
        "hashed-account-id",
        *FORBIDDEN_IN_DIAGNOSTICS,
    ):
        assert secret not in rendered, f"{secret} leaked into diagnostics"

    # Not even a hashed patient ID, and no measurement timestamp.
    assert "FactoryTimestamp" not in rendered
    assert "patient" not in rendered.replace("configured_patients", "").replace(
        "patients_with_data", ""
    )


async def test_diagnostics_of_an_unloaded_account(hass) -> None:
    entry = make_entry(hass, name="Mine")

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["account_loaded"] is False
    assert EMAIL not in repr(diagnostics)
