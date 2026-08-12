import datetime as dt
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import config
from database.models import (
    Base,
    BrowserSession,
    Invoice,
    LoginToken,
    PromoCode,
    PromoRedemption,
    Subscription,
    User,
)

engine = create_async_engine(f"sqlite+aiosqlite:///{config.db_path}")
async_session = async_sessionmaker(engine, expire_on_commit=False)

# Токен из /start weblogin живёт недолго — это просто "мостик" от бота к
# браузеру, пользователь переходит по нему сразу же.
LOGIN_TOKEN_TTL_SECONDS = 5 * 60
# А вот браузерная сессия, которую он выдаёт при обмене, живёт долго —
# чтобы не приходилось логиниться через бота при каждом визите на сайт.
BROWSER_SESSION_TTL_DAYS = 30


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


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


async def set_vpn_client_uuid(session: AsyncSession, user: User, client_uuid: str) -> None:
    user.vpn_client_uuid = client_uuid
    await session.commit()


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


async def get_subscription(session: AsyncSession, user_id: int) -> Subscription | None:
    result = await session.execute(select(Subscription).where(Subscription.user_id == user_id))
    return result.scalar_one_or_none()


async def upsert_subscription(
    session: AsyncSession,
    user_id: int,
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
    user_id: int,
    invoice_id: str,
    pay_url: str,
    amount: float,
    plan_code: str | None = None,
    purpose: str = "subscription",
    provider: str = "cryptobot",
    currency: str = "USDT",
    discount_percent: float = 0.0,
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


async def exchange_login_token(session: AsyncSession, token: str) -> BrowserSession | None:
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
    return browser_session


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
