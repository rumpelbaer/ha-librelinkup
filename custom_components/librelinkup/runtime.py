"""What the config entries of one LibreLinkUp account share.

A LibreLinkUp account can be configured once per shared person, and those
entries are not independent: they log in as the same account, poll the same
endpoint and hit the same rate limit. So they share one API client and one
coordinator, held here and keyed by the normalized e-mail.

The repair helpers live here too, because a repair issue is the account
runtime's way of telling the user about something only it can see -- a share
that disappeared, an account state a password cannot fix.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir

from .api import LibreLinkUpApi
from .const import (
    DOMAIN,
    ISSUE_ACCOUNT_STATE,
    ISSUE_LEGACY_ENTRY,
    ISSUE_SHARE_REVOKED,
)
from .coordinator import LibreLinkUpAccountCoordinator

ISSUE_KINDS = (ISSUE_LEGACY_ENTRY, ISSUE_SHARE_REVOKED, ISSUE_ACCOUNT_STATE)


@dataclass
class AccountRuntime:
    """Everything shared by the config entries of one LibreLinkUp account."""

    api: LibreLinkUpApi
    coordinator: LibreLinkUpAccountCoordinator
    password: str
    # entry_id -> patient_id of every loaded entry of this account. The patient
    # IDs are what the coordinator is allowed to keep data for.
    entries: dict[str, str] = field(default_factory=dict)
    # Entries of one account are set up concurrently; the lock keeps them from
    # each firing their own initial poll.
    setup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def patient_ids(self) -> frozenset[str]:
        return frozenset(self.entries.values())


def account_key(source: ConfigEntry | str) -> str:
    """Runtime-only key for an account.

    The normalized e-mail, never the password. This lives in hass.data and is
    never persisted or logged.

    Takes an entry or a bare e-mail, because the config flow has to ask "same
    account?" about an entry that does not exist yet -- while adding a person,
    the address is all there is. Both forms normalize here, so there is one
    definition of when two config entries belong to the same LibreLinkUp
    account.
    """
    email = source if isinstance(source, str) else source.data[CONF_EMAIL]

    return email.strip().lower()


def _issue_id(kind: str, entry_id: str) -> str:
    """Repair issue ID for one config entry.

    Keyed by the config entry ID, never by a patient ID: repair issues are
    persisted in .storage and must not carry health identifiers.
    """
    return f"{kind}_{entry_id}"


@callback
def async_create_issue(hass: HomeAssistant, entry: ConfigEntry, kind: str) -> None:
    """Raise a repair issue for one config entry.

    The entry title is the only placeholder: it is a Home Assistant reference the
    user recognizes, unlike a patient ID, which must never be persisted here.
    """
    ir.async_create_issue(
        hass,
        DOMAIN,
        _issue_id(kind, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=kind,
        translation_placeholders={"entry_title": entry.title},
    )


@callback
def async_delete_issue(hass: HomeAssistant, entry_id: str, kind: str) -> None:
    ir.async_delete_issue(hass, DOMAIN, _issue_id(kind, entry_id))
