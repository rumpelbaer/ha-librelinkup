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
