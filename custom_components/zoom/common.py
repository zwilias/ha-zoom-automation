"""Common classes and functions for Zoom."""
from datetime import timedelta
from http import HTTPStatus
from logging import getLogger
from typing import Any, Dict, List
import hmac
import hashlib

from aiohttp.web import Request, Response, json_response
from homeassistant.components.http.view import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ZoomAPI
from .const import (
    DOMAIN,
    HA_URL,
    HA_ZOOM_EVENT,
    VERIFICATION_TOKENS,
    WEBHOOK_RESPONSE_SCHEMA,
)

_LOGGER = getLogger(__name__)


def valid_external_url(hass: HomeAssistant) -> bool:
    """Return whether a valid external URL for HA is available."""
    try:
        get_url(hass, allow_internal=False, prefer_cloud=True)
        return True
    except NoURLAvailableError:
        _LOGGER.error(
            "You do not have an external URL for your Home Assistant instance "
            "configured which is needed to set up the Zoom integration. "
            "You need to set the `external_url` property in the "
            "`homeassistant` section of your `configuration.yaml`, or set the "
            "`External URL` property in the Home Assistant `General "
            "Configuration` UI, before trying to setup the Zoom integration "
            "again. You can learn more about configuring this parameter at "
            "https://www.home-assistant.io/docs/configuration/basic"
        )
        return False


def get_contact_name(contact: dict) -> str:
    """Determine contact name from available first name, last naame, and email."""
    contact_name = ""
    if contact["first_name"]:
        contact_name = f"{contact['first_name']} "
    if contact["last_name"]:
        contact_name += f"{contact['last_name']} "

    if contact_name:
        return f"{contact_name}({contact['email']})"
    return contact["email"]


class ZoomOAuth2Implementation(config_entry_oauth2_flow.LocalOAuth2Implementation):
    """Oauth2 implementation that only uses the external url."""

    def __init__(
        self,
        hass: HomeAssistant,
        domain: str,
        client_id: str,
        client_secret: str,
        authorize_url: str,
        token_url: str,
        verification_token: str,
        name: str,
    ) -> None:
        """Initialize local auth implementation."""
        self._verification_token = verification_token
        self._name = name
        super().__init__(
            hass, domain, client_id, client_secret, authorize_url, token_url
        )

    @property
    def name(self) -> str:
        """Name of the implementation."""
        return self._name

    @property
    def domain(self) -> str:
        """Domain of the implementation."""
        return self._name

    @property
    def redirect_uri(self) -> str:
        """Return the redirect uri."""
        url = get_url(self.hass, allow_internal=False, prefer_cloud=True)
        return f"{url}{config_entry_oauth2_flow.AUTH_CALLBACK_PATH}"


class ZoomWebhookRequestView(HomeAssistantView):
    """Provide a page for the device to call."""

    requires_auth = False
    cors_allowed = True
    url = HA_URL
    name = HA_URL[1:].replace("/", ":")

    async def post(self, request: Request) -> Response:
        """Respond to requests from the device."""
        hass = request.app["hass"]
        headers = request.headers
        verification_tokens = hass.data.get(DOMAIN, {}).get(VERIFICATION_TOKENS, set())

        ts = headers.getone("x-zm-request-timestamp")
        check = headers.getone("x-zm-signature")
        textBody = await request.text()
        digestData = f'v0:{ts}:{textBody}'
        digest = hmac.new(bytes(next(iter(verification_tokens)), 'latin-1'), msg = bytes(digestData, 'latin-1'), digestmod = hashlib.sha256).hexdigest()
        if f'v0={digest}' != check:
            return Response(status=HTTPStatus.UNAUTHORIZED)

        try:
            data = await request.json()
            if data["event"] == "endpoint.url_validation":
                plain = data["payload"]["plainToken"]
                signature = hmac.new(bytes(next(iter(verification_tokens)) , 'latin-1'), msg = bytes(plain , 'latin-1'), digestmod = hashlib.sha256).hexdigest()
                _LOGGER.info(
                    "Received validation request: %s",
                    data
                )
                return json_response({'plainToken': plain, 'encryptedToken': signature})

            status = WEBHOOK_RESPONSE_SCHEMA(data)
            _LOGGER.warning("Received event: %s", status)
            hass.bus.async_fire(f"{HA_ZOOM_EVENT}", {**status})
        except Exception as err:
            _LOGGER.warning(
                "Received authorized event but unable to parse: %s (%s)",
                await request.text(),
                err,
            )
        return Response(status=HTTPStatus.OK)


class ZoomUserProfileDataUpdateCoordinator(DataUpdateCoordinator):
    """Define an object to hold Zoom user profile data."""

    def __init__(self, hass: HomeAssistant, api: ZoomAPI) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(days=1),
            update_method=self._async_update_data,
        )
        self._api = api

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via library."""
        try:
            return await self._api.async_get_my_user_profile()
        except Exception as err:
            raise UpdateFailed from err


class ZoomContactListDataUpdateCoordinator(DataUpdateCoordinator):
    """Define an object to hold Zoom Contact List data."""

    def __init__(
        self, hass: HomeAssistant, api: ZoomAPI, contact_types: List[str] = ["external"]
    ) -> None:
        """Initialize."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(hours=1),
            update_method=self._async_update_data,
        )
        self._api = api
        self._contact_types = contact_types

    async def _async_update_data(self) -> List[Dict[str, str]]:
        """Update data via library."""
        try:
            return await self._api.async_get_contacts(self._contact_types)
        except Exception as err:
            raise UpdateFailed from err
