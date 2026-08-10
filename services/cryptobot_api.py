"""
Клиент к Crypto Pay API (@CryptoBot) для приёма оплаты в USDT/TON/BTC и т.п.

Как получить токен: открыть в Telegram @CryptoBot -> Crypto Pay ->
Create App -> скопировать API Token.

Документация: https://help.crypt.bot/crypto-pay-api
"""

from __future__ import annotations

import aiohttp

from config import config


class CryptoPayError(RuntimeError):
    pass


class CryptoPayClient:
    def __init__(self) -> None:
        self._base_url = config.crypto_pay_api_url
        self._token = config.crypto_pay_token

    async def _request(self, method: str, params: dict | None = None) -> dict:
        headers = {"Crypto-Pay-API-Token": self._token}
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/{method}", headers=headers, params=params
            ) as resp:
                data = await resp.json()
        if not data.get("ok"):
            raise CryptoPayError(f"Crypto Pay API error: {data}")
        return data["result"]

    async def create_invoice(
        self,
        amount_usdt: float,
        description: str,
        payload: str,
        asset: str = "USDT",
    ) -> dict:
        """
        payload - произвольная строка, которую мы потом получим обратно
        в вебхуке/при проверке статуса (используем как invoice_id из нашей БД).
        """
        params = {
            "asset": asset,
            "amount": str(amount_usdt),
            "description": description,
            "payload": payload,
            "allow_comments": "false",
            "allow_anonymous": "false",
        }
        return await self._request("createInvoice", params)

    async def get_invoice(self, invoice_id: str) -> dict | None:
        result = await self._request("getInvoices", {"invoice_ids": invoice_id})
        items = result.get("items", [])
        return items[0] if items else None


cryptopay_client = CryptoPayClient()
