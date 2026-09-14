from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.librelinkup.const import (
    CONF_PATIENT_ID,
    CONF_PATIENT_NAME,
    DOMAIN,
)


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
