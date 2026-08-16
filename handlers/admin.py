import asyncio
import datetime as dt
import logging
import uuid

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

from config import PLANS, config
from database.db import (
    PromoError,
    adjust_balance,
    all_active_subscriptions,
    count_all_users,
    count_paid_users,
    list_users_missing_first_name,
    set_user_first_name,
    all_promo_codes,
    async_session,
    create_promo_code,
    effective_device_limit,
    get_or_create_user,
    get_plans,
    get_promo_by_code,
    get_recent_invoices,
    get_referral_count,
    get_setting,
    get_subscription,
    get_user_by_id,
    get_user_by_tg_id,
    get_user_by_username,
    LEADERBOARD_RESET_SETTING_KEY,
    set_setting,
    reset_plan_price,
    set_plan_enabled,
    set_plan_price,
    set_promo_active,
    set_subscription_url,
)

router = Router(name="admin")
logger = logging.getLogger(__name__)

PROMO_HELP = (
    "Формат:\n"
    "<code>/promo_create КОД ТИП ЗНАЧЕНИЕ [max=N] [expires=YYYY-MM-DD] "
    "[uses=N] [valid_days=N]</code>\n\n"
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
    "• <code>partner</code> — то же самое, что discount, но ограничение "
    "\"один раз на юзера\" считается по ЛЮБОМУ partner-коду сразу (не по "
    "конкретному коду). Пробных дней не даёт — они и так выдаются "
    "автоматически на первом /start.\n"
    "  <code>/promo_create PARTNER15 partner 15 uses=0 valid_days=365 max=1000</code>"
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
    return f"скидка {promo.value:.0f}% ({uses_text}{valid_text}) [partner]"


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
        inline_keyboard=[[InlineKeyboardButton(text="Активировать", url=url)]]
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


async def _resolve_target_user(session, token: str):
    """Принимает UUID пользователя (из /user_info), TG_ID или @username.
    Возвращает (User | None, error_message | None)."""
    token = token.strip()
    try:
        user_id = uuid.UUID(token)
    except ValueError:
        pass
    else:
        user = await get_user_by_id(session, user_id)
        if user is None:
            return None, f"Пользователь с UUID <code>{token}</code> не найден в базе."
        return user, None

    tg_id, error = await _resolve_target_tg_id(session, token)
    if error:
        return None, error
    user = await get_user_by_tg_id(session, tg_id)
    if user is None:
        return None, f"Пользователь с TG_ID <code>{tg_id}</code> не найден в базе."
    return user, None


@router.message(Command("set_vpn_link"))
async def set_vpn_link(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split(maxsplit=2)[1:]
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/set_vpn_link ИДЕНТИФИКАТОР ССЫЛКА</code>\n"
            "ИДЕНТИФИКАТОР — UUID пользователя (см. /user_info), TG_ID или @username.\n\n"
            "Примеры:\n"
            "<code>/set_vpn_link 3fa85f64-5717-4562-b3fc-2c963f66afa6 https://example.com/sub/abc123</code>\n"
            "<code>/set_vpn_link @ivanov https://example.com/sub/abc123</code>"
        )
        return

    target_raw, url = parts[0], parts[1].strip()

    async with async_session() as session:
        user, error = await _resolve_target_user(session, target_raw)
        if error:
            await message.answer(error)
            return

        sub = await set_subscription_url(session, user.id, url)

    if sub is None:
        await message.answer(
            f"⚠️ У пользователя <code>{user.id}</code> ещё нет подписки — "
            "сначала выдайте её (оплата/промокод//add_balance), потом привязывайте ссылку."
        )
        return

    await message.answer(
        f"✅ Ссылка на VPN-ключ привязана.\n\n"
        f"UUID: <code>{user.id}</code>\n"
        f"TG ID: <code>{user.tg_id}</code>\n"
        f"Ссылка: <code>{sub.subscription_url}</code>"
    )


@router.message(Command("stats"))
async def stats(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    async with async_session() as session:
        subs = await all_active_subscriptions(session)
        total_users = await count_all_users(session)
        paid_users = await count_paid_users(session)

    now = dt.datetime.utcnow()
    active = sum(1 for s in subs if s.expires_at > now)
    expired = len(subs) - active

    await message.answer(
        "📊 Статистика\n\n"
        f"Всего пользователей в боте: {total_users}\n"
        f"Купили подписку (реальные лиды, не пробный период): {paid_users}\n\n"
        f"Всего подписок: {len(subs)}\nАктивных: {active}\nИстёкших: {expired}"
    )


@router.message(Command("backfill_names"))
async def backfill_names(message: Message, bot: Bot) -> None:
    """Разовая утилита: дозаполняет first_name пользователям, у которых он
    пустой (обычно те, кто зашёл в бота до того, как first_name стали
    сохранять для их конкретного пути — например, переход по реф. ссылке до
    соответствующего фикса). Идём в Telegram get_chat() по каждому tg_id —
    работает, только если у бота уже есть открытый чат с пользователем."""
    if not _is_admin(message.from_user.id):
        return

    async with async_session() as session:
        users = await list_users_missing_first_name(session)

    if not users:
        await message.answer("Все пользователи уже с именем — нечего дозаполнять.")
        return

    status = await message.answer(f"Дозаполняю имена: 0/{len(users)}…")
    updated = 0
    failed = 0
    for i, user in enumerate(users, start=1):
        try:
            chat = await bot.get_chat(user.tg_id)
            first_name = getattr(chat, "first_name", None)
            if first_name:
                async with async_session() as session:
                    fresh = await get_user_by_tg_id(session, user.tg_id)
                    if fresh is not None:
                        await set_user_first_name(session, fresh, first_name)
                updated += 1
        except Exception:
            logger.exception("Не удалось получить имя для tg_id=%s при бэкфилле", user.tg_id)
            failed += 1
        if i % 15 == 0:
            await asyncio.sleep(1)  # не превышаем rate limit Telegram Bot API
            try:
                await status.edit_text(f"Дозаполняю имена: {i}/{len(users)}…")
            except Exception:
                pass

    await message.answer(
        f"✅ Готово: обновлено {updated} из {len(users)}"
        + (f", не удалось получить имя для {failed} (бот не писал им раньше)" if failed else "")
    )


@router.message(Command("leaderboard_reset"))
async def leaderboard_reset(message: Message) -> None:
    """Обнуляет счётчик /leaderboard для конкурсов вида 'кто за 30 минут
    пригласит больше всего людей' — НЕ удаляет реальных рефералов и не
    трогает уже начисленные бонусы, а просто задаёт точку отсчёта: дальше
    /leaderboard считает только тех, кто присоединился после неё.

    /leaderboard_reset — начать отсчёт заново (с текущего момента)
    /leaderboard_reset off — вернуть счёт за всё время (снять точку отсчёта)
    /leaderboard_reset status — посмотреть текущую точку отсчёта, если есть
    """
    if not _is_admin(message.from_user.id):
        return

    arg = (message.text or "").split(maxsplit=1)
    arg = arg[1].strip().lower() if len(arg) > 1 else ""

    async with async_session() as session:
        if arg == "off":
            await set_setting(session, LEADERBOARD_RESET_SETTING_KEY, None)
            await message.answer("✅ Точка отсчёта снята — /leaderboard снова считает за всё время.")
            return

        if arg == "status":
            current = await get_setting(session, LEADERBOARD_RESET_SETTING_KEY)
            if current is None:
                await message.answer("Точка отсчёта не задана — /leaderboard считает за всё время.")
            else:
                since = dt.datetime.fromisoformat(current)
                await message.answer(f"Точка отсчёта: {since.strftime('%d.%m.%Y %H:%M')} UTC")
            return

        now = dt.datetime.utcnow()
        await set_setting(session, LEADERBOARD_RESET_SETTING_KEY, now.isoformat())

    await message.answer(
        f"🔄 Готово! /leaderboard теперь считает рефералов с {now.strftime('%d.%m %H:%M')} UTC — "
        "удобно для конкурса на время (например, 'кто за 30 минут пригласит больше всего').\n\n"
        "Когда захотите вернуть счёт за всё время — <code>/leaderboard_reset off</code>."
    )


@router.message(Command("getemojiid"))
async def get_emoji_id(message: Message) -> None:
    """Dev-утилита: перешлите/отправьте боту сообщение с премиум-эмодзи после
    команды /getemojiid — бот вернёт custom_emoji_id, который можно вписать
    в .env (ICON_EMOJI_BUY и т.п.) для иконок на кнопках. Работает, только
    если у вас есть Telegram Premium — иначе эмодзи в сообщении останется
    обычным, без custom_emoji entity."""
    if not _is_admin(message.from_user.id):
        return

    entities = message.entities or []
    custom_emoji_ids = [e.custom_emoji_id for e in entities if e.type == "custom_emoji"]

    if not custom_emoji_ids:
        await message.answer(
            "В этом сообщении нет кастомного эмодзи.\n\n"
            "Отправьте команду /getemojiid, а сразу следом (или в том же сообщении, "
            "если это позволяет клиент) — премиум-эмодзи. Нужен Telegram Premium, "
            "иначе Telegram присылает обычный unicode-эмодзи без ID."
        )
        return

    lines = [f"<code>{eid}</code>" for eid in custom_emoji_ids]
    await message.answer("Custom emoji ID:\n" + "\n".join(lines))


def _has_custom_emoji(message: Message) -> bool:
    """Фильтр, а не F.entities: последний матчит ЛЮБЫЕ сущности, включая
    bot_command, и перехватил бы остальные /admin-команды ниже по роутеру."""
    return any(e.type == "custom_emoji" for e in (message.entities or []))


@router.message(_has_custom_emoji)
async def get_emoji_id_reply(message: Message) -> None:
    """Второй шаг /getemojiid: если клиент не позволяет вставить эмодзи в ту
    же команду, админ может просто прислать сообщение с премиум-эмодзи
    следующим — бот всё равно вернёт ID."""
    if not _is_admin(message.from_user.id):
        return
    custom_emoji_ids = [e.custom_emoji_id for e in (message.entities or []) if e.type == "custom_emoji"]
    lines = [f"<code>{eid}</code>" for eid in custom_emoji_ids]
    await message.answer("Custom emoji ID:\n" + "\n".join(lines))


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

    if type_ in ("discount", "partner") and not (0 < value <= 100):
        await message.answer("Для discount/partner значение — процент от 1 до 100")
        return

    max_uses = None
    expires_at = None
    discount_uses = 1 if type_ in ("discount", "partner") else None  # по умолчанию одноразовая
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
            value_text = f"скидка {p.value:.0f}% [partner]"
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


PLAN_HELP = (
    "Формат:\n"
    "<code>/plans</code> — список тарифов, их статус и цена\n"
    "<code>/plan_disable КОД</code> — скрыть тариф из продажи\n"
    "<code>/plan_enable КОД</code> — снова показать тариф\n"
    "<code>/plan_price КОД ЦЕНА_RUB [ЦЕНА_USDT]</code> — изменить цену тарифа\n"
    "<code>/plan_price_reset КОД</code> — вернуть цену по умолчанию (из конфига)\n\n"
    "Коды тарифов: " + ", ".join(p["code"] for p in PLANS)
)


def _find_config_plan(plan_code: str) -> dict | None:
    return next((p for p in PLANS if p["code"] == plan_code), None)


@router.message(Command("plans"))
async def plans_list(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    async with async_session() as session:
        plans = await get_plans(session, include_disabled=True)

    lines = ["💳 Тарифы:\n"]
    for p in plans:
        status = "✅" if p["enabled"] else "🚫"
        base = _find_config_plan(p["code"])
        price_note = ""
        if base and (p["price_rub"] != base["price_rub"] or p["price_usdt"] != base["price_usdt"]):
            price_note = f" (по умолчанию {base['price_usdt']}$ / {base['price_rub']}₽)"
        lines.append(
            f"{status} <code>{p['code']}</code> {p['title']} — {p['price_usdt']}$ / {p['price_rub']}₽{price_note}"
        )
    lines.append(f"\n{PLAN_HELP}")

    await message.answer("\n".join(lines))


@router.message(Command("plan_disable"))
async def plan_disable(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if not parts:
        await message.answer("Формат: <code>/plan_disable КОД</code>")
        return

    plan_code = parts[0]
    if _find_config_plan(plan_code) is None:
        await message.answer(f"Тарифа с кодом «{plan_code}» нет в конфиге")
        return

    async with async_session() as session:
        await set_plan_enabled(session, plan_code, False)

    await message.answer(f"🚫 Тариф <code>{plan_code}</code> отключён и больше не продаётся")


@router.message(Command("plan_enable"))
async def plan_enable(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if not parts:
        await message.answer("Формат: <code>/plan_enable КОД</code>")
        return

    plan_code = parts[0]
    if _find_config_plan(plan_code) is None:
        await message.answer(f"Тарифа с кодом «{plan_code}» нет в конфиге")
        return

    async with async_session() as session:
        await set_plan_enabled(session, plan_code, True)

    await message.answer(f"✅ Тариф <code>{plan_code}</code> снова доступен для покупки")


@router.message(Command("plan_price"))
async def plan_price(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if len(parts) < 2:
        await message.answer(
            "Формат: <code>/plan_price КОД ЦЕНА_RUB [ЦЕНА_USDT]</code>\n"
            "Если ЦЕНА_USDT не указана — меняется только цена в рублях.\n"
            "Пример: <code>/plan_price 1m 199</code>\n"
            "Пример: <code>/plan_price 1m 199 2.49</code>"
        )
        return

    plan_code, price_rub_raw, *rest = parts
    if _find_config_plan(plan_code) is None:
        await message.answer(f"Тарифа с кодом «{plan_code}» нет в конфиге")
        return

    try:
        price_rub = float(price_rub_raw)
    except ValueError:
        await message.answer("Цена в рублях должна быть числом")
        return
    if price_rub < 0:
        await message.answer("Цена не может быть отрицательной")
        return

    price_usdt = None
    if rest:
        try:
            price_usdt = float(rest[0])
        except ValueError:
            await message.answer("Цена в USDT должна быть числом")
            return
        if price_usdt < 0:
            await message.answer("Цена не может быть отрицательной")
            return

    async with async_session() as session:
        await set_plan_price(session, plan_code, price_rub=price_rub, price_usdt=price_usdt)
        plans = await get_plans(session, include_disabled=True)

    plan = next(p for p in plans if p["code"] == plan_code)
    await message.answer(
        f"✅ Цена тарифа <code>{plan_code}</code> обновлена: {plan['price_usdt']}$ / {plan['price_rub']}₽"
    )


@router.message(Command("plan_price_reset"))
async def plan_price_reset(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if not parts:
        await message.answer("Формат: <code>/plan_price_reset КОД</code>")
        return

    plan_code = parts[0]
    base = _find_config_plan(plan_code)
    if base is None:
        await message.answer(f"Тарифа с кодом «{plan_code}» нет в конфиге")
        return

    async with async_session() as session:
        await reset_plan_price(session, plan_code)

    await message.answer(
        f"✅ Цена тарифа <code>{plan_code}</code> сброшена к значению по умолчанию: "
        f"{base['price_usdt']}$ / {base['price_rub']}₽"
    )


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


CMD_LIST_TEXT = (
    "📋 <b>Список команд</b>\n\n"
    "<b>Пользователь</b>\n"
    "/start — главное меню (также реф. ссылка: ?start=ref_TG_ID)\n"
    "/leaderboard — топ-10 по числу приглашённых рефералов\n\n"
    "<b>Админ</b>\n"
    "/stats — статистика: пользователи, реальные лиды, подписки\n"
    "/backfill_names — дозаполнить имена пользователям, у которых их ещё нет\n"
    "/leaderboard_reset [off|status] — обнулить/снять/посмотреть точку отсчёта конкурса по рефералам\n"
    "/user_info TG_ID_или_@username — полная информация о пользователе (UUID, баланс, подписка, счета)\n"
    "/add_balance TG_ID_или_@username СУММА — начислить/списать баланс\n"
    "/set_vpn_link UUID_или_TG_ID_или_@username ССЫЛКА — вручную привязать ссылку на VPN-ключ к подписке\n"
    "/plans — список тарифов со статусом и ценой\n"
    "/plan_disable КОД — скрыть тариф из продажи\n"
    "/plan_enable КОД — включить тариф\n"
    "/plan_price КОД ЦЕНА_RUB [ЦЕНА_USDT] — изменить цену тарифа\n"
    "/plan_price_reset КОД — сбросить цену тарифа к значению по умолчанию\n"
    "/promo_create КОД ТИП ЗНАЧЕНИЕ [...] — создать промокод\n"
    "/promo_list — список промокодов со статистикой\n"
    "/promo_disable КОД — отключить промокод\n"
    "/promo_send TG_ID_или_@username КОД — отправить промокод конкретному пользователю\n"
    "/getemojiid — получить id кастомного эмодзи (ответом на сообщение с ним)\n"
    "/cmdlist — этот список"
)


@router.message(Command("cmdlist"))
async def cmdlist(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await message.answer(CMD_LIST_TEXT)


@router.message(Command("user_info"))
async def user_info(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return

    parts = (message.text or "").split()[1:]
    if len(parts) < 1:
        await message.answer(
            "Формат: <code>/user_info TG_ID_или_@username</code>\n"
            "Примеры:\n"
            "<code>/user_info 123456789</code>\n"
            "<code>/user_info @ivanov</code>"
        )
        return

    target_raw = parts[0]

    async with async_session() as session:
        tg_id, error = await _resolve_target_tg_id(session, target_raw)
        if error:
            await message.answer(error)
            return

        user = await get_user_by_tg_id(session, tg_id)
        if user is None:
            await message.answer(f"Пользователь с TG_ID <code>{tg_id}</code> не найден в базе.")
            return

        subscription = await get_subscription(session, user.id)
        invoices = await get_recent_invoices(session, user.id, limit=5)
        referral_count = await get_referral_count(session, user.tg_id)

    lines = [
        "👤 <b>Информация о пользователе</b>",
        "",
        f"UUID: <code>{user.id}</code>",
        f"TG ID: <code>{user.tg_id}</code>",
        f"Username: @{user.username}" if user.username else "Username: —",
        f"Баланс: {user.balance:.0f}₽",
        f"Доп. устройства: {user.extra_devices}",
        f"Лимит устройств: {effective_device_limit(user) or '∞'}",
        f"VPN client UUID: <code>{user.vpn_client_uuid}</code>" if user.vpn_client_uuid else "VPN client UUID: —",
        f"Регистрация: {user.created_at:%Y-%m-%d %H:%M} UTC",
    ]

    if user.discount_percent > 0:
        uses_text = f"{user.discount_uses_left}×" if user.discount_uses_left is not None else "безлимит"
        expires_text = (
            f", до {user.discount_expires_at:%Y-%m-%d}" if user.discount_expires_at else ""
        )
        lines.append(f"Скидка: {user.discount_percent:.0f}% ({uses_text}{expires_text})")

    if user.referred_by:
        lines.append(f"Приглашён: TG_ID <code>{user.referred_by}</code>")
    lines.append(f"Пригласил(а) сам(а): {referral_count}")

    lines.append("")
    if subscription:
        status = "✅ активна" if subscription.expires_at > dt.datetime.utcnow() else "❌ истекла"
        lines.append("📦 <b>Подписка</b>")
        lines.append(f"Тариф: {subscription.plan_code}")
        lines.append(f"Статус: {status}")
        lines.append(f"Действует до: {subscription.expires_at:%Y-%m-%d %H:%M} UTC")
    else:
        lines.append("📦 Подписки нет")

    lines.append("")
    if invoices:
        lines.append("💳 <b>Последние счета</b>")
        for inv in invoices:
            status_icon = {"paid": "✅", "active": "🕓", "expired": "❌"}.get(inv.status, "•")
            lines.append(
                f"{status_icon} {inv.created_at:%Y-%m-%d} — {inv.amount:.0f} {inv.currency} "
                f"({inv.purpose}, {inv.provider}, {inv.status})"
            )
    else:
        lines.append("💳 Счетов нет")

    await message.answer("\n".join(lines))
