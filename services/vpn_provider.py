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

        В протоколе мастер-сервера этот параметр называется MAX_DEVICE —
        именно под этим именем его нужно будет положить в payload запроса,
        когда интеграция будет готова, например:
            payload = {..., "MAX_DEVICE": device_limit}
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
        доп. устройств). Пока мастер-сервер не подключён — ничего не делает.

        Как и в ensure_client — поле в протоколе мастер-сервера называется
        MAX_DEVICE, например: payload = {"MAX_DEVICE": device_limit}.
        """
        return None

    async def disable_by_email(self, email: str) -> None:
        """ЗАГЛУШКА. Отключение клиента по истечении подписки — пока ничего не делает."""
        return None

    async def close(self) -> None:
        return None


vpn_client = VpnProviderClient()
