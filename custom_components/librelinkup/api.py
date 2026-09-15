from __future__ import annotations

import asyncio
import hashlib

from aiohttp import (
    ClientResponse,
    ClientResponseError,
    ClientSession,
    ClientTimeout,
    ContentTypeError,
)

from .const import LIBRELINKUP_APP_VERSION
from .utils import mg_dl_to_mmol_l, parse_libre_timestamp


DEFAULT_BASE_URL = "https://api.libreview.io"

REGION_URLS = {
    "us": "https://api.libreview.io",
    "eu": "https://api-eu.libreview.io",
    "eu2": "https://api-eu2.libreview.io",
    "de": "https://api-de.libreview.io",
    "fr": "https://api-fr.libreview.io",
    "jp": "https://api-jp.libreview.io",
    "ap": "https://api-ap.libreview.io",
    "au": "https://api-au.libreview.io",
    "ae": "https://api-ae.libreview.io",
    "ca": "https://api-ca.libreview.io",
}

HEADERS = {
    "product": "llu.android",
    "version": LIBRELINKUP_APP_VERSION,
    "Accept": "application/json",
    "Content-Type": "application/json",
}

# One total budget per request, covering connect, response and body decoding.
REQUEST_TIMEOUT = ClientTimeout(total=20)

CONNECTIONS_PATH = "/llu/connections"

# Status 0 means the login succeeded. Status 2 is the status LibreLinkUp answers
# with when the e-mail/password pair itself is rejected, and it is the only
# status that may send the user into a re-authentication dialog. Both were
# verified against the live API: a correct password answers HTTP 200 with status
# 0, a wrong one HTTP 200 with status 2.
#
# Every other non-zero status describes an account or client state that a new
# password cannot fix -- the best known example is the account having to accept
# new terms of use inside the LibreLinkUp app, which keeps failing no matter how
# often the correct password is typed. Should this mapping ever be wrong, the
# worst case is a missing re-authentication prompt plus a repair issue that
# names the status, never an unresolvable reauth loop.
LOGIN_STATUS_SUCCESS = 0
LOGIN_STATUS_INVALID_CREDENTIALS = 2


class LibreLinkUpAuthenticationError(Exception):
    """The credentials themselves were rejected.

    The only error that may trigger a Home Assistant re-authentication flow.
    """


class LibreLinkUpAccountStateError(Exception):
    """The login failed for a reason a new password cannot fix.

    Terms of use waiting to be accepted in the LibreLinkUp app, a blocked or
    rate-limited client, an account state change -- anything where asking the
    user for their password again would only produce a dialog they cannot
    satisfy. Deliberately not a LibreLinkUpAuthenticationError.
    """


class LibreLinkUpRegionError(Exception):
    """The account lives in a region this integration does not know.

    Permanent: retrying cannot make an unknown region appear in the allowlist.
    """


class LibreLinkUpAuthorizationError(Exception):
    """A data request kept being rejected even after a successful login.

    Deliberately not a LibreLinkUpAuthenticationError: the credentials were
    accepted, so this is a transient server-side condition and must not make
    Home Assistant ask the user to re-enter their password.
    """


class LibreLinkUpResponseError(Exception):
    """The API replied with something that cannot be interpreted safely.

    Messages are deliberately static: the raw payload, the token, the account
    ID and the patient ID must never reach a log file or an issue report.
    """


# Fields a glucoseMeasurement must carry to be usable. TrendArrow is
# deliberately absent: the trend sensor degrades to "unknown" on its own, and
# discarding an otherwise valid glucose reading over a missing arrow would be
# the worse failure. isLow/isHigh are absent for the same reason.
REQUIRED_MEASUREMENT_FIELDS = (
    "ValueInMgPerDl",
    "FactoryTimestamp",
)


def _is_usable_measurement(measurement: object) -> bool:
    """Whether a glucoseMeasurement can be served as a reading.

    Answers instead of raising: /llu/connections carries every shared patient
    at once, so one unusable reading must not invalidate the readings of
    everybody else on the account.
    """
    if not isinstance(measurement, dict) or not measurement:
        return False

    for field in REQUIRED_MEASUREMENT_FIELDS:
        value = measurement.get(field)

        # "is None" rather than a falsy check: a glucose value of 0 would be a
        # legitimate reading. A list or a dict where a scalar belongs is a
        # changed payload shape, not a value.
        if value is None or isinstance(value, (list, dict)):
            return False

    if mg_dl_to_mmol_l(measurement.get("ValueInMgPerDl")) is None:
        return False

    # Freshness is derived from FactoryTimestamp, and a reading whose age
    # cannot be established must never be served as a current value.
    return parse_libre_timestamp(measurement.get("FactoryTimestamp")) is not None


