from __future__ import annotations

import importlib.util
import unittest
from unittest.mock import AsyncMock
from pathlib import Path

from aiohttp import ClientResponseError


API_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "librelinkup"
    / "api.py"
)

spec = importlib.util.spec_from_file_location("librelinkup_api", API_PATH)
api_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(api_module)

LibreLinkUpApi = api_module.LibreLinkUpApi
LibreLinkUpAuthenticationError = api_module.LibreLinkUpAuthenticationError


class FakeResponse:
    def __init__(self, data: dict, status: int = 200) -> None:
        self._data = data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise ClientResponseError(
                request_info=None,
                history=(),
                status=self.status,
                message="Test error",
            )

    async def json(self) -> dict:
        return self._data


class FakeSession:
    def __init__(self) -> None:
        self.get_calls = 0
        self.post_calls = 0

    def post(self, *args, **kwargs) -> FakeResponse:
        self.post_calls += 1
        return FakeResponse(
            {
                "status": 0,
                "data": {
                    "user": {"id": "test-user-id"},
                    "authTicket": {"token": "new-valid-token"},
                },
            }
        )

    def get(self, *args, **kwargs) -> FakeResponse:
        self.get_calls += 1

        if self.get_calls == 1:
            return FakeResponse({}, status=401)

        return FakeResponse(
            {
                "status": 0,
                "data": [{"patientId": "test-patient"}],
            }
        )


class TestLibreLinkUpApi(unittest.IsolatedAsyncioTestCase):
    async def test_reauthenticates_after_unauthorized_response(self) -> None:
        session = FakeSession()
        api = LibreLinkUpApi(session, "test@example.com", "password")

        api._token = "expired-token"
        api._account_id = "test-account-id"

        connections = await api.async_get_connections()

        self.assertEqual(connections, [{"patientId": "test-patient"}])
        self.assertEqual(session.get_calls, 2)
        self.assertEqual(session.post_calls, 1)
        self.assertEqual(api._token, "new-valid-token")


if __name__ == "__main__":
    unittest.main()

class RegionRedirectResponse:
    def __init__(self, data: dict) -> None:
        self._data = data
        self.status = 200

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> dict:
        return self._data


class RegionRedirectSession:
    def __init__(self) -> None:
        self.urls = []

    def post(self, url, *args, **kwargs):
        self.urls.append(url)

        if len(self.urls) == 1:
            return RegionRedirectResponse(
                {
                    "status": 0,
                    "data": {
                        "redirect": True,
                        "region": "eu",
                    },
                }
            )

        return RegionRedirectResponse(
            {
                "status": 0,
                "data": {
                    "user": {
                        "id": "test-user-id",
                        "country": "NL",
                    },
                    "authTicket": {
                        "token": "test-token",
                    },
                },
            }
        )


class TestLibreLinkUpRegionRedirect(unittest.IsolatedAsyncioTestCase):
    async def test_follows_region_redirect(self) -> None:
        session = RegionRedirectSession()
        api = LibreLinkUpApi(session, "test@example.com", "password")

        await api.async_login()

        self.assertEqual(api.region, "eu")
        self.assertEqual(api.base_url, "https://api-eu.libreview.io")
        self.assertEqual(
            session.urls,
            [
                "https://api.libreview.io/llu/auth/login",
                "https://api-eu.libreview.io/llu/auth/login",
            ],
        )

