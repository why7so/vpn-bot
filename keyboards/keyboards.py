import urllib.parse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import DEVICE_QTY_PRESETS, config


def main_menu_kb() -> InlineKeyboardMarkup:
    rows = []
    if config.webapp_url:
        base = config.webapp_url.rstrip("/")
        rows.append(
            [
                InlineKeyboardButton(
                    text="Личный кабинет",
                    web_app=WebAppInfo(url=base),
                    style="success",
                    icon_custom_emoji_id=config.icon_emoji_account,
                )
            ]
        )
        rows.append(
            [
                InlineKeyboardButton(
                    text="Подключить устройство",
                    web_app=WebAppInfo(url=f"{base}#connect-device"),
                    style="success",
                    icon_custom_emoji_id=config.icon_emoji_connect_device,
                )
            ]
        )
    # Тарифы/цены и оплата — бот-нативный флоу (см. handlers/user.py: "buy" ->
    # plans_kb -> payment_method_kb), работает всегда, даже если WEBAPP_URL
    # не настроен (в отличие от кнопок веб-приложения выше).
    rows.append(
        [
            InlineKeyboardButton(
                text="Тарифы и оплата",
                callback_data="buy",
                style="danger",
                icon_custom_emoji_id=config.icon_emoji_buy,
            )
        ]
    )
    # "О сервисе" — подменю с поддержкой, соглашением и политикой
    # конфиденциальности (см. about_kb).
    rows.append(
        [
            InlineKeyboardButton(
                text="О сервисе",
                callback_data="about",
                icon_custom_emoji_id=config.icon_emoji_about,
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def about_kb() -> InlineKeyboardMarkup:
    """Подменю "О сервисе": поддержка, соглашение, политика конфиденциальности."""
    rows = []
    rows.append(
        [InlineKeyboardButton(text="Реферальная программа", callback_data="referral")]
    )
    # Контакт тех. поддержки. Если SUPPORT_USERNAME не задан в .env — вместо
    # ссылки показываем заглушку с подсказкой администратору настроить его.
    if config.support_username:
        support_url = f"https://t.me/{config.support_username.lstrip('@')}"
        rows.append(
            [InlineKeyboardButton(text="Поддержка", url=support_url, icon_custom_emoji_id=config.icon_emoji_support)]
        )
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="Поддержка", callback_data="support_stub", icon_custom_emoji_id=config.icon_emoji_support
                )
            ]
        )
    # Пользовательское соглашение и политика конфиденциальности отдаются
    # как статические страницы веб-приложения (см. webapp-frontend/terms.html,
    # privacy.html) — ссылки доступны только если настроен WEBAPP_URL.
    if config.webapp_url:
        base = config.webapp_url.rstrip("/")
        rows.append([InlineKeyboardButton(text="Соглашение", url=f"{base}/terms")])
        rows.append([InlineKeyboardButton(text="Конфиденциальность", url=f"{base}/privacy")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def promo_result_kb(popup_text: str) -> InlineKeyboardMarkup:
    """Кнопка открытия мини-приложения с результатом активации промокода.

    Результат передаётся приложению через query-параметр ?promo_popup=...,
    а не отдельным сообщением в чате: app.js при загрузке покажет его через
    Telegram.WebApp.showPopup — нативное всплывающее окно.
    """
    base = config.webapp_url.rstrip("/")
    url = f"{base}?promo_popup={urllib.parse.quote(popup_text)}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Открыть приложение", web_app=WebAppInfo(url=url), style="success")],
        ]
    )


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="back_main")]]
    )


def plans_kb(plans: list[dict]) -> InlineKeyboardMarkup:
    """plans — список тарифов (уже отфильтрованных по enabled и с ценами
    с учётом переопределений), см. database.db.get_plans."""
    rows = []
    for plan in plans:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan['title']} — {plan['price_usdt']}$ / {plan['price_rub']}₽",
                    callback_data=f"plan:{plan['code']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_kb(
    plan_code: str,
    balance_enough: bool,
    balance_label: str,
    is_free: bool = False,
    platega_label: str | None = None,
) -> InlineKeyboardMarkup:
    rows = []
    if is_free:
        rows.append(
            [InlineKeyboardButton(text="Активировать бесплатно", callback_data=f"paymethod:{plan_code}:free")]
        )
        rows.append([InlineKeyboardButton(text="Назад", callback_data="buy")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if balance_enough:
        rows.append(
            [InlineKeyboardButton(text=f"Оплатить с баланса ({balance_label})", callback_data=f"paymethod:{plan_code}:balance")]
        )
    rows.append(
        [InlineKeyboardButton(text="Крипта (CryptoBot)", callback_data=f"paymethod:{plan_code}:cryptobot")]
    )
    platega_text = f"СБП (Platega) — {platega_label}" if platega_label else "СБП (Platega)"
    rows.append(
        [InlineKeyboardButton(text=platega_text, callback_data=f"paymethod:{plan_code}:platega")]
    )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_kb(pay_url: str, invoice_id: str, provider: str = "cryptobot", back_callback: str = "buy") -> InlineKeyboardMarkup:
    label = "Оплатить в CryptoBot" if provider == "cryptobot" else "Оплатить через СБП"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=pay_url)],
            [InlineKeyboardButton(text="Я оплатил, проверить", callback_data=f"check:{invoice_id}")],
            [InlineKeyboardButton(text="Назад", callback_data=back_callback)],
        ]
    )


def _device_word(qty: int) -> str:
    """Русское склонение слова 'устройство' под число (простая эвристика,
    для чисел из DEVICE_QTY_PRESETS этого достаточно: 1, 2-4, 5+)."""
    if qty % 10 == 1 and qty % 100 != 11:
        return "устройство"
    if 2 <= qty % 10 <= 4 and not (12 <= qty % 100 <= 14):
        return "устройства"
    return "устройств"


def devices_kb() -> InlineKeyboardMarkup:
    """Клавиатура выбора количества докупаемых устройств."""
    rows = []
    for qty in DEVICE_QTY_PRESETS:
        price_rub = round(config.extra_device_price_rub * qty)
        price_usdt = round(config.extra_device_price_usdt * qty, 2)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"+{qty} {_device_word(qty)} — {price_rub}₽ / {price_usdt}$",
                    callback_data=f"devqty:{qty}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def device_payment_method_kb(qty: int, balance_enough: bool, balance_label: str) -> InlineKeyboardMarkup:
    rows = []
    if balance_enough:
        rows.append(
            [InlineKeyboardButton(text=f"Оплатить с баланса ({balance_label})", callback_data=f"devpay:{qty}:balance")]
        )
    rows.append(
        [InlineKeyboardButton(text="Крипта (CryptoBot)", callback_data=f"devpay:{qty}:cryptobot")]
    )
    rows.append(
        [InlineKeyboardButton(text="СБП (Platega)", callback_data=f"devpay:{qty}:platega")]
    )
    rows.append([InlineKeyboardButton(text="Назад", callback_data="devices")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
