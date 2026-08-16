import datetime as dt
import logging
import secrets
import uuid

from sqlalchemy import func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import PLANS, config
from database.models import (
    Base,
    BrowserSession,
    Invoice,
    LoginToken,
    PlanOverride,
    PromoCode,
    PromoRedemption,
    Subscription,
    User,
)

logger = logging.getLogger(__name__)

# pool_pre_ping — проверяет соединение перед использованием и молча
# переподключается, если оно протухло. Для SQLite (локальный файл) это
# почти no-op, а для PostgreSQL на отдельном мастер-сервере защищает от
# разрывов соединения по сети/таймаутов простоя.
engine = create_async_engine(config.database_url, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False)

# Токен из /start weblogin живёт недолго — это просто "мостик" от бота к
# браузеру, пользователь переходит по нему сразу же.
LOGIN_TOKEN_TTL_SECONDS = 5 * 60
# А вот браузерная сессия, которую он выдаёт при обмене, живёт долго —
# чтобы не приходилось логиниться через бота при каждом визите на сайт.
BROWSER_SESSION_TTL_DAYS = 30


async def init_db() -> None:
    async with engine.begin() as conn:
        await _reset_tables_if_schema_mismatch(conn)
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_login_token_columns(conn)
        await _ensure_device_columns(conn)
        await _ensure_referral_columns(conn)
        await _ensure_invoice_balance_credit_column(conn)


async def _reset_tables_if_schema_mismatch(conn) -> None:
    """users.id перешёл с autoincrement INTEGER на UUID. create_all не умеет
    менять тип уже существующей колонки — на базе, где таблица users была
    создана ДО этого перехода, чтение User.id падает (SQLAlchemy пытается
    распарсить целое число как UUID). Раз старые данные не жалко — при
    обнаружении такого рассинхрона просто дропаем users и таблицы, у которых
    есть FK на неё (в порядке от зависимых к родительской), и даём create_all
    создать их заново по актуальной схеме. Если базе уже сразу создана
    с UUID (например, свежий деплой) — эта функция ничего не делает."""

    def _needs_reset(sync_conn) -> bool:
        insp = inspect(sync_conn)
        if "users" not in insp.get_table_names():
            return False
        columns = {col["name"]: col["type"] for col in insp.get_columns("users")}
        id_col = columns.get("id")
        if id_col is None:
            return False
        type_name = str(id_col).upper()
        # PostgreSQL: тип называется "UUID". SQLite (генерик Uuid-тип
        # эмулируется) хранит как "CHAR(32)". Всё остальное (INTEGER,
        # BIGINT, SERIAL и т.п.) — старая схема, требующая сброса.
        return "UUID" not in type_name and "CHAR(32)" not in type_name

    def _drop_old_tables(sync_conn) -> None:
        # Дочерние таблицы (ссылаются на users.id через FK) — первыми,
        # затем сама users, чтобы не упереться в ограничение внешнего ключа.
        for table in ("promo_redemptions", "invoices", "subscriptions", "users"):
            sync_conn.exec_driver_sql(f"DROP TABLE IF EXISTS {table}")

    if await conn.run_sync(_needs_reset):
        logger.warning(
            "users.id in the database uses the old integer schema (pre-UUID) — "
            "dropping users/subscriptions/invoices/promo_redemptions to recreate "
            "them with the current UUID-based schema. Data in these tables will "
            "be lost (everything else, e.g. promo_codes, is left untouched)."
        )
        await conn.run_sync(_drop_old_tables)


def _table_columns(sync_conn, table_name: str) -> set[str]:
    """Диалект-независимая замена PRAGMA table_info(...) — работает
    одинаково и на SQLite, и на PostgreSQL через SQLAlchemy inspector."""
    return {col["name"] for col in inspect(sync_conn).get_columns(table_name)}


async def _ensure_login_token_columns(conn) -> None:
    """Лёгкая сам-миграция: create_all не добавляет новые колонки
    в уже существующие таблицы. chat_id/message_id в login_tokens появились
    позже — добавляем их, если ещё нет (для свежих БД они и так будут
    созданы через create_all выше, тогда ALTER TABLE просто не понадобится)."""

    def _add_missing_columns(sync_conn):
        existing = _table_columns(sync_conn, "login_tokens")
        if "chat_id" not in existing:
            sync_conn.exec_driver_sql("ALTER TABLE login_tokens ADD COLUMN chat_id BIGINT")
        if "message_id" not in existing:
            sync_conn.exec_driver_sql("ALTER TABLE login_tokens ADD COLUMN message_id INTEGER")

    await conn.run_sync(_add_missing_columns)


