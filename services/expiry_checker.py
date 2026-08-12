"""
Фоновая задача: раз в день проверяет подписки.
- если истекла - отключает клиента у VPN-провайдера;
- если истекает в ближайшие 3 дня - шлёт напоминание в Telegram.
"""

import datetime as dt
import logging

from aiogram import Bot

from database.db import all_active_subscriptions, async_session
from database.models import User
from services.vpn_provider import vpn_client
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def check_expirations(bot: Bot) -> None:
    now = dt.datetime.utcnow()
    soon = now + dt.timedelta(days=3)

    async with async_session() as session:
        subs = await all_active_subscriptions(session)
        for sub in subs:
            result = await session.execute(select(User).where(User.id == sub.user_id))
            user = result.scalar_one_or_none()
            if user is None:
                continue

            if sub.expires_at <= now:
                try:
                    await vpn_client.disable_by_email(vpn_client.build_email(user.tg_id))
                except Exception:
                    logger.exception("Не удалось отключить клиента %s у VPN-провайдера", user.tg_id)
                continue

            if now < sub.expires_at <= soon:
                try:
                    await bot.send_message(
                        user.tg_id,
                        f"⏰ Ваша подписка истекает {sub.expires_at.strftime('%Y-%m-%d')}. "
                        "Продлите её через меню, чтобы не потерять доступ.",
                    )
                except Exception:
                    logger.exception("Не удалось отправить напоминание пользователю %s", user.tg_id)
