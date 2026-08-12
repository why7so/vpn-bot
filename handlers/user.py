import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import PLANS, config
from database.db import (
    PromoError,
    adjust_balance,
    async_session,
    consume_discount_use,
    create_invoice,
    create_login_token,
    get_effective_discount,
    get_invoice_by_invoice_id,
    get_or_create_user,
    get_subscription,
    mark_invoice_paid,
    redeem_promo_code,
    set_user_discount,
    set_vpn_client_uuid,
    upsert_subscription,
)
from keyboards.keyboards import (
    back_main_kb,
    invoice_kb,
    main_menu_kb,
    payment_method_kb,
    plans_kb,
    promo_result_kb,
)
from services.cryptobot_api import cryptopay_client
from services.platega_api import platega_client
from services.vpn_provider import VpnProviderError, vpn_client

router = Router(name="user")
logger = logging.getLogger(__name__)


def _profile_text(user, sub) -> str:
    if sub is not None:
        sub_line = sub.subscription_url or "будет выдана после оплаты"
        expiry_line = sub.expires_at.strftime("%d.%m.%Y, %H:%M UTC")
    else:
        sub_line = "будет выдана после оплаты"
        expiry_line = "нет активной подписки"

    name = user.username and f"@{user.username}" or str(user.tg_id)

    discount_line = ""
    if user.discount_percent > 0:
        uses_text = f", осталось использований: {user.discount_uses_left}" if user.discount_uses_left is not None else ""
        expires_text = f", до {user.discount_expires_at.strftime('%d.%m.%Y')}" if user.discount_expires_at else ""
        discount_line = f"🏷 Скидка: {user.discount_percent:.0f}%{uses_text}{expires_text}\n"

    return (
        f"👋 Добро пожаловать в <b>{config.vpn_name}</b>!\n\n"
        "👤 <b>Профиль:</b>\n"
        f"📝 Имя: {name}\n"
        f"🆔 ID: <code>{user.tg_id}</code>\n"
        f"💳 Баланс: {user.balance:.0f} ₽\n"
        f"{discount_line}\n"
        "🔑 <b>Ваша подписка:</b>\n"
        f"{sub_line}\n\n"
        f"📅 <b>Срок действия:</b> {expiry_line}\n\n"
        "💡 <i>Используйте кнопки ниже для управления подпиской.</i>"
    )


async def _render_profile(session, tg_id: int, username: str | None):
    user = await get_or_create_user(session, tg_id, username)
    sub = await get_subscription(session, user.id)
    return user, sub


async def _issue_browser_login_link(user_id: int, username: str | None) -> str:
    """Выдаёт одноразовую ссылку для входа в веб-версию личного кабинета
    в обычном браузере (см. /api/browser-login в webapp/api.py)."""
    async with async_session() as session:
        token = await create_login_token(session, user_id, username)
    return f"{config.webapp_url.rstrip('/')}?login_token={token}"


