import datetime as dt

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Message,
)

from config import config
from database.db import (
    PromoError,
    adjust_balance,
    all_active_subscriptions,
    all_promo_codes,
    async_session,
    create_promo_code,
    get_or_create_user,
    get_promo_by_code,
    get_user_by_username,
    set_promo_active,
)

router = Router(name="admin")

PROMO_HELP = (
    "Формат:\n"
    "<code>/promo_create КОД ТИП ЗНАЧЕНИЕ [max=N] [expires=YYYY-MM-DD] "
    "[uses=N] [valid_days=N] [discount=N]</code>\n\n"
    "Типы:\n"
    "• <code>days</code> — бонусные дни подписки сразу\n"
    "  <code>/promo_create WELCOME7 days 7 max=100</code>\n"
    "• <code>balance</code> — начисление на баланс сразу (₽)\n"
    "  <code>/promo_create SALE300 balance 300 max=50 expires=2026-12-31</code>\n"
    "• <code>discount</code> — скидка % на будущую покупку (100 = бесплатно)\n"
    "  <code>uses</code> — сколько раз можно применить скидку (по умолчанию 1, 0 = без лимита)\n"
    "  <code>valid_days</code> — сколько дней скидка действует после активации (по умолчанию бессрочно)\n"
    "  <code>/promo_create FREEMONTH discount 100 uses=1 valid_days=3 max=50</code>\n"
    "  <code>/promo_create SPRING15 discount 15 uses=0 valid_days=30 max=500</code>\n"
    "• <code>partner</code> — пробные дни + скидка % разом (ЗНАЧЕНИЕ = дни триала)\n"
    "  <code>discount</code> — обязателен, % скидки на будущие покупки\n"
    "  <code>/promo_create PARTNER15 partner 3 discount=15 uses=0 valid_days=365 max=1000</code>"
)


def _promo_value_text(promo) -> str:
    if promo.type == "days":
        return f"{int(promo.value)} дней"
    if promo.type == "balance":
        return f"{promo.value:.0f} ₽"
    if promo.type == "discount":
        uses_text = f"{promo.discount_uses}×" if promo.discount_uses else "безлимит"
        valid_text = f", {promo.discount_valid_days}д" if promo.discount_valid_days else ""
        return f"скидка {promo.value:.0f}% ({uses_text}{valid_text})"
    uses_text = f"{promo.discount_uses}×" if promo.discount_uses else "безлимит"
    valid_text = f", {promo.discount_valid_days}д" if promo.discount_valid_days else ""
    return f"{int(promo.value)} дней триала + скидка {promo.extra_value:.0f}% ({uses_text}{valid_text})"


def _promo_button_kb(bot_username: str, code: str) -> InlineKeyboardMarkup:
    # Раньше использовался deep-link в чат с ботом (t.me/<bot>?start=...),
    # потому что callback-кнопка не доставляется пользователю, который ни
    # разу не писал боту. Теперь, если в @BotFather настроено мини-приложение
    # с коротким именем (WEBAPP_SHORT_NAME), используем прямую ссылку на
    # Mini App — t.me/<bot>/<short_name>?startapp=... . Она тоже работает
    # для любого пользователя без диалога с ботом, но открывает сразу
    # приложение, а не чат: Telegram передаёт startapp-параметр в
    # window.Telegram.WebApp.initDataUnsafe.start_param, и app.js сам
    # активирует промокод и покажет результат всплывающим окном.
    if config.webapp_short_name:
        url = f"https://t.me/{bot_username}/{config.webapp_short_name}?startapp=promo_{code}"
    else:
        url = f"https://t.me/{bot_username}?start=promo_{code}"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🎁 Активировать", url=url)]]
    )


def _is_admin(user_id: int) -> bool:
    return user_id in config.admin_ids


async def _resolve_target_tg_id(session, token: str) -> tuple[int | None, str | None]:
    """Принимает TG_ID или @username, возвращает (tg_id, error_message)."""
    token = token.strip()
    if token.lstrip("-").isdigit():
        return int(token), None

    user = await get_user_by_username(session, token)
    if user is None:
        return None, (
            f"Пользователь с username «{token.lstrip('@')}» не найден в базе.\n"
            "Бот знает username только тех, кто хотя бы раз писал ему (/start и т.п.) — "
            "если человек ни разу не заходил в бота, используйте его TG_ID."
        )
    return user.tg_id, None


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    async with async_session() as session:
        subs = await all_active_subscriptions(session)

    now = dt.datetime.utcnow()
    active = sum(1 for s in subs if s.expires_at > now)
    expired = len(subs) - active

    await message.answer(
        f"📊 Статистика\n\nВсего подписок: {len(subs)}\nАктивных: {active}\nИстёкших: {expired}"
    )


