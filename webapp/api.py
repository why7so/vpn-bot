"""
REST API для Telegram Mini App (WebApp) — «личный кабинет» пользователя внутри бота.

Авторизация: каждый запрос несёт заголовок `Authorization: tma <initData>`,
где initData — то, что Telegram кладёт в `window.Telegram.WebApp.initData`
на фронтенде. Мы проверяем подпись (webapp/auth.py) и достаём tg_id/username
из неё — никаких паролей/токенов заводить не нужно.

Бизнес-логика (выдача подписки, промокоды, скидки, инвойсы) переиспользуется
из handlers/user.py и database/db.py, чтобы не дублировать её между ботом и API.
"""

from __future__ import annotations

import base64
import datetime as dt
import logging
import secrets

from aiohttp import web

from config import DEVICE_QTY_PRESETS, TOPUP_PRESETS_RUB, config
from database.db import (
    PromoError,
    add_extra_devices,
    adjust_balance,
    async_session,
    consume_discount_use,
    create_invoice,
    effective_device_limit,
    remaining_device_capacity,
    exchange_login_token,
    get_browser_session,
    get_effective_discount,
    get_invoice_by_invoice_id,
    get_or_create_user,
    get_plan,
    get_plans as list_plans,
    get_subscription,
    get_user_by_sub_token,
    get_user_by_tg_id,
    get_user_by_vpn_client_uuid,
    mark_invoice_paid,
    revoke_browser_session,
)
from handlers.user import _apply_discount, _grant_subscription, _redeem_promo_for_user
from services.cryptobot_api import cryptopay_client
from services.platega_api import PlategaError, platega_client
from services.vpn_provider import VpnProviderError, vpn_client
from webapp.auth import extract_bearer_init_data, extract_bearer_session_token, validate_init_data

logger = logging.getLogger(__name__)

routes = web.RouteTableDef()


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


async def _tg_user_from_request(request: web.Request) -> dict:
    """Достаёт tg-пользователя из запроса. Поддерживает два способа авторизации:

    1. Telegram Mini App: `Authorization: tma <initData>` — подпись проверяется
       ключом бота (см. webapp/auth.py::validate_init_data).
    2. Обычный браузер вне Telegram: `Authorization: Bearer <session_token>`,
       выданный через POST /api/browser-login после перехода по одноразовой
       ссылке из бота (команда /start weblogin, см. handlers/user.py).

    Бросает ApiError(401), если ни один способ не подошёл.
    """
    authorization = request.headers.get("Authorization")

    init_data = extract_bearer_init_data(authorization) or request.query.get("init_data")
    if init_data:
        parsed = validate_init_data(init_data, config.bot_token)
        if parsed is not None:
            user = parsed.get("user")
            if isinstance(user, dict) and "id" in user:
                return user

    session_token = extract_bearer_session_token(authorization) or request.query.get("session_token")
    if session_token:
        async with async_session() as session:
            browser_session = await get_browser_session(session, session_token)
        if browser_session is not None:
            return {"id": browser_session.tg_id, "username": browser_session.username}

    raise ApiError("Неверная или устаревшая авторизация", status=401)


def _profile_json(user, sub) -> dict:
    subscription = None
    if sub is not None:
        subscription = {
            "plan_code": sub.plan_code,
            "expires_at": sub.expires_at.isoformat() + "Z",
            "subscription_url": sub.subscription_url,
            "active": sub.expires_at > dt.datetime.utcnow(),
        }
    return {
        "tg_id": user.tg_id,
        "username": user.username,
        "balance": user.balance,
        "discount_percent": user.discount_percent,
        "discount_uses_left": user.discount_uses_left,
        "discount_expires_at": user.discount_expires_at.isoformat() + "Z" if user.discount_expires_at else None,
        "subscription": subscription,
        "vpn_name": config.vpn_name,
        "support_username": config.support_username or None,
    }