async def _ensure_device_columns(conn) -> None:
    """Сам-миграция: users.extra_devices и invoices.quantity появились
    позже вместе с доп. услугой "Докупить устройства" — добавляем их в уже
    существующие БД, если их ещё нет."""

    def _add_missing_columns(sync_conn):
        users_columns = _table_columns(sync_conn, "users")
        if "extra_devices" not in users_columns:
            sync_conn.exec_driver_sql("ALTER TABLE users ADD COLUMN extra_devices INTEGER DEFAULT 0")

        invoices_columns = _table_columns(sync_conn, "invoices")
        if "quantity" not in invoices_columns:
            sync_conn.exec_driver_sql("ALTER TABLE invoices ADD COLUMN quantity INTEGER")

    await conn.run_sync(_add_missing_columns)


async def _ensure_referral_columns(conn) -> None:
    """Сам-миграция: users.referred_by появился вместе с реферальной
    программой — добавляем колонку в уже существующие БД, если её ещё нет."""

    def _add_missing_columns(sync_conn):
        users_columns = _table_columns(sync_conn, "users")
        if "referred_by" not in users_columns:
            sync_conn.exec_driver_sql("ALTER TABLE users ADD COLUMN referred_by BIGINT")

    await conn.run_sync(_add_missing_columns)


async def _ensure_invoice_balance_credit_column(conn) -> None:
    """Сам-миграция: invoices.balance_credit появился вместе с автопримешиванием
    остатка баланса к оплате тарифа через провайдера — добавляем колонку
    в уже существующие БД, если её ещё нет."""

    def _add_missing_columns(sync_conn):
        invoices_columns = _table_columns(sync_conn, "invoices")
        if "balance_credit" not in invoices_columns:
            sync_conn.exec_driver_sql("ALTER TABLE invoices ADD COLUMN balance_credit FLOAT DEFAULT 0")

    await conn.run_sync(_add_missing_columns)


async def get_referral_count(session: AsyncSession, tg_id: int) -> int:
    result = await session.execute(select(func.count()).select_from(User).where(User.referred_by == tg_id))
    return result.scalar_one()


async def get_top_referrers(session: AsyncSession, limit: int = 10) -> list[tuple[User, int]]:
    """Топ рефереров по числу приглашённых (referred_by == их tg_id).
    Возвращает список (User, count) отсортированный по убыванию count,
    без пользователей с нулём приглашённых.
    """
    referrer_counts = await session.execute(
        select(User.referred_by, func.count().label("cnt"))
        .where(User.referred_by.is_not(None))
        .group_by(User.referred_by)
        .order_by(func.count().desc())
        .limit(limit)
    )
    rows = referrer_counts.all()
    if not rows:
        return []

    referrer_ids = [r[0] for r in rows]
    users_result = await session.execute(select(User).where(User.tg_id.in_(referrer_ids)))
    users_by_tg_id = {u.tg_id: u for u in users_result.scalars().all()}

    top: list[tuple[User, int]] = []
    for referrer_tg_id, cnt in rows:
        user = users_by_tg_id.get(referrer_tg_id)
        if user is not None:
            top.append((user, cnt))
    return top


