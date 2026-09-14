"""Diagnostics for LibreLinkUp.

Deliberately built from an allowlist rather than by redacting a dump. A
diagnostics file is meant to be attached to a public issue report, so nothing
that identifies the account or the person, and nothing that reconstructs a
glucose history, may appear in it: no e-mail, no password, no token, no account
ID, no patient ID, no patient name, no glucose value and no measurement
timestamp -- not even hashed, because a hash stays linkable.

What is left is the shape of the problem: which region the account talks to, how
often it polls, whether the last poll worked and how long ago that was, what kind
of failure is ongoing, and how many entries and patients are involved.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import AccountRuntime, account_key
from .const import DOMAIN
from .coordinator import LibreLinkUpAccountCoordinator


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    integration = await _async_integration_version(hass)
    accounts: dict[str, AccountRuntime] = hass.data.get(DOMAIN, {})
    runtime = accounts.get(account_key(entry))

    if runtime is None:
        return {
            "integration_version": integration,
            "account_loaded": False,
        }

    coordinator: LibreLinkUpAccountCoordinator = runtime.coordinator
    update_interval = coordinator.update_interval

    return {
        "integration_version": integration,
        "account_loaded": True,
        # The region key and the API host are configuration, not identity.
        "region": runtime.api.region,
        "api_host": runtime.api.base_url,
        "token_present": runtime.api.has_token,
        "polling_enabled": update_interval is not None,
        "update_interval_seconds": (
            update_interval.total_seconds() if update_interval is not None else None
        ),
        "last_update_success": coordinator.last_update_success,
        # Relative, so it cannot be turned back into a point in time at which a
        # measurement existed.
        "seconds_since_last_success": coordinator.seconds_since_last_success,
        "failure_label": coordinator.failure_label,
        "configured_entries": len(runtime.entries),
        "configured_patients": coordinator.configured_patient_count,
        "patients_with_data": len(coordinator.data or {}),
    }


async def _async_integration_version(hass: HomeAssistant) -> str | None:
    """Version from the manifest, without reading the file directly."""
    from homeassistant.loader import async_get_integration  # noqa: PLC0415

    try:
        integration = await async_get_integration(hass, DOMAIN)
    except Exception:  # pragma: no cover - the integration is loaded by then
        return None

    return str(integration.version) if integration.version else None