@routes.get("/api/plans")
async def get_plans(request: web.Request) -> web.Response:
    async with async_session() as session:
        plans = await list_plans(session)  # только включённые, с учётом цен админа
    return web.json_response(
        {
            "plans": [
                {
                    "code": p["code"],
                    "title": p["title"],
                    "days": p["days"],
                    "price_usdt": p["price_usdt"],
                    "price_rub": p["price_rub"],
                }
                for p in plans
            ],
            "topup_presets_rub": TOPUP_PRESETS_RUB,
        }
    )


@routes.post("/api/browser-login")
async def browser_login(request: web.Request) -> web.Response:
    """Обменивает одноразовый login-токен (из ссылки /start weblogin в боте)
    на долгоживущую сессию для обычного браузера. Токена в теле запроса
    достаточно — сам факт его знания подтверждает переход по ссылке из бота."""
    body = await request.json()
    token = (body.get("token") or "").strip()
    if not token:
        raise ApiError("Не указан токен входа")

    async with async_session() as session:
        login_result = await exchange_login_token(session, token)
    if login_result is None:
        raise ApiError(
            "Ссылка для входа недействительна, устарела или уже использована. "
            "Запросите новую в боте: /start weblogin",
            status=401,
        )

    # Редактируем исходное сообщение с кнопкой "Открыть в браузере" в чате бота:
    # убираем кнопку и показываем, что вход уже выполнен, чтобы по одноразовой
    # ссылке нельзя было (и незачем) нажать ещё раз.
    bot = request.app.get("bot")
    if bot is not None and login_result.chat_id and login_result.message_id:
        try:
            await bot.edit_message_text(
                chat_id=login_result.chat_id,
                message_id=login_result.message_id,
                text="✅ Вход выполнен успешно! Можно вернуться в браузер — личный кабинет уже открыт.",
                reply_markup=None,
            )
        except Exception:
            # Сообщение могли уже удалить/отредактировать вручную — это не повод
            # ронять сам вход, пользователь уже получил сессию ниже.
            logger.exception(
                "Не удалось отредактировать сообщение с кнопкой входа (chat_id=%s, message_id=%s)",
                login_result.chat_id,
                login_result.message_id,
            )

    browser_session = login_result.browser_session
    return web.json_response(
        {
            "session_token": browser_session.token,
            "tg_id": browser_session.tg_id,
            "username": browser_session.username,
        }
    )


@routes.post("/api/logout")
async def logout(request: web.Request) -> web.Response:
    """Отзывает браузерную сессию (Authorization: Bearer <session_token>).
    На Telegram Mini App (tma initData) не влияет — там сессий нет."""
    session_token = extract_bearer_session_token(request.headers.get("Authorization"))
    if session_token:
        async with async_session() as session:
            await revoke_browser_session(session, session_token)
    return web.json_response({"status": "ok"})


@routes.get("/api/me")
async def get_me(request: web.Request) -> web.Response:
    tg_user = await _tg_user_from_request(request)
    async with async_session() as session:
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"), tg_user.get("first_name"))
        sub = await get_subscription(session, user.id)
        await get_effective_discount(session, user)  # почистит истёкшую скидку, если есть
    return web.json_response(_profile_json(user, sub))


@routes.get("/api/devices")
async def get_devices(request: web.Request) -> web.Response:
    tg_user = await _tg_user_from_request(request)
    async with async_session() as session:
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"), tg_user.get("first_name"))
        limit = effective_device_limit(user)

    limit_line = "без ограничений" if limit <= 0 else f"максимум {limit} устройств одновременно"
    capacity = remaining_device_capacity(user)
    return web.json_response(
        {
            "device_limit": limit,
            "base_device_limit": config.device_limit,
            "extra_devices": user.extra_devices,
            "price_rub": config.extra_device_price_rub,
            "price_usdt": config.extra_device_price_usdt,
            "qty_presets": DEVICE_QTY_PRESETS,
            "max_device_limit": config.max_device_limit if config.max_device_limit > 0 else None,
            "remaining_capacity": capacity,
            "message": (
                f"У вас {limit_line} по одной ссылке-подписке. "
                "Нужно больше — докупите устройства ниже."
            ),
        }
    )