async def register_referral_if_new(session: AsyncSession, tg_id: int, referrer_tg_id: int) -> bool:
    """Реф. ссылка вида ?start=ref_<TG_ID>: если пользователь tg_id ещё не
    существует в базе — создаёт его с привязкой referred_by и сразу
    начисляет бонусы ОБЕИМ сторонам: рефереру — config.referral_bonus_rub,
    самому приглашённому другу — config.referral_invitee_bonus_rub (только
    за то, что он не был раньше зарегистрирован в боте). Возвращает True,
    если бонусы реально начислены.

    Важно: создаёт пользователя ЗДЕСЬ (не в get_or_create_user), чтобы
    гарантированно поймать момент "это первый /start", а не полагаться на
    отдельную проверку до похода в get_or_create_user (иначе между проверкой
    и созданием юзер мог бы засчитаться дважды). get_or_create_user,
    вызванный следом в _render_profile, просто найдёт уже созданную запись
    и не тронет referred_by/balance.
    """
    if referrer_tg_id == tg_id:
        return False  # нельзя пригласить самого себя

    existing = await session.execute(select(User).where(User.tg_id == tg_id))
    if existing.scalar_one_or_none() is not None:
        return False  # это не первый /start — бонус уже мог быть (или не полагается) раньше

    referrer = await get_user_by_tg_id(session, referrer_tg_id)
    if referrer is None:
        return False  # реферер с таким tg_id не найден — ссылка невалидна

    user = User(tg_id=tg_id, referred_by=referrer_tg_id, balance=config.referral_invitee_bonus_rub)
    session.add(user)
    referrer.balance = round(referrer.balance + config.referral_bonus_rub, 2)
    await session.commit()
    return True


async def get_or_create_user(session: AsyncSession, tg_id: int, username: str | None) -> User:
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    user = result.scalar_one_or_none()
    if user is None:
        user = User(tg_id=tg_id, username=username)
        session.add(user)
        await session.commit()
        await session.refresh(user)
    elif username and user.username != username:
        # держим username свежим, чтобы поиск по нему (напр. /promo_send) не устаревал
        user.username = username
        await session.commit()
        await session.refresh(user)
    return user


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    username = username.lstrip("@")
    result = await session.execute(select(User).where(User.username.ilike(username)))
    return result.scalar_one_or_none()


async def get_user_by_tg_id(session: AsyncSession, tg_id: int) -> User | None:
    """В отличие от get_or_create_user — не создаёт пользователя, если его нет."""
    result = await session.execute(select(User).where(User.tg_id == tg_id))
    return result.scalar_one_or_none()


