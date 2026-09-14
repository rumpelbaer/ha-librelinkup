import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.selector import TextSelector
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)


TRANSLATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "librelinkup"
    / "translations"
)


def field_validator(schema, key):
    """Return the validator a voluptuous schema uses for a given field."""
    for marker, validator in schema.schema.items():
        if marker == key:
            return validator

    raise AssertionError(f"{key} not found in schema")


async def test_single_connection_creates_entry(hass) -> None:
    connections = [
        {
            "patientId": "patient-1",
            "firstName": "Florian",
            "lastName": "Junker",
        }
    ]

    with (
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(return_value=connections),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={
                CONF_EMAIL: "test@example.com",
                CONF_PASSWORD: "secret",
            },
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LibreLinkUp - Florian Junker"
    assert result["data"][CONF_PATIENT_ID] == "patient-1"
    assert result["data"][CONF_PATIENT_NAME] == "Florian Junker"


async def test_multiple_connections_show_selection(hass) -> None:
    connections = [
        {
            "patientId": "patient-1",
            "firstName": "Florian",
            "lastName": "Junker",
        },
        {
            "patientId": "patient-2",
            "firstName": "Max",
            "lastName": "Mustermann",
        },
    ]

    with (
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(return_value=connections),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={
                CONF_EMAIL: "multi@example.com",
                CONF_PASSWORD: "secret",
            },
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={CONF_PATIENT_ID: "patient-2"},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "LibreLinkUp - Max Mustermann"
    assert result["data"][CONF_PATIENT_ID] == "patient-2"
    assert result["data"][CONF_PATIENT_NAME] == "Max Mustermann"

async def test_same_account_can_add_multiple_patients(hass) -> None:
    connections = [
        {
            "patientId": "patient-1",
            "firstName": "Florian",
            "lastName": "Junker",
        },
        {
            "patientId": "patient-2",
            "firstName": "Max",
            "lastName": "Mustermann",
        },
    ]

    async def start_flow():
        with (
            patch(
                "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
                new=AsyncMock(return_value=connections),
            ),
        ):
            return await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
                data={
                    CONF_EMAIL: "shared@example.com",
                    CONF_PASSWORD: "secret",
                },
            )

    first = await start_flow()
    first = await hass.config_entries.flow.async_configure(
        first["flow_id"],
        user_input={CONF_PATIENT_ID: "patient-1"},
    )

    second = await start_flow()
    second = await hass.config_entries.flow.async_configure(
        second["flow_id"],
        user_input={CONF_PATIENT_ID: "patient-2"},
    )

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.CREATE_ENTRY
    assert first["title"] == "LibreLinkUp - Florian Junker"
    assert second["title"] == "LibreLinkUp - Max Mustermann"
    assert first["result"].unique_id != second["result"].unique_id

async def test_reauth_updates_password(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LibreLinkUp - Test User",
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "old-password",
            CONF_PATIENT_ID: "patient-1",
            CONF_PATIENT_NAME: "Test User",
        },
        unique_id="unique-id",
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
            new=AsyncMock(),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(
                return_value=[
                    {
                        "patientId": "patient-1",
                        "firstName": "Test",
                        "lastName": "User",
                    }
                ]
            ),
        ),
        patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_PASSWORD: "new-password"},
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-password"


async def test_user_step_masks_password(hass) -> None:
    """The password must be rendered as a masked field, not as plain text."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    password = field_validator(result["data_schema"], CONF_PASSWORD)

    assert isinstance(password, TextSelector)
    assert password.config["type"] == "password"
    assert password.config["autocomplete"] == "current-password"

    email = field_validator(result["data_schema"], CONF_EMAIL)

    assert isinstance(email, TextSelector)
    assert email.config["type"] == "email"


async def test_reauth_step_masks_password(hass) -> None:
    """The reauth dialog must mask the password as well."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="LibreLinkUp - Test User",
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "old-password",
            CONF_PATIENT_ID: "patient-1",
            CONF_PATIENT_NAME: "Test User",
        },
        unique_id="unique-id",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_REAUTH,
            "entry_id": entry.entry_id,
        },
        data=dict(entry.data),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    password = field_validator(result["data_schema"], CONF_PASSWORD)

    assert isinstance(password, TextSelector)
    assert password.config["type"] == "password"
    assert password.config["autocomplete"] == "current-password"