async def _grant_extra_devices(session, tg_id: int, username: str | None, qty: int) -> int:
    """Начисляет qty доп. устройств и пытается сразу применить новый лимит
    у VPN-провайдера (best-effort — см. handlers/user.py:_grant_extra_devices)."""
    user = await get_or_create_user(session, tg_id, username)
    user = await add_extra_devices(session, user, qty)
    new_limit = effective_device_limit(user)
    try:
        await vpn_client.update_device_limit(user.tg_id, new_limit)
    except Exception:
        logger.exception("Не удалось применить новый лимит устройств сразу для tg_id=%s", tg_id)
    return new_limit


@routes.post("/api/devices/purchase")
async def purchase_devices(request: web.Request) -> web.Response:
    """provider: balance | cryptobot | platega"""
    tg_user = await _tg_user_from_request(request)
    body = await request.json()
    try:
        qty = int(body.get("qty"))
    except (TypeError, ValueError) as e:
        raise ApiError("Некорректное количество устройств") from e
    provider = body.get("provider")

    if qty not in DEVICE_QTY_PRESETS:
        raise ApiError("Некорректное количество устройств")
    if provider not in ("balance", "cryptobot", "platega"):
        raise ApiError("Некорректный способ оплаты")

    price_usdt = round(config.extra_device_price_usdt * qty, 2)
    price_rub = round(config.extra_device_price_rub * qty)

    async with async_session() as session:
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))

        capacity = remaining_device_capacity(user)
        if capacity is not None and qty > capacity:
            raise ApiError(
                f"Нельзя купить {qty} устройств — доступно ещё {capacity} из {config.max_device_limit} "
                "(потолок протокола на одну ссылку-подписку)."
            )

        if provider == "balance":
            if user.balance < price_rub:
                raise ApiError("Недостаточно средств на балансе")
            await adjust_balance(session, user, -price_rub)
            new_limit = await _grant_extra_devices(session, tg_user["id"], tg_user.get("username"), qty)
            return web.json_response({"status": "granted", "device_limit": new_limit})

        # provider in (cryptobot, platega) -> выставляем счёт, начисление устройств — отдельным
        # шагом через GET /api/invoice/{id} после оплаты
        if provider == "platega":
            try:
                invoice = await platega_client.create_invoice(
                    amount_rub=price_rub,
                    comment=f"{config.vpn_name}: +{qty} устройств",
                    order_id=f"devices-user{user.tg_id}-{qty}-{int(dt.datetime.utcnow().timestamp())}",
                    user_id=user.tg_id,
                    username=tg_user.get("username"),
                )
            except PlategaError as e:
                logger.exception("Platega error while creating devices invoice for tg_id=%s", user.tg_id)
                raise ApiError("Не удалось создать счёт на оплату. Попробуйте позже", status=502) from e
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
            amount, currency = price_rub, "RUB"
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
            amount, currency = price_usdt, "USDT"

    return web.json_response(
        {
            "status": "invoice",
            "invoice_id": str(invoice["invoice_id"]),
            "pay_url": invoice["pay_url"],
            "amount": amount,
            "currency": currency,
        }
    )


@routes.post("/api/promo")
async def redeem_promo(request: web.Request) -> web.Response:
    tg_user = await _tg_user_from_request(request)
    body = await request.json()
    code = (body.get("code") or "").strip()
    if not code:
        raise ApiError("Не указан промокод")

    try:
        text = await _redeem_promo_for_user(tg_user["id"], tg_user.get("username"), code)
    except PromoError as e:
        raise ApiError(str(e)) from e
    except VpnProviderError as e:
        logger.exception("VPN provider error while redeeming promo (webapp) for tg_id=%s", tg_user["id"])
        raise ApiError("Промокод принят, но не удалось выдать доступ из-за ошибки VPN-панели", status=502) from e

    return web.json_response({"message": text})


