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

import datetime as dt
import logging

from aiohttp import web

from config import PLANS, TOPUP_PRESETS_RUB, config
from database.db import (
    PromoError,
    adjust_balance,
    async_session,
    consume_discount_use,
    create_invoice,
    exchange_login_token,
    get_browser_session,
    get_effective_discount,
    get_invoice_by_invoice_id,
    get_or_create_user,
    get_subscription,
    mark_invoice_paid,
    revoke_browser_session,
)
from handlers.user import _apply_discount, _grant_subscription, _redeem_promo_for_user
from services.cryptobot_api import cryptopay_client
from services.platega_api import platega_client
from services.vpn_provider import VpnProviderError
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
                for p in PLANS
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
        browser_session = await exchange_login_token(session, token)
    if browser_session is None:
        raise ApiError(
            "Ссылка для входа недействительна, устарела или уже использована. "
            "Запросите новую в боте: /start weblogin",
            status=401,
        )

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
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
        sub = await get_subscription(session, user.id)
        await get_effective_discount(session, user)  # почистит истёкшую скидку, если есть
    return web.json_response(_profile_json(user, sub))


@routes.get("/api/devices")
async def get_devices(request: web.Request) -> web.Response:
    await _tg_user_from_request(request)  # только проверяем авторизацию
    return web.json_response(
        {
            "message": (
                "Ограничения на количество устройств нет — используйте одну и ту же "
                "ссылку-подписку на всех своих устройствах одновременно."
            )
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
        invoice = await platega_client.create_invoice(
            amount_rub=amount,
            comment=f"{config.vpn_name}: пополнение баланса",
            order_id=f"topup-user{user.tg_id}-{int(dt.datetime.utcnow().timestamp())}",
            user_id=user.tg_id,
            username=tg_user.get("username"),
        )
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

    plan = next((p for p in PLANS if p["code"] == plan_code), None)
    if plan is None:
        raise ApiError("Такого плана не существует")
    if provider not in ("free", "balance", "cryptobot", "platega"):
        raise ApiError("Некорректный способ оплаты")

    async with async_session() as session:
        user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
        discount = await get_effective_discount(session, user)
        price_usdt, price_rub = _apply_discount(plan, discount)

        if provider == "free":
            if price_rub > 0:
                raise ApiError("Скидка недостаточна для бесплатной активации")
            try:
                subscription_url = await _grant_subscription(
                    session, tg_user["id"], tg_user.get("username"), plan_code, plan["days"]
                )
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
            except VpnProviderError as e:
                await adjust_balance(session, user, price_rub)  # откатываем списание
                raise ApiError(
                    "Не удалось выдать доступ: ошибка связи с VPN-панелью. Средства не списаны", status=502
                ) from e
            if discount > 0:
                await consume_discount_use(session, user)
            return web.json_response({"status": "granted", "subscription_url": subscription_url})

        # provider in (cryptobot, platega) -> выставляем счёт, оплата и выдача доступа — отдельным шагом
        # через GET /api/invoice/{id} (аналог кнопки "Я оплатил" в боте)
        if provider == "platega":
            invoice = await platega_client.create_invoice(
                amount_rub=price_rub,
                comment=f"{config.vpn_name}: подписка {plan['title']}",
                order_id=f"user{user.tg_id}-{plan_code}-{int(dt.datetime.utcnow().timestamp())}",
                user_id=user.tg_id,
                username=tg_user.get("username"),
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
            amount, currency = price_rub, "RUB"
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

        plan = next((p for p in PLANS if p["code"] == local_invoice.plan_code), None)
        days = plan["days"] if plan else 30
        try:
            subscription_url = await _grant_subscription(
                session, tg_user["id"], tg_user.get("username"), local_invoice.plan_code, days
            )
        except VpnProviderError as e:
            raise ApiError(
                "Оплата найдена, но не удалось выдать доступ из-за ошибки VPN-панели. Напишите в поддержку",
                status=502,
            ) from e

        if local_invoice.discount_percent > 0:
            user = await get_or_create_user(session, tg_user["id"], tg_user.get("username"))
            await consume_discount_use(session, user)
        await mark_invoice_paid(session, local_invoice)

    return web.json_response({"status": "paid", "subscription_url": subscription_url})


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


def create_app() -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app.add_routes(routes)

    # aiohttp по умолчанию не роутит OPTIONS для каждого маршрута — добавляем catch-all
    # для preflight-запросов браузера.
    async def _preflight(request: web.Request) -> web.Response:
        return web.Response()

    app.router.add_route("OPTIONS", "/{tail:.*}", _preflight)
    return app