async def test_duplicate_patient_aborts_as_already_configured(hass) -> None:
    """The same account plus the same patient must not be added twice."""
    connections = [
        {
            "patientId": "patient-1",
            "firstName": "Test",
            "lastName": "User",
        }
    ]

    async def start_flow(email: str):
        with (
            patch(
                "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
                new=AsyncMock(),
            ),
            patch(
                "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
                new=AsyncMock(return_value=connections),
            ),
        ):
            return await hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": config_entries.SOURCE_USER},
                data={
                    CONF_EMAIL: email,
                    CONF_PASSWORD: "secret",
                },
            )

    first = await start_flow("test@example.com")

    assert first["type"] is FlowResultType.CREATE_ENTRY

    # Same account and patient, but entered with different casing and padding.
    second = await start_flow("  Test@Example.COM  ")

    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("language", ["en", "de"])
def test_translation_structure(language) -> None:
    """Config flow strings must live under "config" so Home Assistant finds them."""
    translations = json.loads(
        (TRANSLATIONS_DIR / f"{language}.json").read_text(encoding="utf-8")
    )

    assert set(translations) == {"config", "issues"}

    config = translations["config"]

    assert set(config["step"]) == {"user", "connection", "reauth_confirm"}
    assert set(config["error"]) == {
        "invalid_auth",
        "cannot_connect",
        "no_connections",
        "share_revoked",
        "unsupported_region",
        "account_state",
        "invalid_response",
        "unknown",
    }
    assert set(config["abort"]) == {"reauth_successful", "already_configured"}

    # Repair issues need their own strings; without them the user would see a
    # raw translation key in the repairs panel.
    assert set(translations["issues"]) == {
        "share_revoked",
        "account_state",
        "legacy_entry",
    }

    for issue in translations["issues"].values():
        assert issue["title"]
        assert "{entry_title}" in issue["description"]

    reauth = config["step"]["reauth_confirm"]

    assert reauth["title"]
    assert reauth["description"]
    assert reauth["data"][CONF_PASSWORD]


def test_translations_have_matching_keys() -> None:
    """Both languages must cover exactly the same keys."""

    def keys(value, prefix=""):
        if not isinstance(value, dict):
            return {prefix}
        return {k for key, v in value.items() for k in keys(v, f"{prefix}.{key}")}

    english = json.loads((TRANSLATIONS_DIR / "en.json").read_text(encoding="utf-8"))
    german = json.loads((TRANSLATIONS_DIR / "de.json").read_text(encoding="utf-8"))

    assert keys(english) == keys(german)


# --- Error mapping: every failure gets its own message -----------------------


def flow_patch(*, login_error=None, connections=None, connections_error=None):
    """Patch the flow's client with one specific failure."""
    return (
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_login",
            new=AsyncMock(side_effect=login_error),
        ),
        patch(
            "custom_components.librelinkup.config_flow.LibreLinkUpApi.async_get_connections",
            new=AsyncMock(
                return_value=connections or [], side_effect=connections_error
            ),
        ),
    )