@routes.post("/api/topup")
async def topup(request: web.Request) -> web.Response:
    tg_user = await _tg_user_from_request(request)
    body = await request.json()
    try:
        amount = float(body.get("amount"))
    except (TypeError, ValueError) as e:
        raise ApiError("Некорректная сумма") from e
    if amount < 10:
        raise ApiError("Минимальная сумма пополнения — 10 ₽")

    async with async_session() as session:
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
        try:
            invoice = await platega_client.create_invoice(
                amount_rub=amount,
                comment=f"{config.vpn_name}: пополнение баланса",
                order_id=f"topup-user{user.tg_id}-{int(dt.datetime.utcnow().timestamp())}",
                user_id=user.tg_id,
                username=tg_user.get("username"),
            )
        except PlategaError as e:
            logger.exception("Platega error while creating topup invoice for tg_id=%s", user.tg_id)
            raise ApiError("Не удалось создать счёт на оплату. Попробуйте позже", status=502) from e
        await create_invoice(
            session,
            user_id=user.id,
            invoice_id=str(invoice["invoice_id"]),
            pay_url=invoice["pay_url"],
            amount=amount,
            purpose="topup",
            provider="platega",
            currency="RUB",
        )

    return web.json_response(
        {"invoice_id": str(invoice["invoice_id"]), "pay_url": invoice["pay_url"], "amount": amount, "currency": "RUB"}
    )