def _browser_login_kb(login_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🌐 Открыть в браузере", url=login_url)]]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    payload = (command.args or "").strip()

    # Ссылка "Войти через Telegram" на сайте ведёт на t.me/<bot>?start=weblogin —
    # выдаём одноразовую ссылку для входа в веб-версию в обычном браузере
    # (не Mini App). Отдельная ветка, не смешивается с активацией промокодов.
    if payload == "weblogin":
        if not config.webapp_url:
            await message.answer("Веб-версия личного кабинета пока не настроена.")
            return
        login_url = await _issue_browser_login_link(message.from_user.id, message.from_user.username)
        await message.answer(
            "🌐 Ссылка для входа в личный кабинет в браузере (одноразовая, "
            "действует 5 минут):",
            reply_markup=_browser_login_kb(login_url),
        )
        return

    # Кнопка "Активировать" у промокодов теперь ведёт на deep-link
    # https://t.me/<bot>?start=promo_<CODE> — это работает даже для тех, кто
    # никогда раньше не писал боту (в отличие от обычной callback-кнопки,
    # которую Telegram в таком случае просто не доставляет и открывает чат
    # с ботом без активации). Если параметр промокода есть — сразу активируем.
    promo_text = None
    if payload.startswith("promo_"):
        code = payload[len("promo_"):]
        try:
            promo_text = await _redeem_promo_for_user(message.from_user.id, message.from_user.username, code)
        except PromoError as e:
            promo_text = f"❌ {e}"
        except VpnProviderError:
            logger.exception("VPN provider error while redeeming promo (deep-link) for tg_id=%s", message.from_user.id)
            promo_text = (
                "Промокод принят, но не удалось выдать доступ из-за ошибки VPN-панели. Напишите в поддержку."
            )
        except Exception:
            logger.exception("Unexpected error while redeeming promo (deep-link) for tg_id=%s", message.from_user.id)
            promo_text = "Произошла непредвиденная ошибка при активации промокода. Попробуйте позже."

    async with async_session() as session:
        user, sub = await _render_profile(session, message.from_user.id, message.from_user.username)

    # Результат активации промокода теперь не отправляется отдельным
    # сообщением в чат — вместо этого он передаётся мини-приложению через
    # URL кнопки, и приложение показывает его всплывающим окном
    # (Telegram.WebApp.showPopup) сразу после открытия. Если мини-приложение
    # не настроено (нет WEBAPP_URL) — некуда показывать popup, тогда как
    # раньше отправляем текст обычным сообщением.
    if promo_text and config.webapp_url:
        keyboard = promo_result_kb(_shorten_for_popup(promo_text))
    else:
        if promo_text:
            await message.answer(promo_text)
        keyboard = main_menu_kb()

    await message.answer(_profile_text(user, sub), reply_markup=keyboard)


@router.callback_query(F.data == "weblogin")
async def weblogin_button(callback: CallbackQuery) -> None:
    if not config.webapp_url:
        await callback.answer("Веб-версия личного кабинета пока не настроена.", show_alert=True)
        return
    login_url = await _issue_browser_login_link(callback.from_user.id, callback.from_user.username)
    await callback.message.answer(
        "🌐 Ссылка для входа в личный кабинет в браузере (одноразовая, действует 5 минут):",
        reply_markup=_browser_login_kb(login_url),
    )
    await callback.answer()


@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user, sub = await _render_profile(session, callback.from_user.id, callback.from_user.username)

    await callback.message.edit_text(_profile_text(user, sub), reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "buy")
async def show_plans(callback: CallbackQuery) -> None:
    await callback.message.edit_text("Выберите тарифный план:", reply_markup=plans_kb())
    await callback.answer()


@router.callback_query(F.data == "connect_device")
async def connect_device(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user, sub = await _render_profile(session, callback.from_user.id, callback.from_user.username)

    if sub is None or not sub.subscription_url:
        text = "У вас пока нет активной подписки — сначала оформите её через «Продлить подписку»."
    else:
        text = (
            "📱 <b>Подключение устройства</b>\n\n"
            f"Ссылка-подписка:\n<code>{sub.subscription_url}</code>\n\n"
            "1. Установите клиент: V2rayNG (Android), Streisand / Shadowrocket (iOS), "
            "Hiddify (Windows/macOS/Linux/Android/iOS)\n"
            "2. В клиенте выберите «Добавить по ссылке-подписке» и вставьте ссылку выше\n"
            "3. Обновите список серверов и подключайтесь"
        )

    await callback.message.edit_text(text, reply_markup=back_main_kb())
    await callback.answer()


def _shorten_for_popup(text: str) -> str:
    """Готовит текст результата промокода для Telegram.WebApp.showPopup.

    showPopup ограничен 256 символами, а ссылка-подписка и так видна в
    приложении на главном экране — поэтому обрезаем текст на строке с ней
    (если она есть) и подстраховываемся на случай других длинных текстов.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.startswith("Ваша ссылка-подписка"):
            lines = lines[:i]
            break
    result = "\n".join(lines).rstrip()
    if len(result) > 250:
        result = result[:247].rstrip() + "..."
    return result


async def _redeem_promo_for_user(user_id: int, username: str | None, code: str) -> str:
    """Активирует промокод и возвращает готовый текст ответа. Бросает PromoError/VpnProviderError."""
    async with async_session() as session:
        user = await get_or_create_user(session, user_id, username)
        promo = await redeem_promo_code(session, code, user)

        if promo.type == "balance":
            user = await adjust_balance(session, user, promo.value)
            text = (
                f"✅ Промокод активирован!\n"
                f"Начислено на баланс: {promo.value:.0f} ₽\n"
                f"Текущий баланс: {user.balance:.0f} ₽"
            )
        elif promo.type == "days":
            subscription_url = await _grant_subscription(
                session, user_id, username, "promo", int(promo.value)
            )
            text = (
                f"✅ Промокод активирован!\n"
                f"Начислено дней подписки: {int(promo.value)}\n\n"
                f"Ваша ссылка-подписка:\n{subscription_url}"
            )
        elif promo.type == "discount":
            user = await set_user_discount(
                session, user, promo.value, promo.discount_uses, promo.discount_valid_days
            )
            uses_text = f"{promo.discount_uses} раз(а)" if promo.discount_uses else "без ограничения по количеству"
            valid_text = f"{promo.discount_valid_days} дней" if promo.discount_valid_days else "бессрочно"
            text = (
                f"✅ Промокод активирован!\n"
                f"Скидка {promo.value:.0f}% на покупку/продление подписки.\n"
                f"Действует: {uses_text}, срок: {valid_text}."
            )
        else:  # partner
            subscription_url = await _grant_subscription(
                session, user_id, username, "promo", int(promo.value)
            )
            user = await set_user_discount(
                session, user, promo.extra_value or 0, promo.discount_uses, promo.discount_valid_days
            )
            text = (
                f"✅ Партнёрский промокод активирован!\n"
                f"🎁 Пробный период: {int(promo.value)} дней (доступ уже выдан)\n"
                f"🏷 Скидка на покупки: {promo.extra_value:.0f}%\n\n"
                f"Ваша ссылка-подписка:\n{subscription_url}"
            )
    return text


@router.callback_query(F.data.startswith("promo_redeem:"))
async def promo_redeem_button(callback: CallbackQuery, bot: Bot) -> None:
    code = callback.data.split(":", 1)[1]

    try:
        text = await _redeem_promo_for_user(callback.from_user.id, callback.from_user.username, code)
    except PromoError as e:
        await callback.answer(str(e), show_alert=True)
        return
    except VpnProviderError:
        logger.exception("VPN provider error while redeeming promo (button) for tg_id=%s", callback.from_user.id)
        await callback.answer(
            "Промокод принят, но не удалось выдать доступ из-за ошибки VPN-панели. Напишите в поддержку.",
            show_alert=True,
        )
        return
    except Exception:
        logger.exception("Unexpected error while redeeming promo (button) for tg_id=%s", callback.from_user.id)
        await callback.answer(
            "Произошла непредвиденная ошибка при активации промокода. Попробуйте позже.",
            show_alert=True,
        )
        return

    # Сообщения, отправленные через inline-режим в чат, где бота нет как участника
    # (например, личка между двумя обычными пользователями), не имеют обычного
    # message_id — доступен только inline_message_id.
    if callback.message is not None:
        await callback.message.edit_text(text, reply_markup=main_menu_kb())
    elif callback.inline_message_id:
        await bot.edit_message_text(
            text, inline_message_id=callback.inline_message_id, reply_markup=main_menu_kb()
        )
    await callback.answer()


def _apply_discount(plan: dict, discount_percent: float) -> tuple[float, float]:
    """Возвращает (price_usdt, price_rub) с учётом скидки в %."""
    if discount_percent <= 0:
        return plan["price_usdt"], plan["price_rub"]
    factor = max(0.0, 1 - discount_percent / 100)
    price_usdt = round(plan["price_usdt"] * factor, 2)
    price_rub = round(plan["price_rub"] * factor)
    return price_usdt, price_rub


@router.callback_query(F.data.startswith("plan:"))
async def choose_plan(callback: CallbackQuery) -> None:
    plan_code = callback.data.split(":", 1)[1]
    plan = next((p for p in PLANS if p["code"] == plan_code), None)
    if plan is None:
        await callback.answer("Такого плана не существует", show_alert=True)
        return

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        discount = await get_effective_discount(session, user)

    price_usdt, price_rub = _apply_discount(plan, discount)
    is_free = price_rub <= 0
    balance_enough = (not is_free) and user.balance >= price_rub

    price_lines = f"Крипта: {plan['price_usdt']} USDT · Рубли: {plan['price_rub']}₽"
    if discount > 0:
        price_lines = (
            f"<s>{plan['price_usdt']} USDT / {plan['price_rub']}₽</s> → "
            f"<b>{price_usdt} USDT / {price_rub}₽</b> (скидка {discount:.0f}%)"
        )

    await callback.message.edit_text(
        f"Тариф: {plan['title']}\n"
        f"{price_lines}\n"
        f"Ваш баланс: {user.balance:.0f} ₽\n\n"
        + ("Скидка полностью покрывает стоимость 🎉" if is_free else "Выберите способ оплаты:"),
        reply_markup=payment_method_kb(plan_code, balance_enough, f"{price_rub:.0f}₽", is_free=is_free),
    )
    await callback.answer()


async def _grant_subscription(session, tg_id: int, username: str | None, plan_code: str, days: int) -> str | None:
    user = await get_or_create_user(session, tg_id, username)
    sub = await get_subscription(session, user.id)
    now = dt.datetime.utcnow()
    base = sub.expires_at if (sub and sub.expires_at > now) else now
    new_expire = base + dt.timedelta(days=days)

    vpn_result = await vpn_client.ensure_client(user.tg_id, new_expire)
    subscription_url = vpn_result.get("subscription_url")
    await set_vpn_client_uuid(session, user, vpn_result["uuid"])

    await upsert_subscription(
        session,
        user_id=user.id,
        plan_code=plan_code,
        days=days,
        subscription_url=subscription_url,
    )
    return subscription_url


@router.callback_query(F.data.startswith("paymethod:"))
async def choose_payment_method(callback: CallbackQuery) -> None:
    _, plan_code, provider = callback.data.split(":", 2)
    plan = next((p for p in PLANS if p["code"] == plan_code), None)
    if plan is None:
        await callback.answer("Такого плана не существует", show_alert=True)
        return

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        discount = await get_effective_discount(session, user)
    price_usdt, price_rub = _apply_discount(plan, discount)

    if provider == "free":
        if price_rub > 0:
            await callback.answer("Скидка недостаточна для бесплатной активации", show_alert=True)
            return
        try:
            async with async_session() as session:
                user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
                subscription_url = await _grant_subscription(
                    session, callback.from_user.id, callback.from_user.username, plan_code, plan["days"]
                )
                if discount > 0:
                    await consume_discount_use(session, user)
        except VpnProviderError:
            logger.exception("VPN provider error while granting free subscription for tg_id=%s", callback.from_user.id)
            await callback.answer(
                "Не удалось выдать доступ: ошибка связи с VPN-панелью. Попробуйте позже или напишите в поддержку.",
                show_alert=True,
            )
            return
        except Exception:
            logger.exception("Unexpected error while granting free subscription for tg_id=%s", callback.from_user.id)
            await callback.answer(
                "Произошла непредвиденная ошибка. Попробуйте позже или напишите в поддержку.",
                show_alert=True,
            )
            return

        await callback.message.edit_text(
            "✅ Скидка 100% — доступ выдан бесплатно!\n\n"
            f"Ваша ссылка-подписка (добавьте в клиент V2rayNG / Streisand / Hiddify):\n{subscription_url}",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    if provider == "balance":
        async with async_session() as session:
            user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
            if user.balance < price_rub:
                await callback.answer("Недостаточно средств на балансе", show_alert=True)
                return

            await adjust_balance(session, user, -price_rub)
            try:
                subscription_url = await _grant_subscription(
                    session, callback.from_user.id, callback.from_user.username, plan_code, plan["days"]
                )
            except VpnProviderError:
                logger.exception("VPN provider error while granting balance subscription for tg_id=%s", callback.from_user.id)
                await adjust_balance(session, user, price_rub)  # откатываем списание
                await callback.answer(
                    "Не удалось выдать доступ: ошибка связи с VPN-панелью. Средства не списаны, попробуйте позже.",
                    show_alert=True,
                )
                return
            except Exception:
                logger.exception("Unexpected error while granting balance subscription for tg_id=%s", callback.from_user.id)
                await adjust_balance(session, user, price_rub)  # откатываем списание
                await callback.answer(
                    "Произошла непредвиденная ошибка. Средства не списаны, попробуйте позже.",
                    show_alert=True,
                )
                return
            if discount > 0:
                await consume_discount_use(session, user)

        await callback.message.edit_text(
            "✅ Оплата с баланса прошла успешно, доступ выдан!\n\n"
            f"Ваша ссылка-подписка (добавьте в клиент V2rayNG / Streisand / Hiddify):\n{subscription_url}",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)

        if provider == "platega":
            invoice = await platega_client.create_invoice(
                amount_rub=price_rub,
                comment=f"{config.vpn_name}: подписка {plan['title']}",
                order_id=f"user{user.tg_id}-{plan_code}-{int(dt.datetime.utcnow().timestamp())}",
            )
            await create_invoice(
                session,
                user_id=user.id,
                plan_code=plan_code,
                invoice_id=str(invoice["invoice_id"]),
                pay_url=invoice["pay_url"],
                amount=price_rub,
                purpose="subscription",
                provider="platega",
                currency="RUB",
                discount_percent=discount,
            )
            amount_text = f"{price_rub} ₽"
        else:
            invoice = await cryptopay_client.create_invoice(
                amount_usdt=price_usdt,
                description=f"{config.vpn_name}: подписка {plan['title']}",
                payload=f"user:{user.tg_id}:plan:{plan_code}",
            )
            await create_invoice(
                session,
                user_id=user.id,
                plan_code=plan_code,
                invoice_id=str(invoice["invoice_id"]),
                pay_url=invoice["pay_url"],
                amount=price_usdt,
                purpose="subscription",
                provider="cryptobot",
                currency="USDT",
                discount_percent=discount,
            )
            amount_text = f"{price_usdt} USDT"

    await callback.message.edit_text(
        f"Тариф: {plan['title']}\nСумма: {amount_text}\n\n"
        "Нажмите «Оплатить», после оплаты вернитесь и нажмите «Я оплатил».",
        reply_markup=invoice_kb(invoice["pay_url"], str(invoice["invoice_id"]), provider=provider),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("check:"))
async def check_payment(callback: CallbackQuery) -> None:
    invoice_id = callback.data.split(":", 1)[1]

    async with async_session() as session:
        local_invoice = await get_invoice_by_invoice_id(session, invoice_id)
        if local_invoice is None:
            await callback.answer("Счёт не найден", show_alert=True)
            return

        if local_invoice.status == "paid":
            await callback.answer("Этот счёт уже был оплачен ранее ✅", show_alert=True)
            return

        if local_invoice.provider == "platega":
            remote_invoice = await platega_client.get_invoice(invoice_id)
        else:
            remote_invoice = await cryptopay_client.get_invoice(invoice_id)

        if remote_invoice is None or remote_invoice.get("status") != "paid":
            await callback.answer("Оплата пока не найдена, попробуйте чуть позже.", show_alert=True)
            return

        if local_invoice.purpose == "topup":
            user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
            user = await adjust_balance(session, user, local_invoice.amount)
            await mark_invoice_paid(session, local_invoice)

            await callback.message.edit_text(
                f"✅ Баланс пополнен на {local_invoice.amount:.0f} ₽\n"
                f"Текущий баланс: {user.balance:.0f} ₽",
                reply_markup=main_menu_kb(),
            )
            await callback.answer()
            return

        # оплата подтверждена -> продлеваем/создаём клиента у VPN-провайдера
        plan = next((p for p in PLANS if p["code"] == local_invoice.plan_code), None)
        days = plan["days"] if plan else 30

        try:
            subscription_url = await _grant_subscription(
                session, callback.from_user.id, callback.from_user.username, local_invoice.plan_code, days
            )
        except VpnProviderError:
            logger.exception("VPN provider error while granting paid subscription for tg_id=%s", callback.from_user.id)
            await callback.answer(
                "Оплата найдена, но не удалось выдать доступ из-за ошибки VPN-панели. Напишите в поддержку — доступ выдадим вручную.",
                show_alert=True,
            )
            return
        except Exception:
            logger.exception("Unexpected error while granting paid subscription for tg_id=%s", callback.from_user.id)
            await callback.answer(
                "Оплата найдена, но произошла непредвиденная ошибка при выдаче доступа. Напишите в поддержку.",
                show_alert=True,
            )
            return
        if local_invoice.discount_percent > 0:
            user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
            await consume_discount_use(session, user)
        await mark_invoice_paid(session, local_invoice)

    await callback.message.edit_text(
        "✅ Оплата подтверждена, доступ выдан!\n\n"
        f"Ваша ссылка-подписка (добавьте в клиент V2rayNG / Streisand / Hiddify):\n{subscription_url}",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()
