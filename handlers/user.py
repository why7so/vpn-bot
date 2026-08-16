import datetime as dt
import logging
from html import escape as html_escape
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import DEVICE_QTY_PRESETS, build_subscription_url, config
from database.db import (
    PromoError,
    add_extra_devices,
    adjust_balance,
    async_session,
    consume_discount_use,
    create_invoice,
    create_login_token,
    effective_device_limit,
    ensure_sub_token,
    get_effective_discount,
    get_invoice_by_invoice_id,
    get_or_create_user,
    get_plan,
    get_plans,
    get_referral_count,
    get_setting,
    get_subscription,
    get_top_referrers,
    get_user_by_tg_id,
    LEADERBOARD_RESET_SETTING_KEY,
    mark_invoice_paid,
    redeem_promo_code,
    register_referral_if_new,
    remaining_device_capacity,
    set_login_token_message,
    set_user_discount,
    set_vpn_client_uuid,
    upsert_subscription,
)
from keyboards.keyboards import (
    about_kb,
    back_main_kb,
    device_payment_method_kb,
    devices_kb,
    invoice_kb,
    main_menu_kb,
    payment_method_kb,
    plans_kb,
    promo_result_kb,
)
from services.cryptobot_api import cryptopay_client
from services.platega_api import PlategaError, platega_client
from services.vpn_provider import VpnProviderError, vpn_client

router = Router(name="user")
logger = logging.getLogger(__name__)

# Баннер для /start (assets/start_banner.png в корне репозитория).
START_BANNER_PATH = Path(__file__).resolve().parent.parent / "assets" / "start_banner.png"


