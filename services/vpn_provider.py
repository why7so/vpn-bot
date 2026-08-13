"""
Клиент выдачи VPN-доступа.

Интеграция с 3x-ui из бота полностью удалена. Выдача доступа планируется
через собственный мастер-сервер (он будет сам распределять клиентов по
воркер-нодам). Пока мастер-сервер не готов, этот клиент — ЗАГЛУШКА: он
ничего никуда не отправляет и просто возвращает фиктивную подписку, чтобы
остальная логика бота (оплата, продление, промокоды, списание баланса,
тексты сообщений и т.д.) продолжала работать без изменений и без ошибок.

Когда мастер-сервер будет готов:
  1. Заполните MASTER_API_BASE_URL / MASTER_API_TOKEN в .env
  2. Замените тело ensure_client()/disable_by_email()/update_device_limit() на
     реальные HTTP-запросы к мастер-серверу (aiohttp.ClientSession, обработка
     ошибок сети, при необходимости — логин/ретраи, как в других
     services/*_api.py)
  3. ensure_client() уже принимает device_limit — передавайте его мастер-серверу
     при создании/продлении клиента (см. handlers/user.py:_grant_subscription,
     где он берётся через database.db.effective_device_limit)
  4. update_device_limit() нужен, чтобы применять докупленные устройства сразу,
     не дожидаясь следующего продления подписки (см. "Докупить устройства"
     в handlers/user.py и webapp/api.py)
  5. Уберите пометки "ЗАГЛУШКА" из докстрингов ниже
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

    async def ensure_client(self, tg_id: int, expire_at: dt.datetime, device_limit: int = 0) -> dict:
        """
        ЗАГЛУШКА. Создаёт клиента, если его ещё нет, иначе продлевает срок.

        device_limit — лимит одновременных подключений (0 = без ограничений),
        см. database.db.effective_device_limit(). Пока мастер-сервер не
        подключён, параметр принимается, но никак не используется — просто
        выдаётся фиктивная подписка (subscription_url-заглушка), ничего
        никуда не отправляя.
        """
        sub_id = secrets.token_hex(8)
        email = self.build_email(tg_id)
        return {
            "uuid": f"stub-{sub_id}",
            "email": email,
            "sub_id": sub_id,
            "subscription_url": "https://example.com/stub-subscription-not-connected-yet",
        }

    async def update_device_limit(self, tg_id: int, device_limit: int) -> None:
        """ЗАГЛУШКА. Обновляет лимит устройств уже созданного клиента, не
        дожидаясь следующего продления подписки (напр. сразу после покупки
        доп. устройств). Пока мастер-сервер не подключён — ничего не делает."""
        return None

    async def disable_by_email(self, email: str) -> None:
        """ЗАГЛУШКА. Отключение клиента по истечении подписки — пока ничего не делает."""
        return None

    async def close(self) -> None:
        return None


vpn_client = VpnProviderClient()