class TestLibreLinkUpAuthenticationFailure(unittest.IsolatedAsyncioTestCase):
    async def test_raises_authentication_error_after_failed_reauthentication(self) -> None:
        class Response:
            def __init__(self, status, payload=None):
                self.status = status
                self._payload = payload or {}

            async def __aenter__(self):
                if self.status >= 400:
                    from aiohttp import ClientResponseError, RequestInfo
                    from multidict import CIMultiDict, CIMultiDictProxy

                    raise ClientResponseError(
                        RequestInfo(
                            url="https://api.libreview.io/test",
                            method="GET",
                            headers=CIMultiDictProxy(CIMultiDict()),
                            real_url="https://api.libreview.io/test",
                        ),
                        (),
                        status=self.status,
                    )
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def raise_for_status(self):
                return None

            async def json(self):
                return self._payload

        class Session:
            def __init__(self):
                self.get_calls = 0

            def get(self, *args, **kwargs):
                self.get_calls += 1
                return Response(401)

        api = LibreLinkUpApi(Session(), "test@example.com", "password")
        api._token = "expired-token"
        api._account_id = "account-id"
        api.async_login = AsyncMock()

        with self.assertRaises(LibreLinkUpAuthenticationError):
            await api._async_get_authenticated("/llu/connections")

        api.async_login.assert_awaited_once()


# --- Response validation (H8) -------------------------------------------------

import json as _json

import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from aiohttp import ContentTypeError, RequestInfo

LibreLinkUpResponseError = api_module.LibreLinkUpResponseError
LibreLinkUpMeasurementError = api_module.LibreLinkUpMeasurementError

TOKEN = "super-secret-bearer-token"
PATIENT_ID = "patient-uuid-0001"
EMAIL = "user@example.com"
PASSWORD = "super-secret-password"

VALID_MEASUREMENT = {
    "ValueInMgPerDl": 115,
    "FactoryTimestamp": "9/14/2026 11:12:35 AM",
    "TrendArrow": 3,
    "isLow": False,
    "isHigh": False,
}


def _request_info(url="https://api-de.libreview.io/llu/connections"):
    return RequestInfo(
        url=url,
        method="GET",
        headers=CIMultiDictProxy(
            CIMultiDict({"Authorization": f"Bearer {TOKEN}"})
        ),
        real_url=url,
    )


class PayloadResponse:
    """A GET response returning a fixed payload, HTTP status or decode error."""

    def __init__(self, payload=None, status=200, json_error=None):
        self._payload = payload
        self.status = status
        self._json_error = json_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise ClientResponseError(
                _request_info(), (), status=self.status, message="Test error"
            )

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class PayloadSession:
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        self.get_calls = 0

    def get(self, *args, **kwargs):
        self.get_calls += 1
        return PayloadResponse(**self._kwargs)


def make_api(**kwargs):
    session = PayloadSession(**kwargs)
    api = api_module.LibreLinkUpApi(session, EMAIL, PASSWORD)
    api._token = TOKEN
    api._account_id = "account-id"
    return api, session


def assert_no_sensitive_data(err: Exception) -> None:
    """New exceptions must never carry payloads, credentials or identifiers."""
    for rendered in (str(err), repr(err)):
        assert TOKEN not in rendered
        assert PASSWORD not in rendered
        assert EMAIL not in rendered
        assert PATIENT_ID not in rendered
        assert "115" not in rendered
        assert "Bearer" not in rendered


# A. Success payloads keep working -------------------------------------------

async def test_valid_connections_payload() -> None:
    api, _ = make_api(
        payload={
            "status": 0,
            "data": [{"patientId": PATIENT_ID, "firstName": "Test"}],
        }
    )

    assert await api.async_get_connections() == [
        {"patientId": PATIENT_ID, "firstName": "Test"}
    ]


async def test_valid_graph_payload() -> None:
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {"connection": {"glucoseMeasurement": VALID_MEASUREMENT}},
        }
    )

    assert await api.async_get_glucose_measurement(PATIENT_ID) == VALID_MEASUREMENT


async def test_empty_connection_list_is_valid() -> None:
    """No shared patients is a legitimate answer, not a malformed one."""
    api, _ = make_api(payload={"status": 0, "data": []})

    assert await api.async_get_connections() == []


async def test_trend_arrow_zero_and_zero_glucose_stay_valid() -> None:
    """0 is a real value for both fields and must not read as "missing"."""
    measurement = {**VALID_MEASUREMENT, "TrendArrow": 0, "ValueInMgPerDl": 0}
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {"connection": {"glucoseMeasurement": measurement}},
        }
    )

    assert await api.async_get_glucose_measurement(PATIENT_ID) == measurement


