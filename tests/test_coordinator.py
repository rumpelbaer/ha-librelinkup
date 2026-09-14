from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.librelinkup.coordinator import LibreLinkUpCoordinator


@pytest.mark.asyncio
async def test_keeps_last_measurement_on_temporary_failure() -> None:
    hass = MagicMock()
    entry = MagicMock()
    entry.data = {
        "email": "test@example.com",
        "password": "secret",
        "patient_id": "patient-1",
    }

    coordinator = LibreLinkUpCoordinator.__new__(LibreLinkUpCoordinator)
    coordinator.patient_id = "patient-1"
    coordinator.data = {
        "Value": 6.4,
        "ValueInMgPerDl": 115,
        "FactoryTimestamp": "9/14/2026 11:12:35 AM",
    }
    coordinator.api = MagicMock()
    coordinator.api.async_get_glucose_measurement = AsyncMock(
        side_effect=RuntimeError("temporary failure")
    )

    result = await coordinator._async_update_data()

    assert result == coordinator.data
