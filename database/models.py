import datetime as dt

from sqlalchemy import BigInteger, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    xui_client_uuid: Mapped[str | None] = mapped_column(String, nullable=True)
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    discount_uses_left: Mapped[int | None] = mapped_column(Integer, nullable=True)  # None = безлимит до истечения срока
    discount_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)

    subscription: Mapped["Subscription"] = relationship(back_populates="user", uselist=False)
    invoices: Mapped[list["Invoice"]] = relationship(back_populates="user")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    plan_code: Mapped[str] = mapped_column(String)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime)
    subscription_url: Mapped[str | None] = mapped_column(String, nullable=True)

    user: Mapped["User"] = relationship(back_populates="subscription")


class Invoice(Base):
    """Счёт, выставленный через один из платёжных провайдеров (CryptoBot, LAVA)."""

    __tablename__ = "invoices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    plan_code: Mapped[str | None] = mapped_column(String, nullable=True)
    purpose: Mapped[str] = mapped_column(String, default="subscription")  # subscription | topup
    provider: Mapped[str] = mapped_column(String, default="cryptobot")  # cryptobot | lava
    invoice_id: Mapped[str] = mapped_column(String, unique=True)  # id счёта у провайдера
    pay_url: Mapped[str] = mapped_column(String)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String, default="USDT")  # USDT | RUB
    discount_percent: Mapped[float] = mapped_column(Float, default=0.0)  # скидка, применённая к этому счёту
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
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    redeemed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=dt.datetime.utcnow)