@router.message(Command("promo_create"))
async def promo_create(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if len(parts) < 3:
        await message.answer(PROMO_HELP)
        return

    code, type_, value_raw, *rest = parts
    type_ = type_.lower()
    if type_ not in ("days", "balance", "discount", "partner"):
        await message.answer("Тип промокода должен быть days / balance / discount / partner")
        return

    try:
        value = float(value_raw)
    except ValueError:
        await message.answer("Значение промокода (3-й аргумент) должно быть числом")
        return

    if type_ in ("discount", "partner") and not (0 < value <= 100 if type_ == "discount" else value >= 0):
        await message.answer("Для discount значение — процент от 1 до 100")
        return

    max_uses = None
    expires_at = None
    discount_uses = 1 if type_ == "discount" else None  # для discount по умолчанию одноразовая
    discount_valid_days = None
    extra_value = None

    for token in rest:
        if "=" in token:
            key, _, raw_val = token.partition("=")
            key = key.lower()
            if key == "max":
                max_uses = int(raw_val) if raw_val.isdigit() else None
            elif key == "expires":
                try:
                    expires_at = dt.datetime.strptime(raw_val, "%Y-%m-%d")
                except ValueError:
                    await message.answer(f"expires должен быть в формате YYYY-MM-DD, получил «{raw_val}»")
                    return
            elif key == "uses":
                n = int(raw_val) if raw_val.lstrip("-").isdigit() else None
                discount_uses = None if (n is not None and n == 0) else n
            elif key == "valid_days":
                discount_valid_days = int(raw_val) if raw_val.isdigit() else None
            elif key == "discount":
                try:
                    extra_value = float(raw_val)
                except ValueError:
                    await message.answer(f"discount должен быть числом, получил «{raw_val}»")
                    return
            else:
                await message.answer(f"Неизвестный параметр «{key}»")
                return
        elif token.isdigit():
            max_uses = int(token)  # обратная совместимость со старым позиционным форматом
        else:
            try:
                expires_at = dt.datetime.strptime(token, "%Y-%m-%d")
            except ValueError:
                await message.answer(f"Не понял параметр «{token}»")
                return

    if type_ == "partner" and extra_value is None:
        await message.answer("Для partner обязателен параметр discount=N (процент скидки)")
        return

    async with async_session() as session:
        try:
            promo = await create_promo_code(
                session,
                code=code,
                type_=type_,
                value=value,
                max_uses=max_uses,
                expires_at=expires_at,
                extra_value=extra_value,
                discount_uses=discount_uses,
                discount_valid_days=discount_valid_days,
            )
        except PromoError as e:
            await message.answer(f"❌ {e}")
            return

    value_text = _promo_value_text(promo)
    limits_text = f"лимит активаций: {promo.max_uses}" if promo.max_uses else "лимит активаций: безлимит"
    expires_text = f", до {promo.expires_at.strftime('%Y-%m-%d')}" if promo.expires_at else ""
    bot_username = (await bot.get_me()).username

    await message.answer(
        f"✅ Промокод <code>{promo.code}</code> создан: {value_text}, {limits_text}{expires_text}\n\n"
        "⚠️ Обычная пересылка (Forward) уберёт кнопку — Telegram так устроен. Чтобы отправить рабочую "
        "кнопку активации, есть два способа:\n\n"
        f"1️⃣ Команда: <code>/promo_send TG_ID_или_@username {promo.code}</code>\n"
        f"2️⃣ Inline прямо в чате с человеком: наберите "
        f"<code>@{bot_username} {promo.code}</code> в любом чате\n",
        reply_markup=_promo_button_kb(bot_username, promo.code),
    )


@router.message(Command("promo_send"))
async def promo_send(message: Message, bot: Bot) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/promo_send TG_ID_или_@username КОД</code>\n"
            "Примеры:\n"
            "<code>/promo_send 123456789 WELCOME7</code>\n"
            "<code>/promo_send @ivanov WELCOME7</code>"
        )
        return

    target_raw, code = parts[0], parts[1]
    code = code.strip().upper()

    async with async_session() as session:
        tg_id, error = await _resolve_target_tg_id(session, target_raw)
        if error:
            await message.answer(error)
            return

        promo = await get_promo_by_code(session, code)
    if promo is None:
        await message.answer(f"Промокод «{code}» не найден")
        return

    bot_username = (await bot.get_me()).username
    keyboard = _promo_button_kb(bot_username, promo.code)

    try:
        await bot.send_message(
            tg_id,
            f"🎁 Вам подарили промокод <code>{promo.code}</code>!\nНажмите кнопку, чтобы активировать:",
            reply_markup=keyboard,
        )
    except Exception as e:
        await message.answer(f"Не удалось отправить сообщение пользователю {tg_id}: {e}")
        return

    await message.answer(f"✅ Отправлено пользователю {tg_id}")