@routes.post("/api/purchase")
async def purchase(request: web.Request) -> web.Response:
    """provider: free | balance | cryptobot | platega"""
    tg_user = await _tg_user_from_request(request)
    body = await request.json()
    plan_code = body.get("plan_code")
    provider = body.get("provider")
    try:
        extra_qty = int(body.get("extra_devices_qty") or 0)
    except (TypeError, ValueError):
        raise ApiError("Некорректное количество устройств")

    if provider not in ("free", "balance", "cryptobot", "platega"):
        raise ApiError("Некорректный способ оплаты")
    if extra_qty < 0 or extra_qty > 50:
        raise ApiError("Некорректное количество устройств")

    async with async_session() as session:
        plan = await get_plan(session, plan_code)
        if plan is None:
            raise ApiError("Такого плана не существует")
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
        discount = await get_effective_discount(session, user)
        price_usdt, price_rub = _apply_discount(plan, discount)
        # Доп. устройства не участвуют в скидке на тариф — цена за них фиксированная,
        # как и при самостоятельной покупке через "Докупить устройства".
        if extra_qty:
            price_usdt = round(price_usdt + config.extra_device_price_usdt * extra_qty, 2)
            price_rub = round(price_rub + config.extra_device_price_rub * extra_qty)

            capacity = remaining_device_capacity(user)
            if capacity is not None and extra_qty > capacity:
                raise ApiError(
                    f"Нельзя докупить {extra_qty} устройств — доступно ещё {capacity} из "
                    f"{config.max_device_limit} (потолок протокола на одну ссылку-подписку)."
                )

        if provider == "free":
            if price_rub > 0:
                raise ApiError("Скидка недостаточна для бесплатной активации")
            try:
                subscription_url = await _grant_subscription(
                    session, tg_user["id"], tg_user.get("username"), plan_code, plan["days"]
                )
                if extra_qty:
                    await _grant_extra_devices(session, tg_user["id"], tg_user.get("username"), extra_qty)
                if discount > 0:
                    await consume_discount_use(session, user)
            except VpnProviderError as e:
                raise ApiError("Не удалось выдать доступ: ошибка связи с VPN-панелью", status=502) from e
            return web.json_response({"status": "granted", "subscription_url": subscription_url})

        if provider == "balance":
            if user.balance < price_rub:
                raise ApiError("Недостаточно средств на балансе")
            await adjust_balance(session, user, -price_rub)
            try:
                subscription_url = await _grant_subscription(
                    session, tg_user["id"], tg_user.get("username"), plan_code, plan["days"]
                )
                if extra_qty:
                    await _grant_extra_devices(session, tg_user["id"], tg_user.get("username"), extra_qty)
            except VpnProviderError as e:
                await adjust_balance(session, user, price_rub)  # откатываем списание
                raise ApiError(
                    "Не удалось выдать доступ: ошибка связи с VPN-панелью. Средства не списаны", status=502
                ) from e
            if discount > 0:
                await consume_discount_use(session, user)
            return web.json_response({"status": "granted", "subscription_url": subscription_url})

        # provider in (cryptobot, platega) -> выставляем счёт, оплата и выдача доступа — отдельным шагом
        # через GET /api/invoice/{id} (аналог кнопки "Я оплатил" в боте). Кол-во доп.
        # устройств едет вместе со счётом в invoice.quantity и начисляется там же.
        devices_comment = f" + {extra_qty} устройств" if extra_qty else ""
        balance_credit = 0.0
        if provider == "platega":
            # Автопримешивание баланса: если денег на балансе не хватает на
            # полную оплату (иначе сработала бы ветка provider == "balance"
            # выше), но что-то есть — спишем эту часть ПОСЛЕ подтверждения
            # оплаты остатка (см. /api/invoice/{id} ниже), а через провайдера
            # выставим счёт только на остаток.
            balance_credit = round(min(user.balance, price_rub), 2) if price_rub > 0 else 0.0
            remaining_rub = round(price_rub - balance_credit)

            if remaining_rub <= 0:
                # Баланс успел стать достаточным между загрузкой страницы и
                # нажатием кнопки — просто выдаём полностью с баланса.
                await adjust_balance(session, user, -price_rub)
                try:
                    subscription_url = await _grant_subscription(
                        session, tg_user["id"], tg_user.get("username"), plan_code, plan["days"]
                    )
                    if extra_qty:
                        await _grant_extra_devices(session, tg_user["id"], tg_user.get("username"), extra_qty)
                except VpnProviderError as e:
                    await adjust_balance(session, user, price_rub)  # откатываем списание
                    raise ApiError(
                        "Не удалось выдать доступ: ошибка связи с VPN-панелью. Средства не списаны", status=502
                    ) from e
                if discount > 0:
                    await consume_discount_use(session, user)
                return web.json_response({"status": "granted", "subscription_url": subscription_url})

            try:
                invoice = await platega_client.create_invoice(
                    amount_rub=remaining_rub,
                    comment=f"{config.vpn_name}: подписка {plan['title']}{devices_comment}",
                    order_id=f"user{user.tg_id}-{plan_code}-{int(dt.datetime.utcnow().timestamp())}",
                    user_id=user.tg_id,
                    username=tg_user.get("username"),
                )
            except PlategaError as e:
                logger.exception("Platega error while creating purchase invoice for tg_id=%s", user.tg_id)
                raise ApiError("Не удалось создать счёт на оплату. Попробуйте позже", status=502) from e
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
                quantity=extra_qty or None,
                balance_credit=balance_credit,
            )
            amount, currency = remaining_rub, "RUB"
        else:
            invoice = await cryptopay_client.create_invoice(
                amount_usdt=price_usdt,
                description=f"{config.vpn_name}: подписка {plan['title']}{devices_comment}",
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
                quantity=extra_qty or None,
            )
            amount, currency = price_usdt, "USDT"

    return web.json_response(
        {
            "status": "invoice",
            "invoice_id": str(invoice["invoice_id"]),
            "pay_url": invoice["pay_url"],
            "amount": amount,
            "currency": currency,
            "balance_credit": balance_credit,
        }
    )


