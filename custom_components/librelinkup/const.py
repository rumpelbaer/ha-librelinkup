from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "librelinkup"

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]

CONF_PATIENT_ID = "patient_id"
CONF_PATIENT_NAME = "patient_name"

# Shown when LibreLinkUp reports a shared person without a name. Never the
# patient ID: it would end up in the config entry title, the device name and
# every screenshot of that person's dashboard.
DEFAULT_PATIENT_NAME = "LibreLinkUp Patient"

# Sent as the "version" header to mimic the LibreLinkUp Android client. Abbott
# has rejected outdated clients before, so this may need raising if logins
# suddenly start failing; it is deliberately not discovered at runtime.
LIBRELINKUP_APP_VERSION = "4.16.0"

# Repair issue kinds. The issue ID always ends in a config entry ID, never in a
# patient ID: repair issues are persisted in .storage and must not carry health
# identifiers.
ISSUE_LEGACY_ENTRY = "legacy_entry"
ISSUE_SHARE_REVOKED = "share_revoked"
ISSUE_ACCOUNT_STATE = "account_state"
