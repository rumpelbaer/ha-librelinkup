"""Login semantics, driven through a real aiohttp session.

Uses Home Assistant's own aiohttp mocker instead of a hand-written fake
response, so the request the integration really sends -- headers, JSON body,
ClientTimeout -- is what gets exercised.

The point of these tests: only a verdict on the credentials themselves may end
up as a LibreLinkUpAuthenticationError, because that is what sends the user into
a re-authentication dialog. Everything else the API can answer describes a state
a new password cannot fix, and a dialog the user cannot satisfy is worse than an
error message.
"""

from __future__ import annotations

import pytest
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.librelinkup.api import (
    DEFAULT_BASE_URL,
    REGION_URLS,
    LibreLinkUpAccountStateError,
    LibreLinkUpApi,
    LibreLinkUpAuthenticationError,
    LibreLinkUpRegionError,
    LibreLinkUpResponseError,
)
from custom_components.librelinkup.const import LIBRELINKUP_APP_VERSION

LOGIN_URL = f"{DEFAULT_BASE_URL}/llu/auth/login"
EMAIL = "user@example.com"
PASSWORD = "super-secret-password"

SUCCESS_PAYLOAD = {
    "status": 0,
    "data": {
        "user": {"id": "user-id", "country": "de"},
        "authTicket": {"token": "a-token"},
    },
}


def make_api(hass) -> LibreLinkUpApi:
    return LibreLinkUpApi(async_get_clientsession(hass), EMAIL, PASSWORD)


async def test_successful_login(hass, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, json=SUCCESS_PAYLOAD)
    api = make_api(hass)

    await api.async_login()

    assert api.has_token is True
    assert api.region == "de"

    request = aioclient_mock.mock_calls[0]
    headers = request[3]

    # The app version lives in const.py so it can be raised in one place.
    assert headers["version"] == LIBRELINKUP_APP_VERSION
    assert headers["product"] == "llu.android"
    # And nothing leaks an Authorization header on the login request.
    assert "Authorization" not in headers


async def test_rejected_credentials_are_an_authentication_error(
    hass, aioclient_mock
) -> None:
    """The status LibreLinkUp answers with when the password is wrong."""
    aioclient_mock.post(LOGIN_URL, json={"status": 2})
    api = make_api(hass)

    with pytest.raises(LibreLinkUpAuthenticationError):
        await api.async_login()

    assert api.has_token is False


async def test_http_401_is_an_authentication_error(hass, aioclient_mock) -> None:
    aioclient_mock.post(LOGIN_URL, status=401, json={})
    api = make_api(hass)

    with pytest.raises(LibreLinkUpAuthenticationError):
        await api.async_login()


@pytest.mark.parametrize("status", [1, 3, 4, 5, -1, 429])
async def test_other_api_statuses_are_not_credential_failures(
    hass, aioclient_mock, status
) -> None:
    """Status 4 is the terms-of-use case: a new password would never help.

    Treating it as a wrong password produced a dialog the user could type the
    correct password into forever without getting anywhere.
    """
    aioclient_mock.post(LOGIN_URL, json={"status": status})
    api = make_api(hass)

    with pytest.raises(LibreLinkUpAccountStateError) as caught:
        await api.async_login()

    assert not isinstance(caught.value, LibreLinkUpAuthenticationError)
    # The status is safe to name; it says nothing about the account holder.
    assert str(status) in str(caught.value)


async def test_http_403_is_not_a_credential_failure(hass, aioclient_mock) -> None:
    """A "forbidden" login is an edge or client block, not a wrong password."""
    aioclient_mock.post(
        LOGIN_URL,
        status=403,
        text="<html>Request blocked</html>",
        headers={"Content-Type": "text/html"},
    )
    api = make_api(hass)

    with pytest.raises(LibreLinkUpAccountStateError) as caught:
        await api.async_login()

    assert not isinstance(caught.value, LibreLinkUpAuthenticationError)
    # Nothing of the blocked body is carried into the message.
    assert "html" not in str(caught.value).lower()


async def test_a_login_without_token_is_a_response_error(hass, aioclient_mock) -> None:
    """Status 0 but no usable ticket: the payload is broken, not the password."""
    aioclient_mock.post(LOGIN_URL, json={"status": 0, "data": {"user": {"id": "x"}}})
    api = make_api(hass)

    with pytest.raises(LibreLinkUpResponseError):
        await api.async_login()


async def test_region_redirect_is_followed_once(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        LOGIN_URL, json={"status": 0, "data": {"redirect": True, "region": "eu2"}}
    )
    aioclient_mock.post(f"{REGION_URLS['eu2']}/llu/auth/login", json=SUCCESS_PAYLOAD)
    api = make_api(hass)

    await api.async_login()

    assert api.base_url == REGION_URLS["eu2"]
    assert api.has_token is True


async def test_unknown_region_is_permanent(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        LOGIN_URL,
        json={"status": 0, "data": {"redirect": True, "region": "atlantis"}},
    )
    api = make_api(hass)

    with pytest.raises(LibreLinkUpRegionError):
        await api.async_login()


async def test_a_redirect_loop_does_not_spin(hass, aioclient_mock) -> None:
    aioclient_mock.post(
        LOGIN_URL, json={"status": 0, "data": {"redirect": True, "region": "us"}}
    )
    api = make_api(hass)

    with pytest.raises(LibreLinkUpRegionError):
        await api.async_login()

    # Two attempts, then it gives up instead of following redirects forever.
    assert len(aioclient_mock.mock_calls) == 2


async def test_credentials_never_appear_in_an_error_message(
    hass, aioclient_mock
) -> None:
    aioclient_mock.post(LOGIN_URL, json={"status": 2})
    api = make_api(hass)

    with pytest.raises(LibreLinkUpAuthenticationError) as caught:
        await api.async_login()

    message = str(caught.value)

    assert PASSWORD not in message
    assert EMAIL not in message