@routes.get("/api/invoice/{invoice_id}")
async def invoice_status(request: web.Request) -> web.Response:
    """Проверяет статус счёта у провайдера и, если оплачен, выдаёт доступ/зачисляет баланс
    (аналог кнопки «Я оплатил» в боте). Идемпотентно — повторные вызовы после выдачи безопасны."""
    tg_user = await _tg_user_from_request(request)
    invoice_id = request.match_info["invoice_id"]

    async with async_session() as session:
        local_invoice = await get_invoice_by_invoice_id(session, invoice_id)
        if local_invoice is None:
            raise ApiError("Счёт не найден", status=404)

        if local_invoice.status == "paid":
            return web.json_response({"status": "paid", "already_processed": True})

        if local_invoice.provider == "platega":
            remote_invoice = await platega_client.get_invoice(invoice_id)
        else:
            remote_invoice = await cryptopay_client.get_invoice(invoice_id)

        if remote_invoice is None or remote_invoice.get("status") != "paid":
            return web.json_response({"status": "pending"})

        if local_invoice.purpose == "topup":
            user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
            user = await adjust_balance(session, user, local_invoice.amount)
            await mark_invoice_paid(session, local_invoice)
            return web.json_response({"status": "paid", "balance": user.balance})

        if local_invoice.purpose == "devices":
            new_limit = await _grant_extra_devices(
                session, tg_user["id"], tg_user.get("username"), local_invoice.quantity or 0
            )
            await mark_invoice_paid(session, local_invoice)
            return web.json_response({"status": "paid", "device_limit": new_limit})

        # include_disabled=True: тариф уже оплачен раньше, доступ выдаём в любом
        # случае, даже если админ отключил его после создания счёта.
        plan = await get_plan(session, local_invoice.plan_code, include_disabled=True)
        days = plan["days"] if plan else 30
        try:
            subscription_url = await _grant_subscription(
                session, tg_user["id"], tg_user.get("username"), local_invoice.plan_code, days
            )
            if local_invoice.quantity:
                await _grant_extra_devices(session, tg_user["id"], tg_user.get("username"), local_invoice.quantity)
        except VpnProviderError as e:
            raise ApiError(
                "Оплата найдена, но не удалось выдать доступ из-за ошибки VPN-панели. Напишите в поддержку",
                status=502,
            ) from e

        if local_invoice.discount_percent > 0 or local_invoice.balance_credit > 0:
            user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
            if local_invoice.discount_percent > 0:
                await consume_discount_use(session, user)
            if local_invoice.balance_credit > 0:
                # Списываем ровно после успешной выдачи доступа, не раньше —
                # min() на случай, если баланс уменьшился с момента создания
                # счёта другой покупкой.
                await adjust_balance(session, user, -min(local_invoice.balance_credit, user.balance))
        await mark_invoice_paid(session, local_invoice)

    return web.json_response({"status": "paid", "subscription_url": subscription_url})


