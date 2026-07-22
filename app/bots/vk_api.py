"""Лёгкий асинхронный клиент VK API.

``vkbottle`` импортировал все сгенерированные методы/ответы VK и занимал
около 550 МБ RSS ещё до первого сообщения. Здесь реализован только транспорт
и те операции, которые реально использует проект. Ответы остаются удобными:
поля словарей доступны и как ключи, и как атрибуты.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, AsyncIterator

import aiohttp

logger = logging.getLogger("vk.api")

VK_API_URL = "https://api.vk.com/method"
VK_API_VERSION = "5.199"
_RETRIABLE_CODES = {1, 6, 9, 10, 29}


class VKAPIError(RuntimeError):
    def __init__(self, method: str, code: int | None, message: str):
        self.method = method
        self.code = code
        super().__init__(f"VK API {method}: [{code}] {message}")


class VKObject(dict):
    """Dict с доступом ``obj.field`` для совместимости старых обработчиков."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _objectify(value: Any) -> Any:
    if isinstance(value, dict):
        return VKObject({key: _objectify(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_objectify(item) for item in value]
    return value


def _camel(name: str) -> str:
    return re.sub(r"_([a-z])", lambda m: m.group(1).upper(), name)


def _param(value: Any) -> str | int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (list, tuple, set)):
        # Обычные массивные параметры VK API (user_ids, fields и т.п.) —
        # CSV, не JSON. Вложенные структуры встречаются только в payload.
        if all(not isinstance(item, (dict, list, tuple, set)) for item in value):
            return ",".join(str(item) for item in value)
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
    return value


class VKTransport:
    """Один HTTP connection pool для всех клиентских токенов."""

    def __init__(self, *, timeout_seconds: float = 35.0):
        self.timeout_seconds = timeout_seconds
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
                    connector = aiohttp.TCPConnector(limit=50, ttl_dns_cache=300)
                    self._session = aiohttp.ClientSession(
                        timeout=timeout, connector=connector,
                        headers={"User-Agent": "sports-training-bot/1"},
                    )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def api_call(self, token: str, method: str, **params: Any) -> Any:
        data = {
            key: _param(value)
            for key, value in params.items()
            if value is not None
        }
        data.update({"access_token": token, "v": VK_API_VERSION})
        session = await self.session()
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with session.post(f"{VK_API_URL}/{method}", data=data) as response:
                    response.raise_for_status()
                    payload = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                last_error = exc
                if attempt == 3:
                    raise RuntimeError(f"VK HTTP {method}: {type(exc).__name__}") from exc
                await asyncio.sleep(0.25 * (2 ** attempt))
                continue

            error = payload.get("error") if isinstance(payload, dict) else None
            if not error:
                return _objectify(payload.get("response"))
            code = error.get("error_code")
            message = str(error.get("error_msg") or "unknown error")
            if code in _RETRIABLE_CODES and attempt < 3:
                await asyncio.sleep(0.35 * (2 ** attempt))
                continue
            raise VKAPIError(method, code, message)
        raise RuntimeError(f"VK HTTP {method}: {last_error}")

    async def get_json(self, url: str, **params: Any) -> dict[str, Any]:
        session = await self.session()
        async with session.get(url, params=params) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise RuntimeError("VK Long Poll вернул не объект")
        return payload

    async def upload(self, url: str, *, filename: str, data: bytes) -> dict[str, Any]:
        form = aiohttp.FormData()
        form.add_field(
            "file", data, filename=filename,
            content_type="text/csv; charset=utf-8",
        )
        session = await self.session()
        async with session.post(url, data=form) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
        if not isinstance(payload, dict):
            raise RuntimeError("VK upload вернул не объект")
        return payload


class _Namespace:
    def __init__(self, api: "VKAPI", category: str):
        self.api = api
        self.category = category

    def __getattr__(self, method: str):
        vk_method = f"{self.category}.{_camel(method)}"

        async def call(**params: Any) -> Any:
            return await self.api.call(vk_method, **params)

        return call


class VKAPI:
    def __init__(self, token: str, transport: VKTransport):
        self.token = token
        self.transport = transport
        for category in ("messages", "groups", "users", "wall", "docs"):
            setattr(self, category, _Namespace(self, category))

    async def call(self, method: str, **params: Any) -> Any:
        return await self.transport.api_call(self.token, method, **params)

    async def upload_message_document(
        self, *, peer_id: int, filename: str, data: bytes
    ) -> str:
        server = await self.docs.get_messages_upload_server(
            peer_id=peer_id, type="doc"
        )
        upload_url = getattr(server, "upload_url", None)
        if not upload_url:
            raise RuntimeError("VK не вернул upload_url для документа")
        uploaded = await self.transport.upload(
            upload_url, filename=filename, data=data
        )
        saved = await self.docs.save(file=uploaded.get("file"), title=filename)
        doc = None
        if isinstance(saved, dict):
            doc = saved.get("doc") or saved.get("graffiti") or saved
        elif isinstance(saved, list) and saved:
            doc = saved[0]
        if not isinstance(doc, dict):
            raise RuntimeError("VK не вернул сохранённый документ")
        owner_id, doc_id = doc.get("owner_id"), doc.get("id")
        if owner_id is None or doc_id is None:
            raise RuntimeError("VK вернул документ без owner_id/id")
        access_key = doc.get("access_key")
        suffix = f"_{access_key}" if access_key else ""
        return f"doc{owner_id}_{doc_id}{suffix}"


def _first_group(response: Any) -> int | None:
    groups = response
    if isinstance(response, dict):
        groups = response.get("groups") or response.get("items") or []
    if not isinstance(groups, list) or not groups:
        return None
    first = groups[0]
    if isinstance(first, dict):
        value = first.get("id")
        return int(value) if value is not None else None
    value = getattr(first, "id", None)
    return int(value) if value is not None else None


class VKBot:
    """Токен + API + лёгкий Group Long Poll fallback."""

    def __init__(self, token: str, transport: VKTransport):
        self.api = VKAPI(token, transport)
        self.group_id: int | None = None

    async def resolve_group_id(self) -> int:
        response = await self.api.groups.get_by_id()
        group_id = _first_group(response)
        if group_id is None:
            raise RuntimeError("VK groups.getById не вернул id сообщества")
        self.group_id = group_id
        return group_id

    async def listen(self) -> AsyncIterator[dict[str, Any]]:
        if self.group_id is None:
            await self.resolve_group_id()
        server: str | None = None
        key: str | None = None
        ts: str | None = None
        while True:
            if not (server and key and ts):
                info = await self.api.groups.get_long_poll_server(
                    group_id=self.group_id
                )
                server = str(info.server)
                key = str(info.key)
                ts = str(info.ts)
            payload = await self.api.transport.get_json(
                server, act="a_check", key=key, ts=ts, wait=25
            )
            failed = payload.get("failed")
            if failed:
                if failed == 1 and payload.get("ts") is not None:
                    ts = str(payload["ts"])
                else:
                    server = key = ts = None
                continue
            ts = str(payload.get("ts", ts))
            for update in payload.get("updates") or []:
                if isinstance(update, dict):
                    yield update