# B. Top level is not a dict --------------------------------------------------

@pytest.mark.parametrize("payload", [[], None, "invalid", 42])
async def test_non_dict_top_level_is_rejected(payload) -> None:
    api, _ = make_api(payload=payload)

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert_no_sensitive_data(caught.value)


# C. API level status ---------------------------------------------------------

@pytest.mark.parametrize("status", [1, 2, 4, -1])
async def test_non_success_status_is_rejected(status) -> None:
    api, _ = make_api(payload={"status": status, "data": {}})

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert "non-success status" in str(caught.value)
    assert str(status) not in str(caught.value)
    assert_no_sensitive_data(caught.value)


async def test_non_success_status_is_rejected_on_graph() -> None:
    api, _ = make_api(payload={"status": 2, "data": {}})

    with pytest.raises(LibreLinkUpResponseError):
        await api.async_get_glucose_measurement(PATIENT_ID)


# D-F. Connections structure --------------------------------------------------

@pytest.mark.parametrize("data", [{}, "invalid", None, 5])
async def test_connections_data_must_be_a_list(data) -> None:
    api, _ = make_api(payload={"status": 0, "data": data})

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert_no_sensitive_data(caught.value)


@pytest.mark.parametrize("element", ["invalid", None, 1, []])
async def test_connection_elements_must_be_dicts(element) -> None:
    api, _ = make_api(payload={"status": 0, "data": [element]})

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert_no_sensitive_data(caught.value)


@pytest.mark.parametrize(
    "connection",
    [
        {"firstName": "Test"},
        {"patientId": None},
        {"patientId": ""},
        {"patientId": "   "},
        {"patientId": 12345},
    ],
)
async def test_connection_without_usable_patient_id_is_rejected(connection) -> None:
    """Fail closed: never offer a connection we cannot address later."""
    api, _ = make_api(payload={"status": 0, "data": [connection]})

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert_no_sensitive_data(caught.value)


async def test_one_broken_connection_invalidates_the_whole_list() -> None:
    api, _ = make_api(
        payload={
            "status": 0,
            "data": [{"patientId": PATIENT_ID}, {"firstName": "Broken"}],
        }
    )

    with pytest.raises(LibreLinkUpResponseError):
        await api.async_get_connections()


# G-I. Graph structure --------------------------------------------------------

@pytest.mark.parametrize("data", [[], "invalid", None, 7])
async def test_graph_data_must_be_a_dict(data) -> None:
    api, _ = make_api(payload={"status": 0, "data": data})

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_glucose_measurement(PATIENT_ID)

    assert_no_sensitive_data(caught.value)


@pytest.mark.parametrize("connection", [[], "invalid", 3])
async def test_graph_connection_must_be_a_dict(connection) -> None:
    api, _ = make_api(payload={"status": 0, "data": {"connection": connection}})

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_glucose_measurement(PATIENT_ID)

    assert_no_sensitive_data(caught.value)


@pytest.mark.parametrize("measurement", ["invalid", [], 1.0, [VALID_MEASUREMENT]])
async def test_measurement_with_wrong_type_is_rejected(measurement) -> None:
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {"connection": {"glucoseMeasurement": measurement}},
        }
    )

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_glucose_measurement(PATIENT_ID)

    assert not isinstance(caught.value, LibreLinkUpMeasurementError)
    assert_no_sensitive_data(caught.value)


# J. Missing measurement ------------------------------------------------------

@pytest.mark.parametrize(
    "connection",
    [{}, {"glucoseMeasurement": {}}, {"glucoseMeasurement": None}],
)
async def test_missing_measurement_raises_measurement_error(connection) -> None:
    api, _ = make_api(payload={"status": 0, "data": {"connection": connection}})

    with pytest.raises(LibreLinkUpMeasurementError) as caught:
        await api.async_get_glucose_measurement(PATIENT_ID)

    assert_no_sensitive_data(caught.value)