@routes.get("/sub/{token}")
async def get_subscription_config(request: web.Request) -> web.Response:
    """Ссылка-подписка для VPN-клиента (Happ/INCY/v2rayNG и т.п.) —
    ПУБЛИЧНЫЙ роут без Telegram-авторизации: сам VPN-клиент не умеет её
    передавать, безопасность держится на непредсказуемости sub_token в пути.

    Отдаёт conf-строки из SUBSCRIPTION_NODE_LINKS (общие для всех
    пользователей — per-user провижининг появится вместе с мастер-сервером,
    см. services/vpn_provider.py) плюс заголовок subscription-userinfo с
    РЕАЛЬНЫМ сроком действия подписки этого пользователя (total=0 — Happ
    показывает это как безлимитный трафик "∞", upload/download=0, потому что
    per-node учёт трафика тоже появится только с мастер-сервером).

    support-url — иконка Telegram-поддержки в приложении (см.
    https://www.happ.su/main/dev-docs/app-management#link-to-the-support-page),
    стандартный параметр, не требует ничего доп.

    sub-info-text/providerid — "карточка" с тарифом и днями подписки в
    приложении (см. скриншот из тикета) — это "Advanced announcements" Happ,
    для которых нужен Provider ID: отдельная регистрация на
    https://happ-proxy.com (сторонний сервис Happ, не часть этого проекта).
    Пока config.happ_provider_id не задан — эти заголовки просто не
    отправляются, остальное продолжает работать как раньше.
    """
    token = request.match_info["token"]

    async with async_session() as session:
        user = await get_user_by_sub_token(session, token)
        if user is None:
            raise web.HTTPNotFound(text="subscription not found")
        sub = await get_subscription(session, user.id)
        plan = await get_plan(session, sub.plan_code) if sub and sub.plan_code else None

    now = dt.datetime.utcnow()
    expire_at = sub.expires_at if sub else now
    expire_ts = int(expire_at.replace(tzinfo=dt.timezone.utc).timestamp())

    body = "\n".join(config.subscription_node_links)
    body_b64 = base64.b64encode(body.encode("utf-8")).decode("ascii")

    filename = "".join(ch for ch in config.vpn_name if ch.isalnum()) or "subscription"
    headers = {
        "profile-title": config.vpn_name,
        "subscription-userinfo": f"upload=0; download=0; total=0; expire={expire_ts}",
        "content-disposition": f'attachment; filename="{filename}"',
        "profile-update-interval": "1",
    }

    if config.support_username:
        headers["support-url"] = f"https://t.me/{config.support_username.lstrip('@')}"
    if config.webapp_url:
        headers["profile-web-page-url"] = config.webapp_url

    if config.happ_provider_id:
        headers["providerid"] = config.happ_provider_id
        days_left = max(0, (expire_at - now).days) if sub else 0
        info_lines = []
        if plan:
            info_lines.append(f"Тариф: {plan['title']}")
        info_lines.append(f"Осталось {days_left} дн. подписки" if sub else "Подписка не активна")
        if config.support_username:
            info_lines.append(f"Telegram — @{config.support_username.lstrip('@')}")
        # HTTP-заголовки не допускают перевод строки внутри значения —
        # многострочный текст передаём как base64 (тот же паттерн, что
        # у profile-title/announce в документации Happ).
        info_text = "\n".join(info_lines)
        headers["sub-info-text"] = "base64:" + base64.b64encode(info_text.encode("utf-8")).decode("ascii")
        headers["sub-expire"] = "1"

    return web.Response(text=body_b64, headers=headers, content_type="text/plain")


