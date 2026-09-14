from __future__ import annotations

import hashlib

from aiohttp import ClientResponse, ClientResponseError, ClientSession, ContentTypeError


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
    "version": "4.16.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class LibreLinkUpAuthenticationError(Exception):
    pass


class LibreLinkUpRegionError(Exception):
    pass


class LibreLinkUpResponseError(Exception):
    """The API replied with something that cannot be interpreted safely.

    Messages are deliberately static: the raw payload, the token, the account
    ID and the patient ID must never reach a log file or an issue report.
    """


class LibreLinkUpMeasurementError(LibreLinkUpResponseError):
    """The API replied successfully but carried no usable measurement."""


# Fields the integration actually consumes. isLow/isHigh are intentionally not
# required: the binary sensors coerce them with bool(), so a missing flag
# degrades to "off" instead of discarding an otherwise valid glucose reading.
REQUIRED_MEASUREMENT_FIELDS = (
    "ValueInMgPerDl",
    "FactoryTimestamp",
    "TrendArrow",
)


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
    means the credentials were rejected, which stays an authentication error.
    """
    result = _require_dict(result)

    status = result.get("status")

    if status is not None and status != 0:
        raise LibreLinkUpResponseError(
            "LibreLinkUp API returned a non-success status"
        )

    return result


class LibreLinkUpApi:
    def __init__(self, session: ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._account_id: str | None = None
        self._base_url = DEFAULT_BASE_URL
        self._region: str | None = None

    @property
    def region(self) -> str | None:
        return self._region

    @property
    def base_url(self) -> str:
        return self._base_url

    async def async_login(self) -> None:
        for _ in range(2):
            async with self._session.post(
                f"{self._base_url}/llu/auth/login",
                headers=HEADERS,
                json={"email": self._email, "password": self._password},
                timeout=20,
            ) as response:
                if response.status in (401, 403):
                    raise LibreLinkUpAuthenticationError(
                        "LibreLinkUp rejected the supplied credentials"
                    )

                response.raise_for_status()
                result = _require_dict(await _async_read_json(response))

            if result.get("status") != 0:
                raise LibreLinkUpAuthenticationError(
                    f"LibreLinkUp login failed with status {result.get('status')}"
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

            if not user_id or not token:
                raise LibreLinkUpAuthenticationError(
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

    def _auth_headers(self) -> dict[str, str]:
        if not self._token or not self._account_id:
            raise RuntimeError("LibreLinkUp client is not authenticated")

        return {
            **HEADERS,
            "Authorization": f"Bearer {self._token}",
            "Account-Id": self._account_id,
        }

    async def _async_get_authenticated(self, path: str) -> dict:
        if not self._token or not self._account_id:
            await self.async_login()

        for attempt in range(2):
            try:
                async with self._session.get(
                    f"{self._base_url}{path}",
                    headers=self._auth_headers(),
                    timeout=20,
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
                    raise LibreLinkUpAuthenticationError(
                        "LibreLinkUp authentication failed after re-authentication"
                    ) from err

                await self.async_login()
                continue

            return _validate_envelope(result)

        raise RuntimeError(
            "LibreLinkUp request failed after re-authentication"
        )

    async def async_get_connections(self) -> list[dict]:
        result = await self._async_get_authenticated(
            "/llu/connections"
        )

        connections = result.get("data")

        if not isinstance(connections, list):
            raise LibreLinkUpResponseError(
                "LibreLinkUp API returned malformed data"
            )

        # Fail closed: a connection we cannot identify is never silently
        # skipped, because the user would then pick from an incomplete list.
        for connection in connections:
            if not isinstance(connection, dict):
                raise LibreLinkUpResponseError(
                    "LibreLinkUp API returned malformed data"
                )

            patient_id = connection.get("patientId")

            if not isinstance(patient_id, str) or not patient_id.strip():
                raise LibreLinkUpResponseError(
                    "LibreLinkUp connection is missing a patient identifier"
                )

        return connections

    async def async_get_glucose_measurement(
        self,
        patient_id: str,
    ) -> dict:
        result = await self._async_get_authenticated(
            f"/llu/connections/{patient_id}/graph"
        )

        data = result.get("data")

        if not isinstance(data, dict):
            raise LibreLinkUpResponseError(
                "LibreLinkUp API returned malformed data"
            )

        connection = data.get("connection")

        if not isinstance(connection, dict):
            raise LibreLinkUpResponseError(
                "LibreLinkUp API returned malformed data"
            )

        measurement = connection.get("glucoseMeasurement")

        if measurement is None or measurement == {}:
            raise LibreLinkUpMeasurementError(
                "LibreLinkUp returned no glucose measurement"
            )

        if not isinstance(measurement, dict):
            raise LibreLinkUpResponseError(
                "LibreLinkUp API returned malformed data"
            )

        for field in REQUIRED_MEASUREMENT_FIELDS:
            value = measurement.get(field)

            # "is None" rather than a falsy check: TrendArrow 0 ("not
            # determined") and a glucose value of 0 are legitimate readings.
            if value is None:
                raise LibreLinkUpMeasurementError(
                    "LibreLinkUp glucose measurement is missing required fields"
                )

            if isinstance(value, (list, dict)):
                raise LibreLinkUpResponseError(
                    "LibreLinkUp API returned malformed data"
                )

        return measurement
