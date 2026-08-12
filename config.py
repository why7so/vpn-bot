import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=_get_admin_ids)

    # --- Выдача VPN-доступа ---
    # Раньше здесь была конфигурация 3x-ui, сейчас интеграция отключена.
    # services/vpn_provider.py — временная заглушка; когда подключим
    # собственный мастер-сервер, реквизиты для него пойдут сюда:
    master_api_base_url: str = os.getenv("MASTER_API_BASE_URL", "")
    master_api_token: str = os.getenv("MASTER_API_TOKEN", "")
    vpn_email_prefix: str = os.getenv("VPN_EMAIL_PREFIX", "tgbot_")

    crypto_pay_token: str = os.getenv("CRYPTO_PAY_TOKEN", "")
    crypto_pay_api_url: str = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

    # --- Platega (platega.io) — оплата в рублях через СБП ---
    # MerchantId и Secret выдаются менеджером при подключении, также доступны
    # в личном кабинете на странице «Настройки». Документация: https://docs.platega.io/
    platega_merchant_id: str = os.getenv("PLATEGA_MERCHANT_ID", "")
    platega_secret: str = os.getenv("PLATEGA_SECRET", "")
    platega_api_url: str = os.getenv("PLATEGA_API_URL", "https://app.platega.io")

    db_path: str = os.getenv("DB_PATH", "bot.db")
    support_username: str = os.getenv("SUPPORT_USERNAME", "")
    vpn_name: str = os.getenv("VPN_NAME", "Unnamed VPN")

    # --- Telegram Mini App (WebApp) ---
    # URL фронтенда мини-приложения (Vercel/Netlify), напр. https://myvpn.vercel.app
    # Если задан — в меню бота появится кнопка запуска.
    webapp_url: str = os.getenv("WEBAPP_URL", "")
    # Короткое имя мини-приложения из @BotFather (Bot Settings -> Mini Apps ->
    # short name), например "app". Нужно, чтобы кнопки промокодов
    # (t.me/<bot>/<short_name>?startapp=...) открывали сразу мини-приложение,
    # минуя чат с ботом. Если не задано — используется старый deep-link
    # в чат бота (t.me/<bot>?start=...).
    webapp_short_name: str = os.getenv("WEBAPP_SHORT_NAME", "")
    # Хост/порт, на котором поднимается HTTP API для мини-приложения (webapp/api.py).
    # Наружу должен быть доступен через reverse-proxy (nginx/caddy) с HTTPS.
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8080"))
    # Origin фронтенда для CORS, напр. https://myvpn.vercel.app ; "*" — разрешить всем (не для продакшена)
    api_cors_origin: str = os.getenv("API_CORS_ORIGIN", "*")


# Тарифные планы: (название, дней, цена в USDT / цена в RUB)
PLANS = [
    {"code": "1m", "title": "1 месяц", "days": 30, "price_usdt": 3.5, "price_rub": 349},
    {"code": "3m", "title": "3 месяца", "days": 90, "price_usdt": 9.0, "price_rub": 899},
    {"code": "12m", "title": "12 месяцев", "days": 365, "price_usdt": 30.0, "price_rub": 2990},
]

# Пресеты пополнения баланса (в рублях, оплата через Platega/СБП)
TOPUP_PRESETS_RUB = [100, 300, 500, 1000]

config = Config()