def _patient_id(connection: object) -> str | None:
    """Return a usable patient ID from a connection, or None."""
    if not isinstance(connection, dict):
        return None

    patient_id = connection.get("patientId")

    if not isinstance(patient_id, str) or not patient_id.strip():
        return None

    return patient_id


async def _async_read_json(response: ClientResponse) -> object:
    """Decode a JSON body, translating decoding failures into safe errors.

    Both branches use "from None" on purpose. ContentTypeError is a
    ClientResponseError whose repr() contains the Authorization header, so
    chaining it would leak the bearer token into any logged traceback.
    """
    try:
        return await response.json()
    except ContentTypeError:
        raise LibreLinkUpResponseError(
            "LibreLinkUp API returned an unexpected content type"
        ) from None
    except ValueError:
        raise LibreLinkUpResponseError(
            "LibreLinkUp API returned invalid JSON"
        ) from None


def _require_dict(result: object) -> dict:
    """Reject anything that is not a JSON object (including an empty body)."""
    if not isinstance(result, dict):
        raise LibreLinkUpResponseError(
            "LibreLinkUp API returned malformed data"
        )

    return result


def _validate_envelope(result: object) -> dict:
    """Validate the response envelope of the authenticated data endpoints.

    Login is deliberately not routed through here: a non-success status there
    describes the account, not the payload.
    """
    result = _require_dict(result)

    status = result.get("status")

    if status is not None and status != LOGIN_STATUS_SUCCESS:
        raise LibreLinkUpResponseError(
            "LibreLinkUp API returned a non-success status"
        )

    return result


def _connection_list(result: dict) -> list:
    """Return the raw connection list of a /llu/connections response."""
    connections = result.get("data")

    if not isinstance(connections, list):
        raise LibreLinkUpResponseError(
            "LibreLinkUp API returned malformed data"
        )

    return connections


