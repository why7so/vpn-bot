import datetime as dt
import uuid

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    # UUID вместо autoincrement-integer — основной идентификатор пользователя
    # в общей схеме (см. мастер-сервер: users/subscriptions/payments и т.д.
    # ссылаются на users по этому id). tg_id остаётся отдельным полем — это
    # "точка входа" от Telegram, а не первичный ключ: апдейты от Telegram
    # всегда приходят с numeric tg_id, поэтому колонка и её lookup никуда
    # не делись, просто больше не используются как PK/FK.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    # Отображаемое имя из Telegram (message.from_user.first_name /
    # tg_user["first_name"]) — используется там, где нужен "человеческий" ник,
    # а не @username (который у многих не задан). См. /leaderboard.
    first_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # Идентификатор клиента у VPN-провайдера (сейчас — заглушка/будущий
    # мастер-сервер). Имя колонки в БД оставлено как есть ради совместимости
    # с уже существующими базами.
    vpn_client_uuid: Mapped[str | None] = mapped_column("xui_client_uuid", String, nullable=True)
    # Кол-во дополнительных устройств, докупленных сверх базового DEVICE_LIMIT
    # (см. config.device_limit и доп. услугу "Докупить устройства" в боте).
    extra_devices: Mapped[int] = mapped_column(Integer, default=0)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    discount_uses_left: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = безлимит до истечения срока
    discount_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # tg_id пригласившего пользователя (реферальная программа). Храним именно
    # tg_id, а не users.id — реф. ссылка формата ?start=ref_<TG_ID> строится
    # из него напрямую, без лишнего похода в БД за UUID при генерации ссылки.
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    subscription: Mapped["Subscription"] = relationship(back_populates="user", uselist=False)
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="user")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), unique=True)
    plan_code: Mapped[str] = mapped_column(String)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    subscription_url: Mapped[str | None] = mapped_column(String, nullable=True)

    user: Mapped["User"] = relationship(back_populates="subscription")


class Invoice(Base):
    """Счёт, выставленный через один из платёжных провайдеров (CryptoBot, Platega)."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    plan_code: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, default="subscription")  # subscription | topup | devices
    provider: Mapped[str] = mapped_column(String, default="cryptobot")  # cryptobot | platega
    # Кол-во устройств в счёте — используется только при purpose="devices"
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    invoice_id: Mapped[str] = mapped_column(String, unique=True)  # id счёта у провайдера
    pay_url: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, default="USDT")  # USDT | RUB
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)  # скидка, применённая к этому счёту
    # Сумма, которая автоматически спишется с баланса пользователя в момент
    # подтверждения оплаты этого счёта (доплата остатка через провайдера +
    # покрытие части цены балансом, чтобы не пополнять баланс отдельно).
    # 0 — баланс к этому счёту не примешивался.
    balance_credit: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String, default="active")  # active | paid | expired
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    user: Mapped["User"] = relationship(back_populates="invoices")


class PromoCode(Base):
    """
    Промокод. Типы (`type`):
      - days      — бонусные дни подписки сразу при активации (value = кол-во дней)
      - balance   — начисление на баланс сразу при активации (value = сумма в ₽)
      - discount  — скидка в % на будущую покупку/продление (value = процент 1-100,
                    100 = бесплатно). discount_uses/discount_valid_days управляют тем,
                    сколько раз и как долго скидка активна у пользователя.
      - partner   — партнёрский код: пробные дни сразу (value = кол-во дней) +
                    скидка в % на покупки (extra_value = процент), т.е. days + discount разом.
    """

    __tablename__ = "promo_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String, unique=True, index=True)
    type: Mapped[str] = mapped_column(String)  # days | balance | discount | partner
    value: Mapped[float] = mapped_column(Float)
    extra_value: Mapped[float | None] = mapped_column(Float, nullable=True)  # % скидки для partner
    discount_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = безлимит до истечения
    discount_valid_days: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = без ограничения по сроку
    max_uses: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = безлимит активаций кода
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class PromoRedemption(Base):
    """Факт использования промокода конкретным пользователем (один код — один раз на юзера)."""

    __tablename__ = "promo_redemptions"
    __table_args__ = (UniqueConstraint("promo_id", "user_id", name="uq_promo_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    promo_id: Mapped[int] = mapped_column(ForeignKey("promo_codes.id"))
    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"))
    redeemed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)


class LoginToken(Base):
    """Одноразовый токен для входа в веб-версию личного кабинета через браузер.

    Выдаётся ботом по команде /start с параметром weblogin (см. handlers/user.py)
    и живёт короткое время (см. LOGIN_TOKEN_TTL_SECONDS в database/db.py).
    После одного успешного обмена на браузерную сессию (POST /api/browser-login)
    помечается использованным и повторно применить его нельзя.
    """

    __tablename__ = "login_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    used_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    # Сообщение в Telegram-чате с кнопкой "Открыть в браузере" — чтобы после
    # успешного входа отредактировать его (убрать кнопку, показать успех).
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


class PlanOverride(Base):
    """Админ-переопределение тарифа из config.PLANS: включён/выключен и/или
    переопределённая цена. Запись создаётся только когда админ что-то
    поменял — если её нет, тариф активен и цена берётся из config.PLANS как есть.
    title/days тарифа по-прежнему берутся из config.PLANS (тут не хранятся).
    """

    __tablename__ = "plan_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_code: Mapped[str] = mapped_column(String, unique=True, index=True)
    enabled: Mapped[bool] = mapped_column(default=True)
    price_rub: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_usdt: Mapped[float | None] = mapped_column(Float, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow
    )


class BrowserSession(Base):
    """Долгоживущая сессия для входа в личный кабинет из обычного браузера
    (не Telegram Mini App). Токен хранится на фронтенде в localStorage и
    передаётся как `Authorization: Bearer <token>` — аналог `tma <initData>`,
    но для запросов вне Telegram-клиента."""

    __tablename__ = "browser_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token: Mapped[str] = mapped_column(String, unique=True, index=True)
    tg_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)


class Setting(Base):
    """Небольшой generic key-value стор для одиночных глобальных настроек,
    под которые не стоит заводить отдельную таблицу/колонку. Сейчас
    используется для точки отсчёта /leaderboard (leaderboard_reset_at) —
    см. handlers/admin.py: /leaderboard_reset."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(String, nullable=True)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)