async def get_user_by_id(session: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def set_subscription_url(session: AsyncSession, user_id: uuid.UUID, url: str) -> Subscription | None:
    """Вручную прикрепляет ссылку на VPN-ключ к подписке пользователя.

    Используется, пока services/vpn_provider.py — заглушка: админ создаёт
    клиента руками на панели и подставляет боту готовую ссылку. Подписка
    у пользователя уже должна существовать (выдана через оплату/промокод/
    /add_balance-флоу) — эта функция её не создаёт, только правит URL.
    Возвращает None, если подписки ещё нет.
    """
    sub = await get_subscription(session, user_id)
    if sub is None:
        return None
    sub.subscription_url = url
    await session.commit()
    await session.refresh(sub)
    return sub


async def get_recent_invoices(session: AsyncSession, user_id: uuid.UUID, limit: int = 5) -> list[Invoice]:
    result = await session.execute(
        select(Invoice)
        .where(Invoice.user_id == user_id)
        .order_by(Invoice.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def set_vpn_client_uuid(session: AsyncSession, user: User, client_uuid: str) -> None:
    user.vpn_client_uuid = client_uuid
    await session.commit()


def effective_device_limit(user: User) -> int:
    """Итоговый лимит устройств пользователя: базовый (config.device_limit) +
    докупленные сверх него (user.extra_devices). Если базовый лимит 0
    (без ограничений), докупать устройства бессмысленно — итог тоже 0."""
    if config.device_limit <= 0:
        return 0
    return config.device_limit + user.extra_devices


def remaining_device_capacity(user: User) -> int | None:
    """Сколько ещё устройств можно докупить, не превысив config.max_device_limit
    (реальный потолок протокола/VPN-панели на одну ссылку-подписку).
    None — потолка нет (max_device_limit <= 0), докупать можно сколько угодно.
    Иначе — неотрицательное число (может быть 0, если потолок уже достигнут
    или превышен раньше добавленным вручную лимитом)."""
    if config.max_device_limit <= 0:
        return None
    return max(0, config.max_device_limit - effective_device_limit(user))


async def add_extra_devices(session: AsyncSession, user: User, count: int) -> User:
    """Начисляет пользователю дополнительные устройства (доп. услуга)."""
    user.extra_devices += count
    await session.commit()
    await session.refresh(user)
    return user


async def adjust_balance(session: AsyncSession, user: User, delta: float) -> User:
    user.balance = round(user.balance + delta, 2)
    await session.commit()
    await session.refresh(user)
    return user


async def get_effective_discount(session: AsyncSession, user: User) -> float:
    """Возвращает текущий % скидки пользователя (0, если нет/истекла), заодно чистит истёкшую."""
    if user.discount_percent <= 0:
        return 0.0
    if user.discount_expires_at is not None and user.discount_expires_at < dt.datetime.utcnow():
        user.discount_percent = 0.0
        user.discount_uses_left = None
        user.discount_expires_at = None
        await session.commit()
        return 0.0
    return user.discount_percent


async def set_user_discount(
    session: AsyncSession,
    user: User,
    percent: float,
    uses: int | None,
    valid_days: int | None,
) -> User:
    user.discount_percent = percent
    user.discount_uses_left = uses
    user.discount_expires_at = (
        dt.datetime.utcnow() + dt.timedelta(days=valid_days) if valid_days else None
    )
    await session.commit()
    await session.refresh(user)
    return user


async def consume_discount_use(session: AsyncSession, user: User) -> None:
    """Списывает одно использование скидки. Если использования не лимитированы (None) - ничего не делает."""
    if user.discount_uses_left is None:
        return
    user.discount_uses_left -= 1
    if user.discount_uses_left <= 0:
        user.discount_percent = 0.0
        user.discount_uses_left = None
        user.discount_expires_at = None
    await session.commit()


async def get_subscription(session: AsyncSession, user_id: uuid.UUID) -> Subscription | None:
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def upsert_subscription(
    session: AsyncSession,
    user_id: uuid.UUID,
    days: int,
    subscription_url: str | None,
    plan_code: str | None = None,
) -> Subscription:
    sub = await get_subscription(session, user_id)
    now = dt.datetime.utcnow()
    if sub is None:
        sub = Subscription(
            user_id=user_id,
            plan_code=plan_code or "promo",
            expires_at=now + dt.timedelta(days=days),
            subscription_url=subscription_url,
        )
        session.add(sub)
    else:
        # если подписка ещё активна - продлеваем от даты окончания, иначе от текущего момента
        base = sub.expires_at if sub.expires_at > now else now
        sub.expires_at = base + dt.timedelta(days=days)
        if plan_code:
            sub.plan_code = plan_code
        if subscription_url:
            sub.subscription_url = subscription_url
    await session.commit()
    await session.refresh(sub)
    return sub


async def create_invoice(
    session: AsyncSession,
    user_id: uuid.UUID,
    invoice_id: str,
    pay_url: str,
    amount: float,
    plan_code: str | None = None,
    purpose: str = "subscription",
    provider: str = "cryptobot",
    currency: str = "USDT",
    discount_percent: float = 0.0,
    quantity: int | None = None,
    balance_credit: float = 0.0,
) -> Invoice:
    inv = Invoice(
        user_id=user_id,
        plan_code=plan_code,
        purpose=purpose,
        provider=provider,
        invoice_id=invoice_id,
        pay_url=pay_url,
        amount=amount,
        currency=currency,
        discount_percent=discount_percent,
        quantity=quantity,
        balance_credit=balance_credit,
        status="active",
    )
    session.add(inv)
    await session.commit()
    await session.refresh(inv)
    return inv


async def get_invoice_by_invoice_id(session: AsyncSession, invoice_id: str) -> Invoice | None:
    result = await session.execute(select(Invoice).where(Invoice.invoice_id == invoice_id))
    return result.scalar_one_or_none()


async def mark_invoice_paid(session: AsyncSession, invoice: Invoice) -> None:
    invoice.status = "paid"
    await session.commit()


async def all_active_subscriptions(session: AsyncSession) -> list[Subscription]:
    result = await session.execute(select(Subscription))
    return list(result.scalars().all())


# --- Тарифы: config.PLANS + админ-переопределения (вкл/выкл, цена) ---


async def get_plans(session: AsyncSession, include_disabled: bool = False) -> list[dict]:
    """Тарифы из config.PLANS с учётом переопределений админа.
    По умолчанию — только включённые (для показа клиенту в боте/мини-аппе)."""
    result = await session.execute(select(PlanOverride))
    overrides = {o.plan_code: o for o in result.scalars().all()}

    plans = []
    for p in PLANS:
        o = overrides.get(p["code"])
        enabled = o.enabled if o is not None else True
        if not enabled and not include_disabled:
            continue
        plans.append(
            {
                "code": p["code"],
                "title": p["title"],
                "days": p["days"],
                "price_usdt": o.price_usdt if (o is not None and o.price_usdt is not None) else p["price_usdt"],
                "price_rub": o.price_rub if (o is not None and o.price_rub is not None) else p["price_rub"],
                "enabled": enabled,
            }
        )
    return plans


async def get_plan(session: AsyncSession, plan_code: str, include_disabled: bool = False) -> dict | None:
    """Один тариф с учётом переопределений. Если он отключён и include_disabled=False — None
    (как будто тарифа не существует), даже если он есть в config.PLANS."""
    plans = await get_plans(session, include_disabled=True)
    plan = next((p for p in plans if p["code"] == plan_code), None)
    if plan is None or (not plan["enabled"] and not include_disabled):
        return None
    return plan


async def _get_or_create_plan_override(session: AsyncSession, plan_code: str) -> PlanOverride:
    result = await session.execute(select(PlanOverride).where(PlanOverride.plan_code == plan_code))
    override = result.scalar_one_or_none()
    if override is None:
        override = PlanOverride(plan_code=plan_code)
        session.add(override)
    return override


async def set_plan_enabled(session: AsyncSession, plan_code: str, enabled: bool) -> None:
    override = await _get_or_create_plan_override(session, plan_code)
    override.enabled = enabled
    await session.commit()


async def set_plan_price(
    session: AsyncSession,
    plan_code: str,
    price_rub: float | None = None,
    price_usdt: float | None = None,
) -> None:
    override = await _get_or_create_plan_override(session, plan_code)
    if price_rub is not None:
        override.price_rub = price_rub
    if price_usdt is not None:
        override.price_usdt = price_usdt
    await session.commit()


async def reset_plan_price(session: AsyncSession, plan_code: str) -> None:
    """Сбрасывает переопределённую цену обратно к значению из config.PLANS."""
    override = await _get_or_create_plan_override(session, plan_code)
    override.price_rub = None
    override.price_usdt = None
    await session.commit()


class PromoError(Exception):
    pass


async def create_promo_code(
    session: AsyncSession,
    code: str,
    type_: str,
    value: float,
    max_uses: int | None = None,
    expires_at: dt.datetime | None = None,
    extra_value: float | None = None,
    discount_uses: int | None = None,
    discount_valid_days: int | None = None,
) -> PromoCode:
    code = code.strip().upper()
    result = await session.execute(select(PromoCode).where(PromoCode.code == code))
    if result.scalar_one_or_none() is not None:
        raise PromoError(f"Промокод {code} уже существует")

    promo = PromoCode(
        code=code,
        type=type_,
        value=value,
        max_uses=max_uses,
        expires_at=expires_at,
        extra_value=extra_value,
        discount_uses=discount_uses,
        discount_valid_days=discount_valid_days,
    )
    session.add(promo)
    await session.commit()
    await session.refresh(promo)
    return promo


async def get_promo_by_code(session: AsyncSession, code: str) -> PromoCode | None:
    result = await session.execute(select(PromoCode).where(PromoCode.code == code.strip().upper()))
    return result.scalar_one_or_none()


async def all_promo_codes(session: AsyncSession) -> list[PromoCode]:
    result = await session.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))
    return list(result.scalars().all())


async def set_promo_active(session: AsyncSession, promo: PromoCode, active: bool) -> None:
    promo.active = active
    await session.commit()


async def redeem_promo_code(session: AsyncSession, code: str, user: User) -> PromoCode:
    """
    Атомарно проверяет и погашает промокод для пользователя.
    Бросает PromoError с человекочитаемым текстом, если код нельзя применить.
    """
    promo = await get_promo_by_code(session, code)
    if promo is None:
        raise PromoError("Такого промокода не существует")
    if not promo.active:
        raise PromoError("Этот промокод больше не активен")
    if promo.expires_at is not None and promo.expires_at < dt.datetime.utcnow():
        raise PromoError("Срок действия промокода истёк")
    if promo.max_uses is not None and promo.used_count >= promo.max_uses:
        raise PromoError("Лимит активаций этого промокода исчерпан")

    result = await session.execute(
        select(PromoRedemption).where(
            PromoRedemption.promo_id == promo.id, PromoRedemption.user_id == user.id
        )
    )
    if result.scalar_one_or_none() is not None:
        raise PromoError("Вы уже использовали этот промокод")

    if promo.type == "partner":
        # Партнёрские коды дают пробный период + скидку — это одноразовая
        # плюшка на пользователя в принципе, а не на конкретный код: если
        # юзер уже гасил ЛЮБОЙ другой partner-промокод, второй раз (даже
        # другим кодом того же типа) применить нельзя.
        other_partner_used = await session.execute(
            select(PromoRedemption)
            .join(PromoCode, PromoRedemption.promo_id == PromoCode.id)
            .where(PromoRedemption.user_id == user.id, PromoCode.type == "partner")
        )
        if other_partner_used.first() is not None:
            raise PromoError("Вы уже использовали партнёрский промокод — повторно активировать нельзя")

    promo.used_count += 1
    session.add(PromoRedemption(promo_id=promo.id, user_id=user.id))
    await session.commit()
    await session.refresh(promo)
    return promo


# ---------- вход в веб-версию личного кабинета через браузер ----------
# Схема: бот по /start weblogin выдаёт одноразовый LoginToken -> пользователь
# переходит по ссылке на сайт -> сайт меняет его на долгоживущий
# BrowserSession через POST /api/browser-login -> дальше сайт шлёт
# `Authorization: Bearer <session_token>` вместо `tma <initData>`.


async def create_login_token(session: AsyncSession, tg_id: int, username: str | None) -> str:
    token = secrets.token_urlsafe(32)
    session.add(
        LoginToken(
            token=token,
            tg_id=tg_id,
            username=username,
            expires_at=dt.datetime.utcnow() + dt.timedelta(seconds=LOGIN_TOKEN_TTL_SECONDS),
        )
    )
    await session.commit()
    return token


async def set_login_token_message(session: AsyncSession, token: str, chat_id: int, message_id: int) -> None:
    """Запоминает, в каком сообщении бота лежит кнопка "Открыть в браузере",
    чтобы после успешного входа отредактировать именно его."""
    result = await session.execute(select(LoginToken).where(LoginToken.token == token))
    login_token = result.scalar_one_or_none()
    if login_token is None:
        return
    login_token.chat_id = chat_id
    login_token.message_id = message_id
    await session.commit()


class LoginResult:
    __slots__ = ("browser_session", "chat_id", "message_id")

    def __init__(self, browser_session: BrowserSession, chat_id: int | None, message_id: int | None) -> None:
        self.browser_session = browser_session
        self.chat_id = chat_id
        self.message_id = message_id


async def exchange_login_token(session: AsyncSession, token: str) -> LoginResult | None:
    """Одноразово гасит login-токен и создаёт взамен него браузерную сессию.
    Возвращает None, если токен неизвестен, уже использован или истёк."""
    result = await session.execute(select(LoginToken).where(LoginToken.token == token))
    login_token = result.scalar_one_or_none()
    if login_token is None or login_token.used_at is not None:
        return None
    if login_token.expires_at < dt.datetime.utcnow():
        return None

    login_token.used_at = dt.datetime.utcnow()
    browser_session = BrowserSession(
        token=secrets.token_urlsafe(32),
        tg_id=login_token.tg_id,
        username=login_token.username,
        expires_at=dt.datetime.utcnow() + dt.timedelta(days=BROWSER_SESSION_TTL_DAYS),
    )
    session.add(browser_session)
    await session.commit()
    await session.refresh(browser_session)
    return LoginResult(browser_session, login_token.chat_id, login_token.message_id)


async def get_browser_session(session: AsyncSession, token: str) -> BrowserSession | None:
    result = await session.execute(select(BrowserSession).where(BrowserSession.token == token))
    browser_session = result.scalar_one_or_none()
    if browser_session is None or browser_session.revoked_at is not None:
        return None
    if browser_session.expires_at < dt.datetime.utcnow():
        return None
    return browser_session


async def revoke_browser_session(session: AsyncSession, token: str) -> None:
    result = await session.execute(select(BrowserSession).where(BrowserSession.token == token))
    browser_session = result.scalar_one_or_none()
    if browser_session is not None and browser_session.revoked_at is None:
        browser_session.revoked_at = dt.datetime.utcnow()
        await session.commit()
