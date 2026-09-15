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

VERY_LOW = (205, 75, 65)
LOW = (235, 165, 45)
NORMAL = (255, 180, 110)
HIGH = (120, 140, 210)
VERY_HIGH = (95, 70, 160)
STALE_COLOR = (115, 110, 120)

# The default brightness of every range, in percent.
DEFAULT_BRIGHTNESS = {
    VERY_LOW: 75,
    LOW: 70,
    NORMAL: 60,
    HIGH: 70,
    VERY_HIGH: 75,
    STALE_COLOR: 45,
}

BRIGHTNESS_INPUT = {
    VERY_LOW: "very_low_brightness",
    LOW: "low_brightness",
    NORMAL: "normal_brightness",
    HIGH: "high_brightness",
    VERY_HIGH: "very_high_brightness",
    STALE_COLOR: "stale_brightness",
}

# Six values that are distinct, so a brightness wired to the wrong range shows
# up instead of hiding behind an identical default.
CUSTOM_BRIGHTNESS = {
    "very_low_brightness": 11,
    "low_brightness": 22,
    "normal_brightness": 33,
    "high_brightness": 44,
    "very_high_brightness": 55,
    "stale_brightness": 66,
}

# One reading per range, and the stale sensor for the stale colour.
READING_PER_RANGE = [
    ("2.9", "off", VERY_LOW),
    ("3.5", "off", LOW),
    ("6.4", "off", NORMAL),
    ("12.0", "off", HIGH),
    ("15.0", "off", VERY_HIGH),
    ("6.4", "on", STALE_COLOR),
]

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


def _data(calls) -> dict:
    assert len(calls) == 1, f"expected exactly one light.turn_on, got {len(calls)}"
    return calls[0].data


def _color(calls) -> tuple:
    data = _data(calls)
    # The two colour modes are mutually exclusive: light.turn_on never gets an
    # RGB colour and a colour temperature in the same call.
    assert "color_temp_kelvin" not in data
    return tuple(data["rgb_color"])