@router.inline_query()
async def promo_inline(inline_query: InlineQuery, bot: Bot) -> None:
    if not _is_admin(inline_query.from_user.id):
        await inline_query.answer([], cache_time=1, is_personal=True)
        return

    code_query = inline_query.query.strip().upper()

    async with async_session() as session:
        if code_query:
            promo = await get_promo_by_code(session, code_query)
            promos = [promo] if promo else []
        else:
            all_promos = await all_promo_codes(session)
            promos = [p for p in all_promos if p.active][:10]

    bot_username = (await bot.get_me()).username
    results = []
    for promo in promos:
        keyboard = _promo_button_kb(bot_username, promo.code)
        title = f"🎁 {promo.code} — {_promo_value_text(promo)}"
        description = "может активировать кто угодно в чате"
        message_text = f"🎁 Вам промокод <code>{promo.code}</code>!\nНажмите кнопку, чтобы активировать:"
        results.append(
            InlineQueryResultArticle(
                id=promo.code,
                title=title,
                description=description,
                input_message_content=InputTextMessageContent(message_text=message_text, parse_mode="HTML"),
                reply_markup=keyboard,
            )
        )

    await inline_query.answer(results, cache_time=1, is_personal=True)


@router.message(Command("promo_list"))
async def promo_list(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    async with async_session() as session:
        promos = await all_promo_codes(session)

    if not promos:
        await message.answer("Промокодов пока нет.")
        return

    lines = ["🎟 Промокоды:\n"]
    for p in promos:
        if p.type == "days":
            value_text = f"{int(p.value)}д"
        elif p.type == "balance":
            value_text = f"{p.value:.0f}₽"
        elif p.type == "discount":
            value_text = f"скидка {p.value:.0f}%"
        else:
            value_text = f"{int(p.value)}д триал + {p.extra_value:.0f}% скидка"
        status = "✅" if p.active else "🚫"
        uses_text = f"{p.used_count}/{p.max_uses}" if p.max_uses else f"{p.used_count}/∞"
        expires_text = p.expires_at.strftime("%Y-%m-%d") if p.expires_at else "—"
        lines.append(f"{status} <code>{p.code}</code> [{p.type}] {value_text}, исп. {uses_text}, до {expires_text}")

    await message.answer("\n".join(lines))


@router.message(Command("promo_disable"))
async def promo_disable(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if not parts:
        await message.answer("Формат: <code>/promo_disable КОД</code>")
        return

    async with async_session() as session:
        promo = await get_promo_by_code(session, parts[0])
        if promo is None:
            await message.answer("Такого промокода нет")
            return
        await set_promo_active(session, promo, False)

    await message.answer(f"🚫 Промокод <code>{promo.code}</code> отключён")


@router.message(Command("add_balance"))
async def add_balance(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/add_balance TG_ID_или_@username СУММА</code>\n"
            "Примеры:\n"
            "<code>/add_balance 123456789 300</code>\n"
            "<code>/add_balance @ivanov 300</code>\n"
            "Сумма может быть отрицательной, чтобы списать баланс."
        )
        return

    target_raw, amount_raw = parts[0], parts[1]
    try:
        amount = float(amount_raw)
    except ValueError:
        await message.answer("Сумма должна быть числом")
        return

    async with async_session() as session:
        tg_id, error = await _resolve_target_tg_id(session, target_raw)
        if error:
            await message.answer(error)
            return
        user = await get_or_create_user(session, tg_id, None)
        user = await adjust_balance(session, user, amount)

    sign = "+" if amount >= 0 else ""
    await message.answer(
        f"✅ Баланс пользователя <code>{tg_id}</code> изменён на {sign}{amount:.0f}₽\n"
        f"Текущий баланс: {user.balance:.0f}₽"
    )
