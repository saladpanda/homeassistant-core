"""Config helpers for Alexa."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Collection
import logging
from typing import Any

from yarl import URL

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import COMPUTED_NAME
from homeassistant.helpers.storage import Store
from homeassistant.util import slugify

from .const import DOMAIN
from .entities import TRANSLATION_TABLE
from .state_report import async_enable_proactive_mode

STORE_AUTHORIZED = "authorized"

_LOGGER = logging.getLogger(__name__)


class AbstractConfig(ABC):
    """Hold the configuration for Alexa."""

    _ALEXA_ALIAS_DELIMITER = "::alias::"
    _store: AlexaConfigStore
    _unsub_proactive_report: CALLBACK_TYPE | None = None

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize abstract config."""
        self.hass = hass
        self._enable_proactive_mode_lock = asyncio.Lock()
        self._on_deinitialize: list[CALLBACK_TYPE] = []

    async def async_initialize(self) -> None:
        """Perform async initialization of config."""
        self._store = AlexaConfigStore(self.hass)
        await self._store.async_load()

    @callback
    def async_deinitialize(self) -> None:
        """Remove listeners."""
        _LOGGER.debug("async_deinitialize")
        while self._on_deinitialize:
            self._on_deinitialize.pop()()

    @property
    def supports_auth(self) -> bool:
        """Return if config supports auth."""
        return False

    @property
    def should_report_state(self) -> bool:
        """Return if states should be proactively reported."""
        return False

    @property
    @abstractmethod
    def endpoint(self) -> str | URL | None:
        """Endpoint for report state."""

    @property
    @abstractmethod
    def locale(self) -> str | None:
        """Return config locale."""

    @property
    def entity_config(self) -> dict[str, Any]:
        """Return entity config."""
        return {}

    @property
    def is_reporting_states(self) -> bool:
        """Return if proactive mode is enabled."""
        return self._unsub_proactive_report is not None

    @callback
    @abstractmethod
    def user_identifier(self) -> str:
        """Return an identifier for the user that represents this config."""

    async def async_enable_proactive_mode(self) -> None:
        """Enable proactive mode."""
        _LOGGER.debug("Enable proactive mode")
        async with self._enable_proactive_mode_lock:
            if self._unsub_proactive_report is not None:
                return
            self._unsub_proactive_report = await async_enable_proactive_mode(
                self.hass, self
            )

    async def async_disable_proactive_mode(self) -> None:
        """Disable proactive mode."""
        _LOGGER.debug("Disable proactive mode")
        if unsub_func := self._unsub_proactive_report:
            unsub_func()
        self._unsub_proactive_report = None

    @callback
    def should_expose(self, entity_id: str) -> bool:
        """If an entity should be exposed."""
        return False

    def generate_alexa_id(self, entity_id: str) -> str:
        """Return the alexa ID for an entity ID."""
        return self.generate_alexa_id_for(entity_id)

    def generate_alexa_id_for(self, entity_id: str, alias: str | None = None) -> str:
        """Return the Alexa ID for an entity ID and optional alias."""
        alexa_id = entity_id.replace(".", "#").translate(TRANSLATION_TABLE)

        if alias is None:
            return alexa_id

        return f"{alexa_id}{self._ALEXA_ALIAS_DELIMITER}{slugify(alias)}"

    @callback
    def get_entity_aliases(self, entity_id: str) -> list[str]:
        """Return configured aliases for an entity."""
        entity_registry = er.async_get(self.hass)
        if not (entity_entry := entity_registry.async_get(entity_id)):
            return []

        return self.normalize_aliases(entity_id, entity_entry.aliases)

    @callback
    def normalize_aliases(self, entity_id: str, aliases: Collection[str]) -> list[str]:
        """Return deduplicated, sanitized aliases for an entity."""

        unique_aliases: list[str] = []
        seen_alexa_ids: set[str] = set()

        for alias in aliases:
            if alias is COMPUTED_NAME:
                continue
            translated_alias = alias.translate(TRANSLATION_TABLE).strip()
            alias_id = self.generate_alexa_id_for(entity_id, translated_alias)

            if not translated_alias or alias_id in seen_alexa_ids:
                continue
            seen_alexa_ids.add(alias_id)
            unique_aliases.append(translated_alias)

        return sorted(unique_aliases, key=str.casefold)

    @callback
    def get_alias_alexa_ids(
        self, entity_id: str, aliases: Collection[str] | None = None
    ) -> list[str]:
        """Return alias Alexa IDs for an entity."""
        if aliases is None:
            aliases = self.get_entity_aliases(entity_id)
        else:
            aliases = self.normalize_aliases(entity_id, aliases)

        return [self.generate_alexa_id_for(entity_id, alias) for alias in aliases]

    @callback
    def get_entity_alexa_ids(self, entity_id: str) -> list[str]:
        """Return the canonical and alias Alexa IDs for an entity."""
        alexa_ids = [self.generate_alexa_id_for(entity_id)]
        alexa_ids.extend(self.get_alias_alexa_ids(entity_id))
        return alexa_ids

    @callback
    def resolve_entity_id(self, endpoint_id: str) -> str:
        """Resolve an Alexa endpoint ID back to an entity ID."""
        entity_endpoint_id = endpoint_id.split(self._ALEXA_ALIAS_DELIMITER, 1)[0]
        return entity_endpoint_id.replace("#", ".")

    @callback
    def async_invalidate_access_token(self) -> None:
        """Invalidate access token."""
        raise NotImplementedError

    async def async_get_access_token(self) -> str | None:
        """Get an access token."""
        raise NotImplementedError

    async def async_accept_grant(self, code: str) -> str | None:
        """Accept a grant."""
        raise NotImplementedError

    @property
    def authorized(self) -> bool:
        """Return authorization status."""
        return self._store.authorized

    async def set_authorized(self, authorized: bool) -> None:
        """Set authorization status.

        - Set when an incoming message is received from Alexa.
        - Unset if state reporting fails
        """
        self._store.set_authorized(authorized)
        if self.should_report_state != self.is_reporting_states:
            if self.should_report_state:
                try:
                    await self.async_enable_proactive_mode()
                except Exception:
                    # We failed to enable proactive mode, unset authorized flag
                    self._store.set_authorized(False)
                    raise
            else:
                await self.async_disable_proactive_mode()


class AlexaConfigStore:
    """A configuration store for Alexa."""

    _STORAGE_VERSION = 1
    _STORAGE_KEY = DOMAIN

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize a configuration store."""
        self._data: dict[str, Any] | None = None
        self._hass = hass
        self._store: Store = Store(hass, self._STORAGE_VERSION, self._STORAGE_KEY)

    @property
    def authorized(self) -> bool:
        """Return authorization status."""
        assert self._data is not None
        return bool(self._data[STORE_AUTHORIZED])

    @callback
    def set_authorized(self, authorized: bool) -> None:
        """Set authorization status."""
        if self._data is not None and authorized != self._data[STORE_AUTHORIZED]:
            self._data[STORE_AUTHORIZED] = authorized
            self._store.async_delay_save(lambda: self._data, 1.0)

    async def async_load(self) -> None:
        """Load saved configuration from disk."""
        if data := await self._store.async_load():
            self._data = data
        else:
            self._data = {STORE_AUTHORIZED: False}