async def start_user_flow(hass, **patches):
    login, connections = flow_patch(**patches)

    with login, connections:
        return await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
        )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("LibreLinkUpAuthenticationError", "invalid_auth"),
        ("LibreLinkUpRegionError", "unsupported_region"),
        ("LibreLinkUpAccountStateError", "account_state"),
        ("LibreLinkUpResponseError", "invalid_response"),
        ("LibreLinkUpAuthorizationError", "cannot_connect"),
    ],
)
async def test_each_failure_has_its_own_message(hass, error, expected) -> None:
    """"Could not connect" for an unsupported region sends the user nowhere."""
    from custom_components.librelinkup import api as api_module

    result = await start_user_flow(
        hass, login_error=getattr(api_module, error)("nope")
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_network_failures_still_say_cannot_connect(hass) -> None:
    from aiohttp import ClientConnectionError

    result = await start_user_flow(hass, login_error=ClientConnectionError("down"))

    assert result["errors"] == {"base": "cannot_connect"}

    result = await start_user_flow(hass, login_error=TimeoutError())

    assert result["errors"] == {"base": "cannot_connect"}


async def test_an_unexpected_error_is_logged_without_secrets(hass, caplog) -> None:
    result = await start_user_flow(hass, login_error=ValueError("boom"))

    assert result["errors"] == {"base": "unknown"}
    # Logged so it can be debugged at all...
    assert "Unexpected error while talking to LibreLinkUp" in caplog.text
    # ...but the message itself carries nothing about the account.
    assert "test@example.com" not in caplog.text
    assert "secret" not in caplog.text


async def test_reauth_reports_a_revoked_share_as_such(hass) -> None:
    """Not "no connections found": the account has them, just not this person."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "secret",
            CONF_PATIENT_ID: "patient-1",
            CONF_PATIENT_NAME: "Test User",
        },
        unique_id="a",
    )
    entry.add_to_hass(hass)

    login, connections = flow_patch(connections=[{"patientId": "somebody-else"}])

    with login, connections:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_REAUTH,
                "entry_id": entry.entry_id,
            },
            data=dict(entry.data),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={CONF_PASSWORD: "new-password"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "share_revoked"}
    # And the password of an entry whose share is gone is left alone.
    assert entry.data[CONF_PASSWORD] == "secret"


# --- M6: adding a person also carries the verified password to its siblings ---


async def test_adding_a_person_propagates_the_new_password(hass) -> None:
    """Otherwise the account runs on whichever password loads first."""
    existing = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "test@example.com",
            CONF_PASSWORD: "old-password",
            CONF_PATIENT_ID: "patient-1",
            CONF_PATIENT_NAME: "Test User A",
        },
        unique_id="a",
    )
    existing.add_to_hass(hass)

    login, connections = flow_patch(
        connections=[
            {"patientId": "patient-2", "firstName": "Test", "lastName": "User B"}
        ]
    )

    with login, connections:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "new-password"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PASSWORD] == "new-password"
    # The stored password of the existing entry was verified moments ago, so it
    # is the one both entries must carry.
    assert existing.data[CONF_PASSWORD] == "new-password"


async def test_another_account_keeps_its_own_password(hass) -> None:
    other_account = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "somebody@example.com",
            CONF_PASSWORD: "their-password",
            CONF_PATIENT_ID: "patient-9",
            CONF_PATIENT_NAME: "Somebody",
        },
        unique_id="z",
    )
    other_account.add_to_hass(hass)

    login, connections = flow_patch(connections=[{"patientId": "patient-1"}])

    with login, connections:
        await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "new-password"},
        )

    assert other_account.data[CONF_PASSWORD] == "their-password"


# --- L6: a patient ID must never become a visible name -----------------------


@pytest.mark.parametrize(
    "connection",
    [
        {"patientId": "3f2b1c8e-0000-4444-aaaa-secret-uuid"},
        {"patientId": "3f2b1c8e-0000-4444-aaaa-secret-uuid", "firstName": ""},
        {
            "patientId": "3f2b1c8e-0000-4444-aaaa-secret-uuid",
            "firstName": None,
            "lastName": None,
        },
    ],
)
async def test_a_nameless_person_gets_a_generic_name(hass, connection) -> None:
    login, connections = flow_patch(connections=[connection])

    with login, connections:
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={"source": config_entries.SOURCE_USER},
            data={CONF_EMAIL: "test@example.com", CONF_PASSWORD: "secret"},
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    # The ID is still what identifies the person internally...
    assert result["data"][CONF_PATIENT_ID] == connection["patientId"]
    # ...but it must not show up in the title or the stored display name.
    assert connection["patientId"] not in result["title"]
    assert result["title"] == "LibreLinkUp - LibreLinkUp Patient"
    assert result["data"][CONF_PATIENT_NAME] == "LibreLinkUp Patient"