async def _edit_message(callback: CallbackQuery, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Замена callback.message.edit_text() для навигации по меню.

    Главное меню теперь отправляется как фото с подписью (см. cmd_start и
    assets/start_banner.png) — у такого сообщения нет .text, только
    .caption, и Telegram отклоняет edit_text на нём с ошибкой (кнопки
    "Тарифы и оплата", "О сервисе" и другие переходы из главного меню
    молча переставали работать). Этот хелпер сам выбирает edit_caption
    для фото-сообщений и edit_text для обычных текстовых.
    """
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=reply_markup)
    else:
        await callback.message.edit_text(text, reply_markup=reply_markup)


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
        f'<tg-emoji emoji-id="6041921818896372382">👋</tg-emoji> Добро пожаловать в <b>{config.vpn_name}</b>!\n\n'
        '<tg-emoji emoji-id="6032609071373226027">👤</tg-emoji> <b>Профиль:</b>\n'
        "<blockquote>"
        f'<tg-emoji emoji-id="5895519358871932592">📝</tg-emoji> Имя: {name}\n'
        f'<tg-emoji emoji-id="5766975922620076409">🆔</tg-emoji> ID: <code>{user.tg_id}</code>\n'
        f'<tg-emoji emoji-id="5769126056262898415">💳</tg-emoji> Баланс: {user.balance:.0f} ₽\n'
        f"{discount_line}</blockquote>\n\n"
        '<tg-emoji emoji-id="5766902139376898645">🔑</tg-emoji> <b>Ваша подписка:</b>\n'
        f"<blockquote>{sub_line}</blockquote>\n\n"
        "Platega test"
        f'<tg-emoji emoji-id="5805331990618053402">📅</tg-emoji> <b>Срок действия:</b> {expiry_line}\n\n'
    )


async def _render_profile(session, tg_id: int, username: str | None, first_name: str | None = None):
    user = await get_or_create_user(session, tg_id, username, first_name)
    sub = await get_subscription(session, user.id)
    return user, sub


async def _issue_browser_login_link(user_id: int, username: str | None) -> tuple[str, str]:
    """Выдаёт одноразовую ссылку для входа в веб-версию личного кабинета
    в обычном браузере (см. /api/browser-login в webapp/api.py).
    Возвращает (token, login_url) — token нужен, чтобы потом привязать
    к нему отправленное сообщение (см. set_login_token_message)."""
    async with async_session() as session:
        token = await create_login_token(session, user_id, username)
    login_url = f"{config.webapp_url.rstrip('/')}?login_token={token}"
    return token, login_url


def _browser_login_kb(login_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть в браузере", url=login_url)]]
    )


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    payload = (command.args or "").strip()

    # Приветственный баннер — отдельным сообщением перед остальным текстом.
    # Приветственный баннер. Раньше отправлялся отдельным сообщением, теперь —
    # если файл на месте, объединяем его с текстом профиля в одно
    # photo+caption сообщение ниже. Если баннер отсутствует на диске
    # (например, не задеплоили assets/) — просто не показываем его, не роняя
    # /start целиком.
    has_banner = START_BANNER_PATH.exists()

    # Определяем "первый ли это /start" ДО любых side-effects (создания
    # пользователя реф-обработчиком/get_or_create_user) — на этом флаге
    # завязана выдача бесплатного пробного периода ниже.
    async with async_session() as session:
        is_new_user = (await get_user_by_tg_id(session, message.from_user.id)) is None

    # Ссылка "Войти через Telegram" на сайте ведёт на t.me/<bot>?start=weblogin —
    # выдаём одноразовую ссылку для входа в веб-версию в обычном браузере
    # (не Mini App). Отдельная ветка, не смешивается с активацией промокодов.
    if payload == "weblogin":
        if not config.webapp_url:
            await message.answer("Веб-версия личного кабинета пока не настроена.")
            return
        login_token, login_url = await _issue_browser_login_link(message.from_user.id, message.from_user.username)
        weblogin_text = (
            "🌐 Ссылка для входа в личный кабинет в браузере (одноразовая, "
            "действует 5 минут):"
        )
        if has_banner:
            sent = await message.answer_photo(
                FSInputFile(START_BANNER_PATH), caption=weblogin_text, reply_markup=_browser_login_kb(login_url)
            )
        else:
            sent = await message.answer(weblogin_text, reply_markup=_browser_login_kb(login_url))
        async with async_session() as session:
            await set_login_token_message(session, login_token, sent.chat.id, sent.message_id)
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

    # Реф. ссылка вида https://t.me/<bot>?start=ref_<TG_ID>. Если это первый
    # /start у пользователя (в базе его ещё нет) — засчитываем реферала:
    # рефереру начисляется referral_bonus_rub, самому приглашённому —
    # referral_invitee_bonus_rub (см. register_referral_if_new). Молча
    # игнорируем некорректный payload (битый TG_ID, самоприглашение,
    # несуществующий реферер) — /start не должен падать из-за плохой ссылки.
    referral_text = None
    if payload.startswith("ref_"):
        try:
            referrer_tg_id = int(payload[len("ref_"):])
        except ValueError:
            referrer_tg_id = None
        if referrer_tg_id is not None:
            async with async_session() as session:
                credited = await register_referral_if_new(
                    session, message.from_user.id, referrer_tg_id, message.from_user.first_name
                )
            if credited:
                if config.referral_invitee_bonus_rub > 0:
                    referral_text = (
                        f"🎉 Вы перешли по реферальной ссылке — "
                        f"на баланс начислено {config.referral_invitee_bonus_rub:.0f}₽!"
                    )
                try:
                    await message.bot.send_message(
                        referrer_tg_id,
                        f"🎉 По вашей реферальной ссылке пришёл новый пользователь — "
                        f"на баланс начислено {config.referral_bonus_rub:.0f}₽.",
                    )
                except Exception:
                    logger.warning("Failed to notify referrer tg_id=%s about bonus", referrer_tg_id)

    # Бесплатный пробный период при самом первом /start (config.trial_days,
    # по умолчанию 3 дня; 0 — отключить). Не зависит от промокода/реф-ссылки —
    # они могут применяться одновременно с триалом на одном и том же /start.
    trial_text = None
    if is_new_user and config.trial_days > 0:
        try:
            async with async_session() as session:
                subscription_url = await _grant_subscription(
                    session, message.from_user.id, message.from_user.username, "trial", config.trial_days
                )
            trial_text = (
                f"🎁 Вам начислен бесплатный пробный период — {config.trial_days} дн.!\n"
                f"Ссылка-подписка:\n{subscription_url}"
            )
        except VpnProviderError:
            logger.exception("VPN provider error while granting trial for tg_id=%s", message.from_user.id)
        except Exception:
            logger.exception("Unexpected error while granting trial for tg_id=%s", message.from_user.id)

    async with async_session() as session:
        user, sub = await _render_profile(session, message.from_user.id, message.from_user.username, message.from_user.first_name)

    # Результат активации промокода теперь не отправляется отдельным
    # сообщением в чат — вместо этого он передаётся мини-приложению через
    # URL кнопки, и приложение показывает его всплывающим окном
    # (Telegram.WebApp.showPopup) сразу после открытия. Если мини-приложение
    # не настроено (нет WEBAPP_URL) — некуда показывать popup, тогда как
    # раньше отправляем текст обычным сообщением.
    combined_text = "\n\n".join(t for t in (promo_text, referral_text, trial_text) if t) or None
    if combined_text and config.webapp_url:
        keyboard = promo_result_kb(_shorten_for_popup(combined_text))
    else:
        if combined_text:
            await message.answer(combined_text)
        keyboard = main_menu_kb()

    profile_text = _profile_text(user, sub)

    # Фото + текст одним сообщением, если текст укладывается в лимит подписи
    # Telegram (1024 символа). У caption лимит меньше, чем у обычного текста
    # сообщения (4096) — subscription_url в редких случаях может быть длинным
    # (VLESS/VMESS-ссылки с закодированными параметрами), тогда подпись не
    # влезет и Telegram отклонит запрос. На этот случай — фолбэк на прежнее
    # поведение (фото и текст отдельными сообщениями), чтобы /start не падал.
    if has_banner and len(profile_text) <= 1024:
        try:
            await message.answer_photo(FSInputFile(START_BANNER_PATH), caption=profile_text, reply_markup=keyboard)
            return
        except Exception:
            logger.exception("Failed to send /start banner+caption for tg_id=%s", message.from_user.id)

    if has_banner:
        try:
            await message.answer_photo(FSInputFile(START_BANNER_PATH))
        except Exception:
            logger.exception("Failed to send /start banner for tg_id=%s", message.from_user.id)

    await message.answer(profile_text, reply_markup=keyboard)


@router.callback_query(F.data == "weblogin")
async def weblogin_button(callback: CallbackQuery) -> None:
    if not config.webapp_url:
        await callback.answer("Веб-версия личного кабинета пока не настроена.", show_alert=True)
        return
    login_token, login_url = await _issue_browser_login_link(callback.from_user.id, callback.from_user.username)
    sent = await callback.message.answer(
        "🌐 Ссылка для входа в личный кабинет в браузере (одноразовая, действует 5 минут):",
        reply_markup=_browser_login_kb(login_url),
    )
    async with async_session() as session:
        await set_login_token_message(session, login_token, sent.chat.id, sent.message_id)
    await callback.answer()


@router.callback_query(F.data == "back_main")
async def back_main(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user, sub = await _render_profile(session, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    await _edit_message(callback, _profile_text(user, sub), reply_markup=main_menu_kb())
    await callback.answer()


@router.callback_query(F.data == "about")
async def about_menu(callback: CallbackQuery) -> None:
    """Подменю "О сервисе": поддержка, соглашение, политика конфиденциальности."""
    await _edit_message(callback, f"ℹ️ {config.vpn_name}", reply_markup=about_kb())
    await callback.answer()


@router.callback_query(F.data == "referral")
async def referral_menu(callback: CallbackQuery) -> None:
    bot_username = (await callback.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start=ref_{callback.from_user.id}"

    async with async_session() as session:
        count = await get_referral_count(session, callback.from_user.id)

    text = (
        "🎁 <b>Реферальная программа</b>\n\n"
        f"Приглашайте друзей — за каждого, кто впервые запустит бота по вашей "
        f"ссылке, вам начислится {config.referral_bonus_rub:.0f}₽ на баланс, а "
        f"другу — {config.referral_invitee_bonus_rub:.0f}₽.\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"Приглашено: {count}"
    )
    await _edit_message(callback, text, reply_markup=back_main_kb())
    await callback.answer()


_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


@router.message(Command("leaderboard"))
async def leaderboard(message: Message) -> None:
    """Топ-10 пользователей по числу приглашённых рефералов. Доступно всем —
    не админ-команда. Если админ вызывал /leaderboard_reset — считаем только
    рефералов, присоединившихся после этой точки (см. handlers/admin.py)."""
    async with async_session() as session:
        since_raw = await get_setting(session, LEADERBOARD_RESET_SETTING_KEY)
        since = dt.datetime.fromisoformat(since_raw) if since_raw else None
        top = await get_top_referrers(session, limit=10, since=since)
        my_count = await get_referral_count(session, message.from_user.id, since=since)

    header = "🏆 <b>Топ-10 по рефералам</b>"
    if since is not None:
        header += f"\nСчёт идёт с {since.strftime('%d.%m %H:%M')} UTC"

    if not top:
        await message.answer(f"{header}\n\nПока никто никого не пригласил — станьте первым!")
        return

    lines = [header, ""]
    for i, (user, count) in enumerate(top, start=1):
        place = _MEDALS.get(i, f"{i}.")
        name = html_escape(user.first_name) if user.first_name else "Без имени"
        lines.append(f"{place} {name} (id{user.tg_id}) — {count}")

    lines.append("")
    lines.append(f"Вы пригласили: {my_count}")

    await message.answer("\n".join(lines))


@router.callback_query(F.data == "support_stub")
async def support_stub(callback: CallbackQuery) -> None:
    """Заглушка на случай, если SUPPORT_USERNAME не задан в .env — чтобы
    кнопка поддержки в меню не пропадала молча, а объясняла, что делать."""
    await callback.answer(
        "Тех.поддержка пока не настроена. Администратору: задайте SUPPORT_USERNAME в .env.",
        show_alert=True,
    )


@router.callback_query(F.data == "buy")
async def show_plans(callback: CallbackQuery) -> None:
    async with async_session() as session:
        plans = await get_plans(session)
    await _edit_message(callback, "Выберите тарифный план:", reply_markup=plans_kb(plans))
    await callback.answer()


@router.callback_query(F.data == "connect_device")
async def connect_device(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user, sub = await _render_profile(session, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    if sub is None or not sub.subscription_url:
        text = "У вас пока нет активной подписки — сначала оформите её через «Тарифы и оплата»."
    else:
        limit = effective_device_limit(user)
        limit_line = "без ограничений" if limit <= 0 else f"до {limit} устройств одновременно"
        text = (
            "📱 <b>Подключение устройства</b>\n\n"
            f"Ссылка-подписка:\n<code>{sub.subscription_url}</code>\n\n"
            "1. Установите клиент: V2rayNG (Android), Streisand / Shadowrocket (iOS), "
            "Hiddify (Windows/macOS/Linux/Android/iOS)\n"
            "2. В клиенте выберите «Добавить по ссылке-подписке» и вставьте ссылку выше\n"
            "3. Обновите список серверов и подключайтесь\n\n"
            f"📦 Лимит устройств: {limit_line}."
        )

    await _edit_message(callback, text, reply_markup=back_main_kb())
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
            user = await set_user_discount(
                session, user, promo.value, promo.discount_uses, promo.discount_valid_days
            )
            uses_text = f"{promo.discount_uses} раз(а)" if promo.discount_uses else "без ограничения по количеству"
            valid_text = f"{promo.discount_valid_days} дней" if promo.discount_valid_days else "бессрочно"
            text = (
                f"✅ Партнёрский промокод активирован!\n"
                f"🏷 Скидка {promo.value:.0f}% на покупку/продление подписки.\n"
                f"Действует: {uses_text}, срок: {valid_text}."
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
        await _edit_message(callback, text, reply_markup=main_menu_kb())
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

    async with async_session() as session:
        plan = await get_plan(session, plan_code)
        if plan is None:
            await callback.answer("Такого плана не существует", show_alert=True)
            return
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        discount = await get_effective_discount(session, user)

    price_usdt, price_rub = _apply_discount(plan, discount)
    is_free = price_rub <= 0
    balance_enough = (not is_free) and user.balance >= price_rub

    # Если баланса не хватает на полную оплату, но что-то на нём есть —
    # автоматически спишем эту часть, а через СБП попросим доплатить только
    # остаток (не нужно отдельно пополнять баланс перед покупкой). Крипта
    # (USDT) баланс не примешивает — валюты разные.
    balance_credit = 0.0
    platega_remaining_rub = price_rub
    if not is_free and not balance_enough and user.balance > 0:
        balance_credit = round(min(user.balance, price_rub), 2)
        platega_remaining_rub = round(price_rub - balance_credit)

    price_lines = f"Крипта: {plan['price_usdt']} USDT · Рубли: {plan['price_rub']}₽"
    if discount > 0:
        price_lines = (
            f"<s>{plan['price_usdt']} USDT / {plan['price_rub']}₽</s> → "
            f"<b>{price_usdt} USDT / {price_rub}₽</b> (скидка {discount:.0f}%)"
        )

    balance_note = ""
    if balance_credit > 0:
        balance_note = (
            f"При оплате через СБП спишется {balance_credit:.0f}₽ с баланса — "
            f"доплатить: {platega_remaining_rub:.0f}₽\n"
        )

    await _edit_message(callback, 
        f"Тариф: {plan['title']}\n"
        f"{price_lines}\n"
        f"Ваш баланс: {user.balance:.0f} ₽\n"
        f"{balance_note}\n"
        + ("Скидка полностью покрывает стоимость 🎉" if is_free else "Выберите способ оплаты:"),
        reply_markup=payment_method_kb(
            plan_code,
            balance_enough,
            f"{price_rub:.0f}₽",
            is_free=is_free,
            platega_label=(f"доплатить {platega_remaining_rub:.0f}₽" if balance_credit > 0 else None),
        ),
    )
    await callback.answer()


async def _grant_subscription(session, tg_id: int, username: str | None, plan_code: str, days: int) -> str | None:
    user = await get_or_create_user(session, tg_id, username)
    sub = await get_subscription(session, user.id)
    now = dt.datetime.utcnow()
    base = sub.expires_at if (sub and sub.expires_at > now) else now
    new_expire = base + dt.timedelta(days=days)

    vpn_result = await vpn_client.ensure_client(user.tg_id, new_expire, device_limit=effective_device_limit(user))
    await set_vpn_client_uuid(session, user, vpn_result["uuid"])

    sub_token = await ensure_sub_token(session, user)
    subscription_url = build_subscription_url(sub_token) or vpn_result.get("subscription_url")

    await upsert_subscription(
        session,
        user_id=user.id,
        plan_code=plan_code,
        days=days,
        subscription_url=subscription_url,
    )
    return subscription_url


def _extra_device_price(qty: int) -> tuple[float, float]:
    """Возвращает (price_usdt, price_rub) за qty дополнительных устройств."""
    price_usdt = round(config.extra_device_price_usdt * qty, 2)
    price_rub = round(config.extra_device_price_rub * qty)
    return price_usdt, price_rub


@router.callback_query(F.data == "devices")
async def show_devices(callback: CallbackQuery) -> None:
    async with async_session() as session:
        user, _ = await _render_profile(session, callback.from_user.id, callback.from_user.username, callback.from_user.first_name)

    limit = effective_device_limit(user)
    limit_line = "без ограничений" if limit <= 0 else f"{limit} устройств"
    text = (
        "📦 <b>Докупить устройства</b>\n\n"
        f"Текущий лимит: {limit_line} (базовый лимит: {config.device_limit}, "
        f"докуплено: {user.extra_devices}).\n\n"
        "Дополнительное устройство действует бессрочно, пока активна подписка.\n"
        "Выберите количество:"
    )
    await _edit_message(callback, text, reply_markup=devices_kb())
    await callback.answer()


@router.callback_query(F.data.startswith("devqty:"))
async def choose_device_qty(callback: CallbackQuery) -> None:
    try:
        qty = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer("Некорректное количество", show_alert=True)
        return
    if qty not in DEVICE_QTY_PRESETS:
        await callback.answer("Некорректное количество", show_alert=True)
        return

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)

    capacity = remaining_device_capacity(user)
    if capacity is not None and qty > capacity:
        await callback.answer(
            f"Нельзя купить {qty} — доступно ещё {capacity} из {config.max_device_limit} "
            "(это потолок протокола на одну ссылку-подписку).",
            show_alert=True,
        )
        return

    price_usdt, price_rub = _extra_device_price(qty)
    balance_enough = user.balance >= price_rub

    await _edit_message(callback, 
        f"Дополнительно устройств: {qty}\n"
        f"Крипта: {price_usdt} USDT · Рубли: {price_rub}₽\n"
        f"Ваш баланс: {user.balance:.0f} ₽\n\n"
        "Выберите способ оплаты:",
        reply_markup=device_payment_method_kb(qty, balance_enough, f"{price_rub}₽"),
    )
    await callback.answer()


async def _grant_extra_devices(session, tg_id: int, username: str | None, qty: int) -> int:
    """Начисляет qty доп. устройств и пытается сразу применить новый лимит
    у VPN-провайдера (best-effort — ошибка тут не должна ронять покупку,
    т.к. деньги уже списаны/оплачены; лимит всё равно подтянется при
    следующем продлении подписки через _grant_subscription)."""
    user = await get_or_create_user(session, tg_id, username)
    user = await add_extra_devices(session, user, qty)
    new_limit = effective_device_limit(user)
    try:
        await vpn_client.update_device_limit(user.tg_id, new_limit)
    except Exception:
        logger.exception("Не удалось применить новый лимит устройств сразу для tg_id=%s", tg_id)
    return new_limit


@router.callback_query(F.data.startswith("devpay:"))
async def choose_device_payment(callback: CallbackQuery) -> None:
    _, qty_raw, provider = callback.data.split(":", 2)
    try:
        qty = int(qty_raw)
    except ValueError:
        await callback.answer("Некорректное количество", show_alert=True)
        return
    if qty not in DEVICE_QTY_PRESETS or provider not in ("balance", "cryptobot", "platega"):
        await callback.answer("Некорректный запрос", show_alert=True)
        return

    price_usdt, price_rub = _extra_device_price(qty)

    async with async_session() as session:
        capacity_user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
    capacity = remaining_device_capacity(capacity_user)
    if capacity is not None and qty > capacity:
        await callback.answer(
            f"Нельзя купить {qty} — доступно ещё {capacity} из {config.max_device_limit}.",
            show_alert=True,
        )
        return

    if provider == "balance":
        async with async_session() as session:
            user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
            if user.balance < price_rub:
                await callback.answer("Недостаточно средств на балансе", show_alert=True)
                return
            await adjust_balance(session, user, -price_rub)
            new_limit = await _grant_extra_devices(session, callback.from_user.id, callback.from_user.username, qty)

        await _edit_message(callback, 
            f"✅ Оплата с баланса прошла успешно!\nДобавлено устройств: {qty}\n"
            f"Текущий лимит устройств: {new_limit}",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)

        if provider == "platega":
            try:
                invoice = await platega_client.create_invoice(
                    amount_rub=price_rub,
                    comment=f"{config.vpn_name}: +{qty} устройств",
                    order_id=f"devices-user{user.tg_id}-{qty}-{int(dt.datetime.utcnow().timestamp())}",
                    user_id=user.tg_id,
                    username=callback.from_user.username,
                )
            except PlategaError:
                logger.exception("Platega error while creating devices invoice for tg_id=%s", user.tg_id)
                await callback.answer("Не удалось создать счёт на оплату. Попробуйте позже.", show_alert=True)
                return
            await create_invoice(
                session,
                user_id=user.id,
                invoice_id=str(invoice["invoice_id"]),
                pay_url=invoice["pay_url"],
                amount=price_rub,
                purpose="devices",
                provider="platega",
                currency="RUB",
                quantity=qty,
            )
            amount_text = f"{price_rub} ₽"
        else:
            invoice = await cryptopay_client.create_invoice(
                amount_usdt=price_usdt,
                description=f"{config.vpn_name}: +{qty} устройств",
                payload=f"user:{user.tg_id}:devices:{qty}",
            )
            await create_invoice(
                session,
                user_id=user.id,
                invoice_id=str(invoice["invoice_id"]),
                pay_url=invoice["pay_url"],
                amount=price_usdt,
                purpose="devices",
                provider="cryptobot",
                currency="USDT",
                quantity=qty,
            )
            amount_text = f"{price_usdt} USDT"

    await _edit_message(callback, 
        f"Дополнительно устройств: {qty}\nСумма: {amount_text}\n\n"
        "Нажмите «Оплатить», после оплаты вернитесь и нажмите «Я оплатил».",
        reply_markup=invoice_kb(invoice["pay_url"], str(invoice["invoice_id"]), provider=provider, back_callback="devices"),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("paymethod:"))
async def choose_payment_method(callback: CallbackQuery) -> None:
    _, plan_code, provider = callback.data.split(":", 2)

    async with async_session() as session:
        plan = await get_plan(session, plan_code)
        if plan is None:
            await callback.answer("Такого плана не существует", show_alert=True)
            return
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

        await _edit_message(callback, 
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

        await _edit_message(callback, 
            "✅ Оплата с баланса прошла успешно, доступ выдан!\n\n"
            f"Ваша ссылка-подписка (добавьте в клиент V2rayNG / Streisand / Hiddify):\n{subscription_url}",
            reply_markup=main_menu_kb(),
        )
        await callback.answer()
        return

    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)

        if provider == "platega":
            # Автопримешивание баланса: если на балансе есть деньги (но не
            # хватает на полную оплату — иначе выше сработал бы provider ==
            # "balance"), спишем эту часть с баланса ПОСЛЕ подтверждения
            # оплаты остатка (см. check_payment) — не сразу, чтобы деньги не
            # "зависали" списанными, если счёт в Platega так и не оплатят.
            balance_credit = round(min(user.balance, price_rub), 2) if price_rub > 0 else 0.0
            remaining_rub = round(price_rub - balance_credit)

            if remaining_rub <= 0:
                # Между открытием экрана оплаты и нажатием кнопки баланс
                # успел стать достаточным (например, пополнился) — просто
                # выдаём доступ полностью с баланса, счёт провайдеру не нужен.
                await adjust_balance(session, user, -price_rub)
                try:
                    subscription_url = await _grant_subscription(
                        session, callback.from_user.id, callback.from_user.username, plan_code, plan["days"]
                    )
                except Exception:
                    logger.exception("Error while granting fully-balance-covered subscription for tg_id=%s", callback.from_user.id)
                    await adjust_balance(session, user, price_rub)  # откатываем списание
                    await callback.answer(
                        "Не удалось выдать доступ. Средства не списаны, попробуйте позже.",
                        show_alert=True,
                    )
                    return
                if discount > 0:
                    await consume_discount_use(session, user)

                await _edit_message(callback, 
                    "✅ Оплата с баланса прошла успешно, доступ выдан!\n\n"
                    f"Ваша ссылка-подписка (добавьте в клиент V2rayNG / Streisand / Hiddify):\n{subscription_url}",
                    reply_markup=main_menu_kb(),
                )
                await callback.answer()
                return

            try:
                invoice = await platega_client.create_invoice(
                    amount_rub=remaining_rub,
                    comment=f"{config.vpn_name}: подписка {plan['title']}",
                    order_id=f"user{user.tg_id}-{plan_code}-{int(dt.datetime.utcnow().timestamp())}",
                    user_id=user.tg_id,
                    username=callback.from_user.username,
                )
            except PlategaError:
                logger.exception("Platega error while creating purchase invoice for tg_id=%s", user.tg_id)
                await callback.answer(
                    "Не удалось создать счёт на оплату. Попробуйте позже.", show_alert=True
                )
                return
            await create_invoice(
                session,
                user_id=user.id,
                plan_code=plan_code,
                invoice_id=str(invoice["invoice_id"]),
                pay_url=invoice["pay_url"],
                amount=remaining_rub,
                purpose="subscription",
                provider="platega",
                currency="RUB",
                discount_percent=discount,
                balance_credit=balance_credit,
            )
            amount_text = f"{remaining_rub} ₽"
            if balance_credit > 0:
                amount_text += f" (+ {balance_credit:.0f}₽ спишется с баланса после оплаты)"
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

    await _edit_message(callback, 
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

            await _edit_message(callback, 
                f"✅ Баланс пополнен на {local_invoice.amount:.0f} ₽\n"
                f"Текущий баланс: {user.balance:.0f} ₽",
                reply_markup=main_menu_kb(),
            )
            await callback.answer()
            return

        if local_invoice.purpose == "devices":
            new_limit = await _grant_extra_devices(
                session, callback.from_user.id, callback.from_user.username, local_invoice.quantity or 0
            )
            await mark_invoice_paid(session, local_invoice)

            await _edit_message(callback, 
                f"✅ Оплата подтверждена!\nДобавлено устройств: {local_invoice.quantity}\n"
                f"Текущий лимит устройств: {new_limit}",
                reply_markup=main_menu_kb(),
            )
            await callback.answer()
            return

        # оплата подтверждена -> продлеваем/создаём клиента у VPN-провайдера
        # include_disabled=True: тариф уже оплачен раньше, доступ выдаём в любом
        # случае, даже если админ отключил его после создания счёта.
        plan = await get_plan(session, local_invoice.plan_code, include_disabled=True)
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

        user = await get_or_create_user(session, callback.from_user.id, callback.from_user.username)
        if local_invoice.discount_percent > 0:
            await consume_discount_use(session, user)
        if local_invoice.balance_credit > 0:
            # Списываем ровно после успешной выдачи доступа, не раньше — чтобы
            # деньги не пропадали, если выдача вдруг не удастся. min() на
            # случай, если баланс уменьшился с момента создания счёта другой
            # покупкой — не уходим в минус.
            await adjust_balance(session, user, -min(local_invoice.balance_credit, user.balance))
        await mark_invoice_paid(session, local_invoice)

    await _edit_message(callback, 
        "✅ Оплата подтверждена, доступ выдан!\n\n"
        f"Ваша ссылка-подписка (добавьте в клиент V2rayNG / Streisand / Hiddify):\n{subscription_url}",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()
