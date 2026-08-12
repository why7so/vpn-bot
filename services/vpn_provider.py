"""
Клиент выдачи VPN-доступа.

Раньше здесь была интеграция с панелью 3x-ui — сейчас она отключена.
Вместо неё планируется подключение к собственному мастер-серверу (он будет
сам распределять клиентов по воркер-нодам). Пока мастер-сервер не готов,
этот клиент — ЗАГЛУШКА: он ничего никуда не отправляет и просто возвращает
фиктивную подписку, чтобы остальная логика бота (оплата, продление,
промокоды, списание баланса, тексты сообщений и т.д.) продолжала работать
без изменений и без ошибок.

Когда мастер-сервер будет готов:
  1. Заполните MASTER_API_BASE_URL / MASTER_API_TOKEN в .env
  2. Замените тело ensure_client()/disable_by_email() на реальные HTTP-запросы
     к мастер-серверу (в духе aiohttp.ClientSession, как это было в
     services/threexui_api.py — можно взять за основу общий каркас запросов
     с логином/ретраями)
  3. Уберите пометки "ЗАГЛУШКА" из докстрингов ниже
"""

from __future__ import annotations

import datetime as dt
import secrets

from config import config


class VpnProviderError(RuntimeError):
    pass


class VpnProviderClient:
    def __init__(self) -> None:
        # Зарезервировано для будущей интеграции с мастер-сервером.
        self._base_url = config.master_api_base_url
        self._token = config.master_api_token

    @staticmethod
    def build_email(tg_id: int) -> str:
        """Идентификатор клиента у провайдера — по нему находим/сопоставляем клиента."""
        return f"{config.vpn_email_prefix}{tg_id}"

    async def ensure_client(self, tg_id: int, expire_at: dt.datetime) -> dict:
        """
        ЗАГЛУШКА. Создаёт клиента, если его ещё нет, иначе продлевает срок.

        Пока мастер-сервер не подключён — просто выдаёт фиктивную подписку
        (subscription_url-заглушку), ничего никуда не отправляя.
        """
        sub_id = secrets.token_hex(8)
        email = self.build_email(tg_id)
        return {
            "uuid": f"stub-{sub_id}",
            "email": email,
            "sub_id": sub_id,
            "subscription_url": "https://example.com/stub-subscription-not-connected-yet",
        }

    async def disable_by_email(self, email: str) -> None:
        """ЗАГЛУШКА. Отключение клиента по истечении подписки — пока ничего не делает."""
        return None

    async def close(self) -> None:
        return None


vpn_client = VpnProviderClient()