class LibreLinkUpApi:
    def __init__(self, session: ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._account_id: str | None = None
        self._base_url = DEFAULT_BASE_URL
        self._region: str | None = None
        # The client is shared by every config entry of one account, so two
        # requests can genuinely need a login at the same time.
        self._login_lock = asyncio.Lock()

    def update_password(self, password: str) -> None:
        """Adopt freshly re-authenticated credentials and drop the old token."""
        if password == self._password:
            return

        self._password = password
        self._token = None
        self._account_id = None

    @property
    def region(self) -> str | None:
        return self._region

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def has_token(self) -> bool:
        """Whether a login has produced credentials for data requests.

        Exposed for diagnostics, which may report that a token exists but never
        the token itself.
        """
        return bool(self._token and self._account_id)

    async def async_login(self, *, invalidate: str | None = None) -> None:
        """Log in, unless another request already did it while we waited.

        `invalidate` is the token the caller saw rejected. If the stored token
        has meanwhile been replaced by a different one, somebody else already
        recovered and no second login is sent to Abbott.
        """
        async with self._login_lock:
            if self._token and self._account_id and self._token != invalidate:
                return

            self._token = None
            self._account_id = None

            await self._async_login_locked()

    async def _async_login_locked(self) -> None:
        for _ in range(2):
            async with self._session.post(
                f"{self._base_url}/llu/auth/login",
                headers=HEADERS,
                json={"email": self._email, "password": self._password},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                if response.status == 401:
                    # The credential-bearing request was answered with
                    # "unauthenticated" -- the one HTTP status that really is a
                    # verdict on the password.
                    raise LibreLinkUpAuthenticationError(
                        "LibreLinkUp rejected the supplied credentials"
                    )

                if response.status == 403:
                    # "Forbidden" on a login is typically an edge or WAF block,
                    # or a client version Abbott no longer accepts. Asking the
                    # user for their password again would not help.
                    raise LibreLinkUpAccountStateError(
                        "LibreLinkUp refused the login request (HTTP 403)"
                    )

                response.raise_for_status()
                result = _require_dict(await _async_read_json(response))

            status = result.get("status")

            if status != LOGIN_STATUS_SUCCESS:
                if status == LOGIN_STATUS_INVALID_CREDENTIALS:
                    raise LibreLinkUpAuthenticationError(
                        "LibreLinkUp rejected the supplied credentials"
                    )

                raise LibreLinkUpAccountStateError(
                    f"LibreLinkUp login failed with status {status}"
                )

            data = result.get("data") or {}

            if data.get("redirect"):
                region = str(data.get("region") or "").lower()
                base_url = REGION_URLS.get(region)

                if not base_url:
                    raise LibreLinkUpRegionError(
                        f"Unsupported LibreLinkUp region: {region or 'unknown'}"
                    )

                self._region = region
                self._base_url = base_url
                continue

            user = data.get("user") or {}
            auth = data.get("authTicket") or {}

            user_id = user.get("id")
            token = auth.get("token")

            # Typed, not just truthy: a user ID that arrives as a number passes
            # a falsy check and then fails on .encode() with an AttributeError,
            # which is neither catchable as a response problem nor reportable
            # to the user as one. Everything else in this module type-checks
            # what it reads out of the payload; this is the same rule.
            if (
                not isinstance(user_id, str)
                or not isinstance(token, str)
                or not user_id
                or not token
            ):
                raise LibreLinkUpResponseError(
                    "LibreLinkUp login did not return user ID and token"
                )

            self._token = token
            self._account_id = hashlib.sha256(
                user_id.encode("utf-8")
            ).hexdigest()

            if self._region is None:
                country = str(user.get("country") or "").lower()
                if country in REGION_URLS:
                    self._region = country

            return

        raise LibreLinkUpRegionError(
            "LibreLinkUp region redirect could not be resolved"
        )

    def _auth_headers(self, token: str, account_id: str) -> dict[str, str]:
        """Headers for one request, built from a snapshot of the credentials.

        Taking token and account ID as arguments instead of reading them back
        from the instance keeps a concurrent re-login -- which briefly clears
        both -- from turning an in-flight request into a RuntimeError. A request
        sent with a token that has just been replaced simply gets a 401 and is
        retried below.
        """
        return {
            **HEADERS,
            "Authorization": f"Bearer {token}",
            "Account-Id": account_id,
        }

    async def _async_get_authenticated(self, path: str) -> dict:
        for attempt in range(2):
            if not self._token or not self._account_id:
                await self.async_login(invalidate=self._token)

            token_used = self._token
            account_id = self._account_id

            if not token_used or not account_id:  # pragma: no cover
                # async_login either sets both or raises; guards the invariant.
                raise LibreLinkUpResponseError(
                    "LibreLinkUp login did not return user ID and token"
                )

            try:
                async with self._session.get(
                    f"{self._base_url}{path}",
                    headers=self._auth_headers(token_used, account_id),
                    timeout=REQUEST_TIMEOUT,
                ) as response:
                    response.raise_for_status()
                    # Decoding happens inside the try, but _async_read_json
                    # converts its failures first, so a ContentTypeError can
                    # never be mistaken for an HTTP-level error below.
                    result = await _async_read_json(response)

            except ClientResponseError as err:
                if err.status not in (401, 403):
                    # 429 and 5xx keep their status for later backoff handling.
                    raise

                if attempt == 1:
                    # The login itself succeeded, so the credentials are fine.
                    # "from None" keeps the ClientResponseError -- whose repr
                    # carries the bearer token -- out of any traceback.
                    raise LibreLinkUpAuthorizationError(
                        "LibreLinkUp rejected the request after re-authentication"
                    ) from None

                # The rejected token is named so a concurrent request that has
                # already refreshed it does not trigger a second login.
                await self.async_login(invalidate=token_used)
                continue

            return _validate_envelope(result)

        raise RuntimeError(  # pragma: no cover
            "LibreLinkUp request failed after re-authentication"
        )

    async def async_get_connections(self) -> list[dict]:
        """Every shared connection, validated strictly.

        Used to let the user pick a person. Fail closed: a connection we cannot
        identify is never silently skipped here, because the user would then
        pick from an incomplete list.
        """
        connections = _connection_list(
            await self._async_get_authenticated(CONNECTIONS_PATH)
        )

        for connection in connections:
            if not isinstance(connection, dict):
                raise LibreLinkUpResponseError(
                    "LibreLinkUp API returned malformed data"
                )

            if _patient_id(connection) is None:
                raise LibreLinkUpResponseError(
                    "LibreLinkUp connection is missing a patient identifier"
                )

        return connections

    async def async_get_measurements(self) -> dict[str, dict | None]:
        """Current measurement of every shared patient, from one request.

        Uses /llu/connections, which already carries the current
        glucoseMeasurement per connection. The former /graph call returned the
        same reading plus roughly twelve hours of history that was discarded.

        Unlike async_get_connections this is deliberately tolerant: polling must
        not lose every configured patient because some unrelated connection --
        a pending invitation, a new field layout -- cannot be read. Connections
        without a usable patient ID are skipped; a patient whose reading is
        missing or unusable maps to None, so callers can tell "not shared any
        more" (absent key) apart from "shared but no usable reading right now"
        (None).
        """
        connections = _connection_list(
            await self._async_get_authenticated(CONNECTIONS_PATH)
        )

        measurements: dict[str, dict | None] = {}

        for connection in connections:
            patient_id = _patient_id(connection)

            if patient_id is None:
                continue

            measurement = connection.get("glucoseMeasurement")

            measurements[patient_id] = (
                measurement if _is_usable_measurement(measurement) else None
            )

        return measurements
