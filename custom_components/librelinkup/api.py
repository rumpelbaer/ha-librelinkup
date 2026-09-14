from __future__ import annotations

import hashlib

from aiohttp import ClientSession


BASE_URL = "https://api-de.libreview.io"

HEADERS = {
    "product": "llu.android",
    "version": "4.16.0",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


class LibreLinkUpAuthenticationError(Exception):
    pass


class LibreLinkUpApi:
    def __init__(self, session: ClientSession, email: str, password: str) -> None:
        self._session = session
        self._email = email
        self._password = password
        self._token: str | None = None
        self._account_id: str | None = None

    async def async_login(self) -> None:
        async with self._session.post(
            f"{BASE_URL}/llu/auth/login",
            headers=HEADERS,
            json={"email": self._email, "password": self._password},
            timeout=20,
        ) as response:
            response.raise_for_status()
            result = await response.json()

        if result.get("status") != 0:
            raise LibreLinkUpAuthenticationError(
                f"LibreLinkUp login failed with status {result.get('status')}"
            )

        data = result.get("data") or {}
        user = data.get("user") or {}
        auth = data.get("authTicket") or {}

        user_id = user.get("id")
        token = auth.get("token")

        if not user_id or not token:
            raise LibreLinkUpAuthenticationError(
                "LibreLinkUp login did not return user ID and token"
            )

        self._token = token
        self._account_id = hashlib.sha256(user_id.encode("utf-8")).hexdigest()

    def _auth_headers(self) -> dict[str, str]:
        if not self._token or not self._account_id:
            raise RuntimeError("LibreLinkUp client is not authenticated")

        return {
            **HEADERS,
            "Authorization": f"Bearer {self._token}",
            "Account-Id": self._account_id,
        }

    async def async_get_connections(self) -> list[dict]:
        async with self._session.get(
            f"{BASE_URL}/llu/connections",
            headers=self._auth_headers(),
            timeout=20,
        ) as response:
            response.raise_for_status()
            result = await response.json()

        return result.get("data") or []

    async def async_get_glucose_measurement(self, patient_id: str) -> dict:
        async with self._session.get(
            f"{BASE_URL}/llu/connections/{patient_id}/graph",
            headers=self._auth_headers(),
            timeout=20,
        ) as response:
            response.raise_for_status()
            result = await response.json()

        data = result.get("data") or {}
        connection = data.get("connection") or {}
        return connection.get("glucoseMeasurement") or {}
