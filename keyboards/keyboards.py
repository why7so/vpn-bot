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
            [InlineKeyboardButton(text="🚀 Личный кабинет", web_app=WebAppInfo(url=base))],
            [InlineKeyboardButton(text="📱 Подключить устройство", web_app=WebAppInfo(url=f"{base}#connect-device"))],
            [InlineKeyboardButton(text="🛒 Продлить подписку", web_app=WebAppInfo(url=f"{base}#plans-title"))],
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
        [InlineKeyboardButton(text="💳 Рубли — карта/СБП (LAVA)", callback_data=f"paymethod:{plan_code}:lava")]
    )
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="buy")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def invoice_kb(pay_url: str, invoice_id: str, provider: str = "cryptobot", back_callback: str = "buy") -> InlineKeyboardMarkup:
    label = "💳 Оплатить в CryptoBot" if provider == "cryptobot" else "💳 Оплатить (LAVA)"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, url=pay_url)],
            [InlineKeyboardButton(text="✅ Я оплатил, проверить", callback_data=f"check:{invoice_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=back_callback)],
        ]
    )