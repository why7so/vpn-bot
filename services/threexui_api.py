"""
Тонкий асинхронный клиент к API панели 3x-ui.

В отличие от Marzban, у 3x-ui нет отдельной сущности "пользователь" —
клиенты (uuid/email/срок действия) живут внутри конкретного инбаунда
(Inbound). Поэтому бот работает с одним заранее выбранным инбаундом
(XUI_INBOUND_ID, см. config.py) и добавляет/обновляет/удаляет в нём
клиентов через JSON-поле `settings`.

Список разрешённых доменов/IP по-прежнему настраивается отдельно —
на стороне Xray routing rules в самой панели 3x-ui.

Авторизация в 3x-ui — по cookie-сессии (POST /login), а не по Bearer-токену,
поэтому клиент держит одну aiohttp.ClientSession с cookie jar и логинится
заново, когда сессия протухает.

Документация/исходники панели: https://github.com/MHSanaei/3x-ui
Имена путей ниже соответствуют этому форку; если вы используете другой
форк x-ui, подкорректируйте префиксы путей (`/panel/api/inbounds/...`).
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import time
import uuid as uuid_lib

import aiohttp

from config import config


class ThreeXUIError(RuntimeError):
    pass


class ThreeXUIClient:
    def __init__(self) -> None:
        self._base_url = config.xui_base_url
        self._username = config.xui_username
        self._password = config.xui_password
        self._inbound_id = config.xui_inbound_id

        self._session: aiohttp.ClientSession | None = None
        self._logged_in_at: float = 0.0

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            # Без таймаута aiohttp может пытаться достучаться до недоступного
            # хоста несколько минут — за это время Telegram callback уже
            # "протухнет" (query is too old), и пользователь просто не увидит
            # сообщение об ошибке. Разумный таймаут даёт быстрый и понятный фейл.
            timeout = aiohttp.ClientTimeout(total=15, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def _login(self, session: aiohttp.ClientSession) -> None:
        try:
            async with session.post(
                f"{self._base_url}/login",
                data={"username": self._username, "password": self._password},
            ) as resp:
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ThreeXUIError(f"Не удалось подключиться к 3x-ui ({self._base_url}): {e}") from e
            if resp.status != 200:
                raise ThreeXUIError(f"Не удалось авторизоваться в 3x-ui: {resp.status} {text}")
            try:
                payload = json.loads(text)
            except ValueError:
                payload = {}
            if payload and not payload.get("success", True):
                raise ThreeXUIError(f"3x-ui отклонил логин: {text}")

        # сессия панели обычно живёт долго, но подстрахуемся и обновим через 50 минут
        self._logged_in_at = time.time()

    async def _ensure_login(self, session: aiohttp.ClientSession) -> None:
        if self._logged_in_at and time.time() - self._logged_in_at < 50 * 60:
            return
        await self._login(session)

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict | None = None,
        _retry: bool = True,
    ) -> dict:
        session = await self._get_session()
        await self._ensure_login(session)

        try:
            async with session.request(method, f"{self._base_url}{path}", json=json_body) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise ThreeXUIError(f"Не удалось подключиться к 3x-ui ({self._base_url}): {e}") from e

        # 3x-ui при протухшей сессии обычно отдаёт редирект на /login или 401/403
        if status in (401, 403) and _retry:
            self._logged_in_at = 0.0
            return await self._request(method, path, json_body=json_body, _retry=False)

        if status >= 400:
            raise ThreeXUIError(f"3x-ui API {method} {path} -> {status}: {text}")

        if not text:
            return {}
        try:
            payload = json.loads(text)
        except ValueError as e:
            raise ThreeXUIError(f"3x-ui вернул не-JSON ответ на {method} {path}: {text[:300]}") from e

        if isinstance(payload, dict) and not payload.get("success", True):
            raise ThreeXUIError(f"3x-ui API {method} {path} -> success=false: {payload}")

        return payload

    @staticmethod
    def build_email(tg_id: int) -> str:
        """Email клиента внутри инбаунда — по нему находим/сопоставляем клиента."""
        return f"{config.xui_email_prefix}{tg_id}"

    async def _get_inbound(self) -> dict:
        payload = await self._request("GET", f"/panel/api/inbounds/get/{self._inbound_id}")
        obj = payload.get("obj")
        if not obj:
            raise ThreeXUIError(f"Инбаунд id={self._inbound_id} не найден в 3x-ui")
        return obj

    async def _get_client_by_email(self, email: str) -> dict | None:
        inbound = await self._get_inbound()
        settings = json.loads(inbound.get("settings") or "{}")
        for client in settings.get("clients", []):
            if client.get("email") == email:
                return client
        return None

    def _client_link(self, sub_id: str) -> str:
        base = config.xui_sub_base_url.rstrip("/")
        return f"{base}/{sub_id}"

    async def create_client(self, email: str, expire_at: dt.datetime) -> dict:
        """
        Создаёт клиента в выбранном инбаунде.

        Поля клиента заданы в общем виде под протокол VLESS; если ваш
        инбаунд использует VMess/Trojan/Shadowsocks — поправьте набор
        полей под соответствующий протокол (см. вкладку "Client" в 3x-ui).
        """
        client_uuid = str(uuid_lib.uuid4())
        sub_id = secrets.token_hex(8)

        client = {
            "id": client_uuid,
            "email": email,
            "enable": True,
            "expiryTime": int(expire_at.timestamp() * 1000),
            "limitIp": 0,
            "totalGB": 0,
            "flow": "",
            "subId": sub_id,
            "tgId": "",
        }
        body = {
            "id": self._inbound_id,
            "settings": json.dumps({"clients": [client]}),
        }
        await self._request("POST", "/panel/api/inbounds/addClient", json_body=body)
        return {"uuid": client_uuid, "email": email, "sub_id": sub_id, "subscription_url": self._client_link(sub_id)}

    async def update_client_expiry(self, client_uuid: str, existing_client: dict, expire_at: dt.datetime) -> dict:
        updated = dict(existing_client)
        updated["expiryTime"] = int(expire_at.timestamp() * 1000)
        updated["enable"] = True

        body = {
            "id": self._inbound_id,
            "settings": json.dumps({"clients": [updated]}),
        }
        await self._request("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json_body=body)
        sub_id = updated.get("subId", "")
        return {
            "uuid": client_uuid,
            "email": updated.get("email"),
            "sub_id": sub_id,
            "subscription_url": self._client_link(sub_id) if sub_id else None,
        }

    async def disable_client(self, client_uuid: str, existing_client: dict) -> None:
        updated = dict(existing_client)
        updated["enable"] = False

        body = {
            "id": self._inbound_id,
            "settings": json.dumps({"clients": [updated]}),
        }
        await self._request("POST", f"/panel/api/inbounds/updateClient/{client_uuid}", json_body=body)

    async def delete_client(self, client_uuid: str) -> None:
        await self._request("POST", f"/panel/api/inbounds/{self._inbound_id}/delClient/{client_uuid}")

    async def ensure_client(self, tg_id: int, expire_at: dt.datetime) -> dict:
        """Создаёт клиента, если его ещё нет в инбаунде, иначе продлевает срок."""
        email = self.build_email(tg_id)
        existing = await self._get_client_by_email(email)
        if existing is None:
            return await self.create_client(email, expire_at)
        return await self.update_client_expiry(existing["id"], existing, expire_at)

    async def disable_by_email(self, email: str) -> None:
        existing = await self._get_client_by_email(email)
        if existing is None:
            return
        await self.disable_client(existing["id"], existing)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


threexui_client = ThreeXUIClient()
