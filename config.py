import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _get_admin_ids() -> list[int]:
    raw = os.getenv("ADMIN_IDS", "")
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


def _normalize_database_url(url: str) -> str:
    """Railway (и Heroku/Render) кладут DATABASE_URL от Postgres-плагина в
    виде "postgres://..." или "postgresql://..." — без указания async-
    драйвера. SQLAlchemy в этом случае по умолчанию берёт sync psycopg2,
    который несовместим с create_async_engine — бот падал бы при старте
    с ошибкой "requires an async driver". Принудительно проставляем
    +asyncpg, если драйвер не указан явно."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


@dataclass
class Config:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=_get_admin_ids)

    # --- Выдача VPN-доступа ---
    # services/vpn_provider.py — пока мастер-сервер не готов, работает как
    # заглушка (см. докстринг в самом файле).
    master_api_base_url: str = os.getenv("MASTER_API_BASE_URL", "")
    master_api_token: str = os.getenv("MASTER_API_TOKEN", "")
    vpn_email_prefix: str = os.getenv("VPN_EMAIL_PREFIX", "tgbot_")
    # Максимальное число одновременных подключений (устройств) на одного клиента.
    # Используется провайдером VPN-доступа (см. services/vpn_provider.py).
    device_limit: int = int(os.getenv("DEVICE_LIMIT", "3"))
    # Жёсткий потолок на ИТОГОВЫЙ лимит устройств (базовый + докупленные) по
    # одной ссылке-подписке — ограничение самого протокола/VPN-панели: выше
    # него клиент технически не может иметь больше одновременных подключений.
    # 0 = без потолка (не рекомендуется — тогда докупить устройства можно
    # неограниченно, даже сверх того, что протокол реально способен обслужить).
    max_device_limit: int = int(os.getenv("MAX_DEVICE_LIMIT", "60"))
    # Цена одного дополнительного устройства сверх лимита (доп. услуга,
    # см. handlers/user.py: "devices"). Действует бессрочно, пока активна подписка.
    extra_device_price_rub: float = float(os.getenv("EXTRA_DEVICE_PRICE_RUB", "40"))
    extra_device_price_usdt: float = float(os.getenv("EXTRA_DEVICE_PRICE_USDT", "0.45"))
    referral_bonus_rub: float = float(os.getenv("REFERRAL_BONUS_RUB", "15"))
    # Бонус самому приглашённому другу — начисляется один раз при первом
    # /start по реф. ссылке, независимо от бонуса рефереру выше.
    referral_invitee_bonus_rub: float = float(os.getenv("REFERRAL_INVITEE_BONUS_RUB", "49"))
    trial_days: int = int(os.getenv("TRIAL_DAYS", "3"))  # 0 = отключить бесплатный пробный период

    crypto_pay_token: str = os.getenv("CRYPTO_PAY_TOKEN", "")
    crypto_pay_api_url: str = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")

    # --- Platega (platega.io) — оплата в рублях через СБП ---
    # MerchantId и Secret выдаются менеджером при подключении, также доступны
    # в личном кабинете на странице «Настройки». Документация: https://docs.platega.io/
    platega_merchant_id: str = os.getenv("PLATEGA_MERCHANT_ID", "")
    platega_secret: str = os.getenv("PLATEGA_SECRET", "")
    platega_api_url: str = os.getenv("PLATEGA_API_URL", "https://app.platega.io")

    # БД: если задан DATABASE_URL — используем его (общая PostgreSQL на
    # мастер-сервере, формат postgresql+asyncpg://user:pass@host:5432/dbname).
    # Если не задан — фолбэк на локальный SQLite-файл (для разработки/старых
    # деплоев без мастер-сервера). db_path сохранён отдельно ради обратной
    # совместимости — на случай, если где-то ещё читается напрямую.
    db_path: str = os.getenv("DB_PATH", "bot.db")
    database_url: str = _normalize_database_url(os.getenv("DATABASE_URL", "") or f"sqlite+aiosqlite:///{db_path}")
    support_username: str = os.getenv("SUPPORT_USERNAME", "")
    vpn_name: str = os.getenv("VPN_NAME", "Unnamed VPN")

    # Кастомные эмодзи-иконки на кнопках (Bot API 9.4, icon_custom_emoji_id).
    # Работает, только если у владельца бота есть Telegram Premium (или бот
    # купил доп. юзернейм на Fragment) — иначе Telegram просто покажет кнопку
    # без иконки. ID эмодзи можно получить, переслав боту сообщение с нужным
    # премиум-эмодзи — админ-команда /getemojiid в handlers/admin.py вернёт ID.
    # Пусто по умолчанию — кнопки рендерятся как обычно, без иконок.
    #
    # ВАЖНО: icon_custom_emoji_id — это String в Telegram Bot API. Приводим
    # явным str(...), а не полагаемся на то, что переменная окружения придёт
    # строкой — некоторые платформы деплоя (Docker Compose с нецитированным
    # числом в YAML и т.п.) могут подсунуть число, и pydantic-валидация
    # aiogram упадёт с ValidationError при отправке /start.
    def _icon_emoji(env_name: str, default: str = "") -> str | None:
        raw = os.getenv(env_name, default)
        raw = str(raw).strip() if raw is not None else ""
        return raw or None

    icon_emoji_buy: str = _icon_emoji("ICON_EMOJI_BUY", "5904462880941545555")
    icon_emoji_account: str = _icon_emoji("ICON_EMOJI_ACCOUNT", "6035084557378654059")
    icon_emoji_connect_device: str = _icon_emoji("ICON_EMOJI_CONNECT_DEVICE", "6028171274939797252")
    icon_emoji_about: str = _icon_emoji("ICON_EMOJI_ABOUT", "6028435952299413210")
    icon_emoji_support: str = os.getenv("ICON_EMOJI_SUPPORT", "") or None

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

    # --- Ссылка-подписка (/sub/<token> в webapp/api.py) ---
    # Публичный базовый URL, на котором отвечает webapp/api.py (тот же сервис,
    # что и бот, — см. main.py: aiohttp поднимается на API_HOST:API_PORT).
    # Например https://vpn-bot-production-555c.up.railway.app — БЕЗ слэша на конце.
    subscription_base_url: str = os.getenv("SUBSCRIPTION_BASE_URL", "").rstrip("/")
    # Строки-конфиги (vless://…, hysteria2://… и т.п.), которые отдаются всем
    # пользователям в теле подписки — по одной на строку. Пока нет
    # мастер-сервера с per-user провижининг — сервера общие для всех клиентов,
    # уникальность на пользователя даёт только сам sub_token в URL (не палит
    # tg_id/UUID) и метаданные (дни/трафик), которые /sub/<token> считает
    # индивидуально по подписке пользователя.
    subscription_node_links_raw: str = os.getenv("SUBSCRIPTION_NODE_LINKS", "")

    @property
    def subscription_node_links(self) -> list[str]:
        return [line.strip() for line in self.subscription_node_links_raw.splitlines() if line.strip()]


def build_subscription_url(sub_token: str) -> str:
    """URL ссылки-подписки для VPN-клиента (Happ/INCY/v2rayNG и т.п.).
    Пусто, если SUBSCRIPTION_BASE_URL ещё не настроен в .env."""
    if not config.subscription_base_url:
        return ""
    return f"{config.subscription_base_url}/sub/{sub_token}"


# Тарифные планы: (название, дней, цена в USDT / цена в RUB)
PLANS = [
    {"code": "1m", "title": "1 месяц", "days": 30, "price_usdt": 1.99, "price_rub": 149},
    {"code": "3m", "title": "3 месяца", "days": 90, "price_usdt": 3.99, "price_rub": 299},
    {"code": "12m", "title": "12 месяцев", "days": 365, "price_usdt": 9.99, "price_rub": 749},
]

# Пресеты пополнения баланса (в рублях, оплата через Platega/СБП)
TOPUP_PRESETS_RUB = [100, 300, 500, 1000]

# Пресеты количества докупаемых устройств (доп. услуга сверх DEVICE_LIMIT)
DEVICE_QTY_PRESETS = [4, 6, 8, 10]

config = Config()
