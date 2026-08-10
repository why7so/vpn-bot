"""
Клиент к LAVA Wallet API (lava.ru) для приёма оплаты в рублях
(карта / СБП — способы оплаты выбираются на стороне LAVA на странице счёта).

Как получить токен: lava.ru -> Личный кабинет -> Настройки -> API
(https://lava.ru/dashboard/settings/api).

Важно:
- Для приёма платежей от сторонних клиентов LAVA требует статус
  самозанятого/ИП/юрлица и модерацию проекта в личном кабинете —
  это условие самого сервиса, бот тут ни при чём.
- Ниже используется публичный wallet-API (создание счёта на кошелёк).
  Реализация основана на официальной open-source библиотеке LAVA
  (https://github.com/billiedark/LavaAPI, ссылается на https://dev.lava.ru/).
  Т.к. страница dev.lava.ru рендерится через JS и не была доступна для
  автоматической проверки, ПЕРЕД боевым запуском сверьте эндпоинты и
  формат ответа с актуальной документацией в вашем личном кабинете —
  формат мог измениться.
"""

from __future__ import annotations

import uuid

import aiohttp

from config import config


class LavaError(RuntimeError):
    pass


class LavaClient:
    def __init__(self) -> None:
        self._base_url = config.lava_api_url.rstrip("/")
        self._token = config.lava_api_token

    def _headers(self) -> dict:
        return {"Authorization": self._token}

    async def _get(self, path: str, params: dict | None = None) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}{path}", headers=self._headers(), params=params
            ) as resp:
                return await resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}{path}", headers=self._headers(), data=data
            ) as resp:
                return await resp.json()

    async def _wallet_account(self) -> str:
        """Номер вашего рублёвого кошелька, на который выставляется счёт."""
        wallets = await self._get("/wallet/list")
        if not isinstance(wallets, list) or not wallets:
            raise LavaError(f"Не удалось получить список кошельков LAVA: {wallets}")
        return wallets[0]["account"]

    async def create_invoice(self, amount_rub: float, comment: str, order_id: str | None = None) -> dict:
        """
        order_id - произвольный внешний идентификатор (для сопоставления со
        своей БД). Кладём его в comment, т.к. в базовом invoice/create нет
        отдельного поля payload/metadata.
        """
        order_id = order_id or uuid.uuid4().hex
        wallet_to = await self._wallet_account()
        data = {
            "wallet_to": wallet_to,
            "sum": amount_rub,
            "comment": f"{comment} [{order_id}]",
        }
        result = await self._post("/invoice/create", data)
        if result.get("status") != "success":
            raise LavaError(f"LAVA create_invoice error: {result}")
        return {
            "invoice_id": result["id"],
            "pay_url": result["url"],
            "amount_rub": result["sum"],
            "order_id": order_id,
        }

    async def get_invoice(self, invoice_id: str) -> dict | None:
        result = await self._post("/invoice/info", {"id": invoice_id})
        invoice = result.get("invoice")
        if not invoice:
            return None
        return {
            "invoice_id": invoice_id,
            "status": "paid" if invoice.get("status") == "success" else invoice.get("status"),
        }


lava_client = LavaClient()
