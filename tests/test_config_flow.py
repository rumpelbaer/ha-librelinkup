from unittest.mock import AsyncMock, patch

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

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