async def test_measurement_error_is_a_response_error() -> None:
    """One except clause must be able to cover both."""
    assert issubclass(LibreLinkUpMeasurementError, LibreLinkUpResponseError)


# K. Required fields ----------------------------------------------------------

@pytest.mark.parametrize(
    "field", ["ValueInMgPerDl", "FactoryTimestamp", "TrendArrow"]
)
async def test_missing_required_field_is_rejected(field) -> None:
    measurement = {k: v for k, v in VALID_MEASUREMENT.items() if k != field}
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {"connection": {"glucoseMeasurement": measurement}},
        }
    )

    with pytest.raises(LibreLinkUpMeasurementError) as caught:
        await api.async_get_glucose_measurement(PATIENT_ID)

    assert_no_sensitive_data(caught.value)


@pytest.mark.parametrize(
    "field", ["ValueInMgPerDl", "FactoryTimestamp", "TrendArrow"]
)
@pytest.mark.parametrize("value", [[], {}])
async def test_structurally_broken_required_field_is_rejected(field, value) -> None:
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {
                "connection": {
                    "glucoseMeasurement": {**VALID_MEASUREMENT, field: value}
                }
            },
        }
    )

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_glucose_measurement(PATIENT_ID)

    assert_no_sensitive_data(caught.value)


async def test_numeric_string_value_stays_accepted() -> None:
    """The entity layer already coerces these, so the API stays permissive."""
    measurement = {**VALID_MEASUREMENT, "ValueInMgPerDl": "115"}
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {"connection": {"glucoseMeasurement": measurement}},
        }
    )

    assert await api.async_get_glucose_measurement(PATIENT_ID) == measurement


async def test_missing_is_low_and_is_high_stay_accepted() -> None:
    """Optional flags degrade to off instead of discarding the reading."""
    measurement = {
        "ValueInMgPerDl": 115,
        "FactoryTimestamp": "9/14/2026 11:12:35 AM",
        "TrendArrow": 3,
    }
    api, _ = make_api(
        payload={
            "status": 0,
            "data": {"connection": {"glucoseMeasurement": measurement}},
        }
    )

    assert await api.async_get_glucose_measurement(PATIENT_ID) == measurement


# L. Broken JSON --------------------------------------------------------------

async def test_wrong_content_type_does_not_leak_the_token() -> None:
    """ContentTypeError is a ClientResponseError whose repr holds the token."""
    error = ContentTypeError(
        _request_info(),
        (),
        message="Attempt to decode JSON with unexpected mimetype: text/html",
    )
    api, _ = make_api(json_error=error)

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert "content type" in str(caught.value)
    assert_no_sensitive_data(caught.value)
    # "from None" must cut the chain, or the token resurfaces in tracebacks.
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_invalid_json_is_translated() -> None:
    error = _json.JSONDecodeError("Expecting value", "{not json", 1)
    api, _ = make_api(json_error=error)

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert "invalid JSON" in str(caught.value)
    assert "not json" not in str(caught.value)
    assert_no_sensitive_data(caught.value)
    assert caught.value.__cause__ is None


async def test_empty_body_is_rejected() -> None:
    """aiohttp decodes an empty JSON body to None rather than raising."""
    api, _ = make_api(payload=None)

    with pytest.raises(LibreLinkUpResponseError) as caught:
        await api.async_get_connections()

    assert_no_sensitive_data(caught.value)


# N/O. HTTP level errors stay distinguishable ---------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_http_errors_keep_their_status(status) -> None:
    """Needed so rate limiting and 5xx backoff can be added later (H2)."""
    api, session = make_api(status=status)

    with pytest.raises(ClientResponseError) as caught:
        await api.async_get_connections()

    assert caught.value.status == status
    assert not isinstance(caught.value, LibreLinkUpResponseError)
    # No pointless re-login attempt for a non-auth failure.
    assert session.get_calls == 1
