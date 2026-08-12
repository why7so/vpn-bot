"""
Клиент к Platega.io API (https://platega.io/ru) — приём оплаты через СБП.

Документация: https://docs.platega.io/
Базовый URL: https://app.platega.io/

Авторизация — 2 заголовка на каждый запрос:
    X-MerchantId: <Ваш MerchantId>
    X-Secret:     <Ваш API ключ>
Оба выдаются менеджером при подключении и доступны в личном кабинете
на странице «Настройки».

Способ оплаты СБП с QR-кодом — paymentMethod = 2 (PaymentMethodInt.SbpQr).

Создание платежа:
    POST /transaction/process
    body: {
        "paymentMethod": 2,
        "paymentDetails": {"amount": <сумма>, "currency": "RUB"},
        "description": "...",
        "return": "<url редиректа после успешной оплаты>",
        "failedUrl": "<url редиректа при неуспехе>",
        "payload": "<произвольная строка для сопоставления со своей БД>",
    }
    ответ: {
        "paymentMethod": "SBPQR",
        "transactionId": "...",
        "redirect": "https://pay.platega.io?...",
        "status": "PENDING",
        "expiresIn": "00:15:00",
        ...
    }

Проверка статуса:
    GET /transaction/{id}
    ответ: {"id": "...", "status": "PENDING" | "CONFIRMED" | "CANCELED" | "CHARGEBACKED", ...}
"""

from __future__ import annotations

import aiohttp

from config import config

# СБП с QR-кодом (см. PaymentMethodInt в документации Platega)
SBP_QR_METHOD = 2


class PlategaError(RuntimeError):
    pass


class PlategaClient:
    def __init__(self) -> None:
        self._base_url = config.platega_api_url.rstrip("/")
        self._merchant_id = config.platega_merchant_id
        self._secret = config.platega_secret

    def _headers(self) -> dict:
        return {
            "X-MerchantId": self._merchant_id,
            "X-Secret": self._secret,
            "Content-Type": "application/json",
        }

    async def create_invoice(
        self,
        amount_rub: float,
        comment: str,
        order_id: str | None = None,
        return_url: str | None = None,
        failed_url: str | None = None,
    ) -> dict:
        payload = {
            "paymentMethod": SBP_QR_METHOD,
            "paymentDetails": {"amount": amount_rub, "currency": "RUB"},
            "description": comment,
        }
        if return_url:
            payload["return"] = return_url
        if failed_url:
            payload["failedUrl"] = failed_url
        if order_id:
            payload["payload"] = order_id

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/transaction/process", headers=self._headers(), json=payload
            ) as resp:
                result = await resp.json()
                if resp.status != 200 or not result.get("transactionId"):
                    raise PlategaError(f"Platega create_invoice error ({resp.status}): {result}")

        return {
            "invoice_id": result["transactionId"],
            "pay_url": result["redirect"],
            "amount_rub": amount_rub,
        }

    async def get_invoice(self, invoice_id: str) -> dict | None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/transaction/{invoice_id}", headers=self._headers()
            ) as resp:
                if resp.status == 404:
                    return None
                result = await resp.json()
                if resp.status != 200:
                    raise PlategaError(f"Platega get_invoice error ({resp.status}): {result}")

        status = result.get("status")
        return {
            "invoice_id": invoice_id,
            # приводим к тому же словарю статусов, что используется для остальных провайдеров
            "status": "paid" if status == "CONFIRMED" else status,
        }


platega_client = PlategaClient()
