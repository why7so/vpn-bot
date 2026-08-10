"""
Проверка подписи Telegram WebApp initData.

Мини-приложение при каждом запросе к нашему API присылает initData — строку,
которую Telegram сам сформировал и подписал HMAC-SHA256 на основе токена бота
(см. https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app).
Если подпись не сходится — значит запрос не от настоящего Telegram-клиента
(его нельзя доверять), и мы отклоняем его.

Это единственный способ авторизации в API: никаких паролей/сессий не нужно,
достаточно каждый раз проверять initData (он живёт достаточно долго в рамках
одного открытия мини-приложения).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 86400) -> dict | None:
    """Возвращает распарсенные поля initData (включая dict `user`), либо None если подпись
    невалидна или данные устарели (max_age_seconds от auth_date)."""
    if not init_data or not bot_token:
        return None

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))

    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    auth_date = pairs.get("auth_date")
    if auth_date is None or not auth_date.isdigit():
        return None
    if (time.time() - int(auth_date)) > max_age_seconds:
        return None

    if "user" in pairs:
        try:
            pairs["user"] = json.loads(pairs["user"])
        except json.JSONDecodeError:
            return None

    return pairs


def extract_bearer_init_data(authorization_header: str | None) -> str | None:
    """Authorization: tma <initData> — рекомендованная Telegram схема заголовка."""
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "tma":
        return None
    return parts[1]