@routes.post("/webhook/device-connected")
async def device_connected_webhook(request: web.Request) -> web.Response:
    """Вебхук от мастер-сервера: реальное подключение устройства к VPN.

    ЗАГЛУШКА-ИНФРАСТРУКТУРА: мастер-сервер с телеметрией подключений ещё не
    построен (см. services/vpn_provider.py), поэтому этот эндпоинт пока
    никто не вызывает. Он существует, чтобы контракт был готов заранее —
    когда мастер-сервер появится, ему останется просто POST-ить сюда при
    каждом новом подключении клиента, и пользователь тут же получит
    уведомление в бота.

    Авторизация: заголовок X-Webhook-Secret должен совпадать с
    MASTER_WEBHOOK_SECRET из .env. Если секрет не настроен — эндпоинт
    отключён (503), чтобы по умолчанию не быть открытым всем.

    Тело запроса (JSON), все поля опциональны кроме идентификатора юзера:
    {
        "tg_id": 123456789,          // либо tg_id, либо client_uuid — один из двух
        "client_uuid": "...",        // uuid клиента у VPN-провайдера (см. set_vpn_client_uuid)
        "device_name": "iPhone 15 Pro",
        "os": "iOS 18.1",
        "ip": "203.0.113.42",
        "node": "nl-01",             // название/локация VPN-ноды, к которой подключились
        "connected_at": "2026-08-17T12:00:00Z"   // ISO 8601, по умолчанию — сейчас
    }
    """
    if not config.master_webhook_secret:
        raise web.HTTPServiceUnavailable(text="MASTER_WEBHOOK_SECRET is not configured")

    provided_secret = request.headers.get("X-Webhook-Secret", "")
    if not secrets.compare_digest(provided_secret, config.master_webhook_secret):
        raise web.HTTPUnauthorized(text="invalid webhook secret")

    try:
        body = await request.json()
    except Exception:
        raise ApiError("Invalid JSON body")

    tg_id = body.get("tg_id")
    client_uuid = body.get("client_uuid")
    if not tg_id and not client_uuid:
        raise ApiError("Either tg_id or client_uuid is required")

    async with async_session() as session:
        if tg_id:
            user = await get_user_by_tg_id(session, int(tg_id))
        else:
            user = await get_user_by_vpn_client_uuid(session, str(client_uuid))

    if user is None:
        raise web.HTTPNotFound(text="user not found")

    bot = request.app.get("bot")
    if bot is None:
        # API запущен без бота (например, отдельный процесс) — уведомить некому.
        logger.warning("device-connected webhook received but no bot instance is attached to the app")
        return web.json_response({"status": "ok", "notified": False})

    device_name = (body.get("device_name") or "").strip()
    os_name = (body.get("os") or "").strip()
    ip = (body.get("ip") or "").strip()
    node = (body.get("node") or "").strip()
    connected_at_raw = body.get("connected_at")
    try:
        connected_at = dt.datetime.fromisoformat(connected_at_raw.replace("Z", "+00:00")) if connected_at_raw else dt.datetime.utcnow()
    except ValueError:
        connected_at = dt.datetime.utcnow()

    lines = ["🔌 <b>Новое устройство подключилось к VPN</b>", ""]
    lines.append(f"📱 Устройство: {device_name}" if device_name else "📱 Устройство: неизвестно")
    if os_name:
        lines.append(f"💻 ОС: {os_name}")
    if ip:
        lines.append(f"🌐 IP: <code>{ip}</code>")
    if node:
        lines.append(f"📍 Сервер: {node}")
    lines.append(f"🕐 Время: {connected_at.strftime('%d.%m.%Y, %H:%M UTC')}")

    try:
        await bot.send_message(chat_id=user.tg_id, text="\n".join(lines))
        notified = True
    except Exception:
        logger.exception("Failed to send device-connected notification to tg_id=%s", user.tg_id)
        notified = False

    return web.json_response({"status": "ok", "notified": notified})


@web.middleware
async def cors_middleware(request: web.Request, handler):
    origin = config.api_cors_origin
    if request.method == "OPTIONS":
        resp = web.Response()
    else:
        try:
            resp = await handler(request)
        except ApiError as e:
            resp = web.json_response({"error": e.message}, status=e.status)
        except web.HTTPException:
            raise
        except Exception:
            logger.exception("Unhandled error in webapp API: %s %s", request.method, request.path)
            resp = web.json_response({"error": "Внутренняя ошибка сервера"}, status=500)

    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def create_app(bot=None) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.add_routes(routes)
    # bot нужен только для редактирования сообщения с кнопкой "Открыть в браузере"
    # после успешного входа (см. /api/browser-login выше). Без него всё остальное
    # API продолжит работать как обычно, просто без этой мелкой полировки UX.
    app["bot"] = bot

    # aiohttp по умолчанию не роутит OPTIONS для каждого маршрута — добавляем catch-all
    # для preflight-запросов браузера.
    async def _preflight(request: web.Request) -> web.Response:
        return web.Response()

    app.router.add_route("OPTIONS", "/{tail:.*}", _preflight)
    return app