async def test_blueprint_schema_is_valid(hass) -> None:
    """The file itself, plus the automation every input combination produces."""
    for extra in (
        {},
        {"activation_mode": "time_and_presence", "presence_entities": ["person.a"]},
        {"activation_mode": "presence"},
        {"activation_mode": "time_or_presence"},
        {"active_start_time": "20:00:00", "active_end_time": "07:00:00"},
        {"inactive_behavior": "leave_unchanged"},
        {"normal_color_mode": "rgb"},
        {"normal_color_mode": "color_temp", "normal_color_temp_kelvin": 3000},
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
        # 2.9 mmol/L as Home Assistant displays it: 2.9 * 18. Was 54 before,
        # which is 3.0 mmol/L and therefore sits exactly on the very low
        # threshold -- it is covered as a threshold case below instead.
        ("52.2", "mg/dL", VERY_LOW),
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


# Every threshold, in both display units. Home Assistant converts a mmol/L blood
# glucose entity to mg/dL for display with a flat factor of 18, so the mg/dL
# column is exactly what a user with that display setting sees for the mmol/L
# reading next to it. The pair has to be classified identically: which unit the
# entity happens to be displayed in is not a fact about the person's glucose.
#
# These are the values that used to disagree. The blueprint divided by the
# 18.0182 of diabetes care instead of undoing Home Assistant's 18, so a reading
# sitting exactly on a threshold came back about 0.1 % low -- under the
# exclusive thresholds it was meant to be on, and one band more alarming.
@pytest.mark.parametrize(
    ("mmol", "mg_dl", "expected"),
    [
        # very low: exclusive, so exactly on it is already "low".
        ("3.0", "54", LOW),
        ("2.99", "53.82", VERY_LOW),
        # low: exclusive, so exactly on it is "normal".
        ("3.9", "70.2", NORMAL),
        ("3.89", "70.02", LOW),
        # high: inclusive, so exactly on it is still "normal".
        ("10.0", "180", NORMAL),
        ("10.01", "180.18", HIGH),
        # very high: inclusive, so exactly on it is still "high".
        ("13.9", "250.2", HIGH),
        ("13.91", "250.38", VERY_HIGH),
    ],
)
async def test_a_threshold_is_the_same_threshold_in_either_display_unit(
    hass, mmol, mg_dl, expected
) -> None:
    in_mmol, _ = await _run(hass, state=mmol, unit="mmol/L")
    assert _color(in_mmol) == expected

    in_mg_dl, _ = await _run(hass, state=mg_dl, unit="mg/dL")
    assert _color(in_mg_dl) == expected


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


# --- Palette and the display style of the normal range ------------------------


@pytest.mark.parametrize(("state", "stale", "expected"), READING_PER_RANGE)
async def test_the_defaults_are_the_intended_palette(hass, state, stale, expected) -> None:
    """What an installation that configures no colour at all ends up with."""
    turn_on, _ = await _run(hass, state=state, unit="mmol/L", stale=stale)

    assert _color(turn_on) == expected
    assert _data(turn_on)["brightness_pct"] == DEFAULT_BRIGHTNESS[expected]


@pytest.mark.parametrize(("state", "stale", "expected"), READING_PER_RANGE)
async def test_every_range_carries_its_own_brightness(hass, state, stale, expected) -> None:
    turn_on, _ = await _run(
        hass, state=state, unit="mmol/L", stale=stale, **CUSTOM_BRIGHTNESS
    )

    assert _color(turn_on) == expected
    assert _data(turn_on)["brightness_pct"] == CUSTOM_BRIGHTNESS[BRIGHTNESS_INPUT[expected]]


async def test_normal_is_rgb_without_a_display_style(hass) -> None:
    """The default keeps an RGB-only light working without any configuration."""
    turn_on, _ = await _run(hass, state="6.4", unit="mmol/L")

    data = _data(turn_on)
    assert tuple(data["rgb_color"]) == NORMAL
    assert "color_temp_kelvin" not in data
    assert data["brightness_pct"] == 60


async def test_normal_in_rgb_mode_ignores_the_color_temperature(hass) -> None:
    turn_on, _ = await _run(
        hass,
        state="6.4",
        unit="mmol/L",
        normal_color_mode="rgb",
        normal_color_temp_kelvin=2730,
    )

    data = _data(turn_on)
    assert tuple(data["rgb_color"]) == NORMAL
    assert "color_temp_kelvin" not in data
    assert data["brightness_pct"] == 60


@pytest.mark.parametrize("kelvin", [2730, 3000])
async def test_normal_in_color_temp_mode_sends_kelvin_and_no_rgb(hass, kelvin) -> None:
    turn_on, _ = await _run(
        hass,
        state="6.4",
        unit="mmol/L",
        normal_color_mode="color_temp",
        normal_color_temp_kelvin=kelvin,
    )

    data = _data(turn_on)
    assert data["color_temp_kelvin"] == kelvin
    assert "rgb_color" not in data
    assert data["brightness_pct"] == 60


async def test_color_temp_mode_defaults_to_warm_white(hass) -> None:
    turn_on, _ = await _run(
        hass, state="6.4", unit="mmol/L", normal_color_mode="color_temp"
    )

    assert _data(turn_on)["color_temp_kelvin"] == 2700


@pytest.mark.parametrize(
    ("state", "stale", "expected"),
    [case for case in READING_PER_RANGE if case[2] != NORMAL],
)
async def test_only_normal_follows_the_display_style(hass, state, stale, expected) -> None:
    """Warning ranges and the stale colour stay RGB in colour temperature mode."""
    turn_on, _ = await _run(
        hass,
        state=state,
        unit="mmol/L",
        stale=stale,
        normal_color_mode="color_temp",
        normal_color_temp_kelvin=2730,
    )

    # _color() already asserts that no colour temperature came along.
    assert _color(turn_on) == expected


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
