import urllib.parse

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from config import PLANS, config


def main_menu_kb() -> InlineKeyboardMarkup:
    if not config.webapp_url:
        # Без настроенного webapp_url кнопки некуда открывать — отдаём пустую клавиатуру,
        # чтобы не показывать пользователю нерабочие кнопки.
        return InlineKeyboardMarkup(inline_keyboard=[])

    base = config.webapp_url.rstrip("/")
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Личный кабинет", web_app=WebAppInfo(url=base), style="Success")],
            [InlineKeyboardButton(text="🛒 Продлить подписку", web_app=WebAppInfo(url=f"{base}#plans-title"), style="Success")],
            [InlineKeyboardButton(text="📱 Подключить устройство", web_app=WebAppInfo(url=f"{base}#connect-device"), style="Danger")],
            [InlineKeyboardButton(text="🌐 Открыть в браузере", callback_data="weblogin")],
        ]
    )


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
            [InlineKeyboardButton(text="🚀 Открыть приложение", web_app=WebAppInfo(url=url), style="Success")],
        ]
    )


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")]]
    )


def plans_kb() -> InlineKeyboardMarkup:
    rows = []
    for plan in PLANS:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{plan['title']} — {plan['price_usdt']}$ / {plan['price_rub']}₽",
                    callback_data=f"plan:{plan['code']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def payment_method_kb(
    plan_code: str,
    balance_enough: bool,
    balance_label: str,
    is_free: bool = False,
) -> InlineKeyboardMarkup:
    rows = []
    if is_free:
        rows.append(
            [InlineKeyboardButton(text="🎁 Активировать бесплатно", callback_data=f"paymethod:{plan_code}:free")]
        )
        rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    if balance_enough:
        rows.append(
            [InlineKeyboardButton(text=f"💰 Оплатить с баланса ({balance_label})", callback_data=f"paymethod:{plan_code}:balance")]
        )
    rows.append(
        [InlineKeyboardButton(text="💎 Крипта (CryptoBot)", callback_data=f"paymethod:{plan_code}:cryptobot")]
    )
    rows.append(
        [InlineKeyboardButton(text="💳 СБП (Platega)", callback_data=f"paymethod:{plan_code}:platega")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_kb(pay_url: str, invoice_id: str, provider: str = "cryptobot", back_callback: str = "buy") -> InlineKeyboardMarkup:
    label = "💳 Оплатить в CryptoBot" if provider == "cryptobot" else "💳 Оплатить через СБП"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=pay_url)],
            [InlineKeyboardButton(text="✅ Я оплатил, проверить", callback_data=f"check:{invoice_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)],
        ]
    )
