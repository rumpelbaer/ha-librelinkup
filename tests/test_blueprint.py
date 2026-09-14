"""Blueprint tests: the glucose light must classify correctly in either unit.

The blueprint is loaded through Home Assistant's own blueprint machinery, its
inputs are substituted, and the resulting automation is really set up. The
assertions are made on the light.turn_on calls it produces, so a template that
renders a string instead of a number, or one that mixes up units, fails here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from homeassistant.components.automation.config import (
    AUTOMATION_BLUEPRINT_SCHEMA,
    async_validate_config_item,
)
from homeassistant.components.blueprint import models
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.setup import async_setup_component
from homeassistant.util.yaml import parse_yaml
from unittest.mock import AsyncMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from helpers import measurement

from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)

BLUEPRINT_PATH = (
    Path(__file__).resolve().parents[1]
    / "blueprints"
    / "automation"
    / "rumpelbaer"
    / "glucose_light.yaml"
)

GLUCOSE = "sensor.person_glucose"
STALE = "binary_sensor.person_data_stale"
LIGHT = "light.glucose"

VERY_LOW = (255, 0, 0)
LOW = (255, 100, 0)
NORMAL = (0, 255, 0)
HIGH = (255, 200, 0)
VERY_HIGH = (180, 0, 255)
STALE_COLOR = (0, 80, 255)

BASE_INPUT = {
    "glucose_sensor": GLUCOSE,
    "stale_sensor": STALE,
    "target_light": {"entity_id": LIGHT},
}


def _blueprint() -> models.Blueprint:
    return models.Blueprint(
        parse_yaml(BLUEPRINT_PATH.read_text(encoding="utf-8")),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )


def _automation_config(**extra_input) -> dict:
    blueprint = _blueprint()
    inputs = models.BlueprintInputs(
        blueprint,
        {"use_blueprint": {"path": "glucose_light.yaml", "input": {**BASE_INPUT, **extra_input}}},
    )
    inputs.validate()
    config = inputs.async_substitute()
    # The blueprint carries no alias, and the tests address the automation by
    # entity ID.
    config["alias"] = "Glucose light"

    return config


async def _run(hass, *, state: str, unit: str | None, stale: str = "off", **extra_input):
    """Set the sensors, run the automation, and return the light calls."""
    attributes = {} if unit is None else {"unit_of_measurement": unit}
    hass.states.async_set(GLUCOSE, state, attributes)
    hass.states.async_set(STALE, stale)
    await hass.async_block_till_done()

    turn_on = async_mock_service(hass, "light", "turn_on")
    turn_off = async_mock_service(hass, "light", "turn_off")

    config = _automation_config(**extra_input)
    assert await async_setup_component(hass, "automation", {"automation": config})
    await hass.async_block_till_done()

    await hass.services.async_call(
        "automation",
        "trigger",
        {ATTR_ENTITY_ID: "automation.glucose_light", "skip_condition": False},
        blocking=True,
    )
    await hass.async_block_till_done()

    return turn_on, turn_off


def _color(calls) -> tuple:
    assert len(calls) == 1, f"expected exactly one light.turn_on, got {len(calls)}"
    return tuple(calls[0].data["rgb_color"])


async def test_blueprint_schema_is_valid(hass) -> None:
    """The file itself, plus the automation every input combination produces."""
    for extra in (
        {},
        {"activation_mode": "time_and_presence", "presence_entities": ["person.a"]},
        {"activation_mode": "presence"},
        {"activation_mode": "time_or_presence"},
        {"active_start_time": "20:00:00", "active_end_time": "07:00:00"},
        {"inactive_behavior": "leave_unchanged"},
    ):
        await async_validate_config_item(hass, "automation", _automation_config(**extra))


async def test_only_three_inputs_are_required() -> None:
    blueprint = _blueprint()
    required = {
        name
        for name, config in blueprint.inputs.items()
        if config is None or "default" not in config
    }

    assert required == {"glucose_sensor", "stale_sensor", "target_light"}


@pytest.mark.parametrize(
    ("state", "unit", "expected"),
    [
        # mmol/L, the unit the thresholds are written in.
        ("6.4", "mmol/L", NORMAL),
        ("2.9", "mmol/L", VERY_LOW),
        ("3.5", "mmol/L", LOW),
        ("12.0", "mmol/L", HIGH),
        ("15.0", "mmol/L", VERY_HIGH),
        # The same readings as mg/dL, which Home Assistant lets the user choose.
        # Before the conversion every one of these was classified "very high".
        ("115.2", "mg/dL", NORMAL),
        ("54", "mg/dL", VERY_LOW),
        ("65", "mg/dL", LOW),
        ("216", "mg/dL", HIGH),
        ("270", "mg/dL", VERY_HIGH),
        # On the thresholds. The low thresholds are exclusive ("values below
        # this threshold"), the high ones inclusive ("values up to this
        # threshold"), so 3.9 mmol/L is still in range.
        ("3.9", "mmol/L", NORMAL),
        ("3.89", "mmol/L", LOW),
        ("10.0", "mmol/L", NORMAL),
        ("13.9", "mmol/L", HIGH),
        ("13.91", "mmol/L", VERY_HIGH),
    ],
)
async def test_glucose_classification(hass, state, unit, expected) -> None:
    turn_on, _ = await _run(hass, state=state, unit=unit)

    assert _color(turn_on) == expected


@pytest.mark.parametrize(
    ("state", "unit"),
    [
        ("unavailable", "mmol/L"),
        ("unknown", "mmol/L"),
        ("", "mmol/L"),
        # An unexpected unit must never be classified as a glucose range.
        ("6.4", "g/L"),
        ("6.4", None),
    ],
)
async def test_unusable_reading_uses_the_stale_color(hass, state, unit) -> None:
    turn_on, _ = await _run(hass, state=state, unit=unit)

    assert _color(turn_on) == STALE_COLOR


async def test_stale_sensor_wins_over_a_valid_reading(hass) -> None:
    turn_on, _ = await _run(hass, state="6.4", unit="mmol/L", stale="on")

    assert _color(turn_on) == STALE_COLOR


async def test_identical_window_times_mean_all_day(hass) -> None:
    """A window from 07:00 to 07:00 is "always", not "never"."""
    turn_on, turn_off = await _run(
        hass,
        state="6.4",
        unit="mmol/L",
        activation_mode="time",
        active_start_time="07:00:00",
        active_end_time="07:00:00",
    )

    assert not turn_off
    assert _color(turn_on) == NORMAL


async def test_presence_mode_turns_the_light_off_when_nobody_is_home(hass) -> None:
    hass.states.async_set("person.a", "not_home")

    turn_on, turn_off = await _run(
        hass,
        state="6.4",
        unit="mmol/L",
        activation_mode="presence",
        presence_entities=["person.a"],
    )

    assert not turn_on
    assert len(turn_off) == 1


async def test_presence_mode_shows_the_color_when_somebody_is_home(hass) -> None:
    hass.states.async_set("person.a", "home")

    turn_on, _ = await _run(
        hass,
        state="6.4",
        unit="mmol/L",
        activation_mode="presence",
        presence_entities=["person.a"],
    )

    assert _color(turn_on) == NORMAL


async def test_leaving_the_light_unchanged_while_inactive(hass) -> None:
    hass.states.async_set("person.a", "not_home")

    turn_on, turn_off = await _run(
        hass,
        state="6.4",
        unit="mmol/L",
        activation_mode="presence",
        presence_entities=["person.a"],
        inactive_behavior="leave_unchanged",
    )

    assert not turn_on
    assert not turn_off


async def test_description_carries_the_disclaimers() -> None:
    description = _blueprint().metadata["description"]

    assert "Not a medical device" in description
    assert "medical decision" in description
    assert "visible to everyone in the room" in description


# --- The blueprint against the real integration entity ------------------------


@pytest.mark.parametrize(
    ("display_unit", "mg_dl", "expected"),
    [
        # Default: the entity reports mmol/L as a UnitOfBloodGlucoseConcentration
        # enum member, not as a plain string.
        (None, 115, NORMAL),
        (None, 46, VERY_LOW),
        (None, 270, VERY_HIGH),
        # After the user switches the display unit, the state is converted and
        # the attribute becomes "mg/dL". Every one of these was classified "very
        # high" before the blueprint learned to read the unit.
        ("mg/dL", 115, NORMAL),
        ("mg/dL", 46, VERY_LOW),
        ("mg/dL", 270, VERY_HIGH),
    ],
)
async def test_real_entity_is_classified_in_either_display_unit(
    hass, display_unit, mg_dl, expected
) -> None:
    """Drives the blueprint with an entity the integration really produced.

    The cases above use the same mg/dL readings in both display units, and away
    from the threshold boundaries: rounding the mmol/L state to one decimal can
    move a reading that sits exactly on a threshold into the neighbouring colour.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_PATIENT_ID: "patient-1",
            CONF_PATIENT_NAME: "Person",
        },
        unique_id="uid",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.coordinator.LibreLinkUpApi.async_get_measurements",
            new=AsyncMock(return_value={"patient-1": measurement(value=mg_dl)}),
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        if display_unit is not None:
            er.async_get(hass).async_update_entity_options(
                "sensor.person_glucose", "sensor", {"unit_of_measurement": display_unit}
            )
            await hass.async_block_till_done()

        turn_on = async_mock_service(hass, "light", "turn_on")
        config = _automation_config(
            glucose_sensor="sensor.person_glucose",
            stale_sensor="binary_sensor.person_data_stale",
        )

        assert await async_setup_component(hass, "automation", {"automation": config})
        await hass.async_block_till_done()

        await hass.services.async_call(
            "automation",
            "trigger",
            {
                ATTR_ENTITY_ID: "automation.glucose_light",
                "skip_condition": False,
            },
            blocking=True,
        )
        await hass.async_block_till_done()

        assert _color(turn_on) == expected
