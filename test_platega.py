"""
Быстрая проверка интеграции с Platega.io без запуска всего бота.

Запуск (из корня проекта, с настроенным .env):
    python test_platega.py

Что делает:
  1. Создаёт тестовый счёт на 10 ₽ через СБП (paymentMethod=2) и печатает
     ссылку на оплату — по ней можно реально оплатить и проверить, что
     деньги проходят.
  2. Опрашивает статус этого счёта раз в 5 секунд, пока вы не оплатите
     (или не прервёте Ctrl+C) — так проверяется связка create -> poll.

Если на шаге 1 сразу падает ошибка — не так MerchantId/Secret, либо
аккаунт не прошёл модерацию в Platega. Текст ошибки от API печатается
целиком, обычно там прямо сказано, что не так.
"""

import asyncio

from config import config
from services.platega_api import platega_client


def _mask(value: str) -> str:
    if not value:
        return "<пусто>"
    if len(value) <= 8:
        return value[0] + "…" + value[-1]
    return value[:4] + "…" + value[-4:] + f" (длина {len(value)})"


async def main():
    print("Проверяю, что реально загружено из .env:")
    print("  PLATEGA_MERCHANT_ID:", _mask(config.platega_merchant_id))
    print("  PLATEGA_SECRET:     ", _mask(config.platega_secret))
    print("  PLATEGA_API_URL:    ", config.platega_api_url)
    print()

    print("Создаю тестовый счёт на 10 ₽ через СБП...")
    invoice = await platega_client.create_invoice(
        amount_rub=10,
        comment="Тестовый платёж (test_platega.py)",
        order_id="test-platega-script",
    )
    print("Счёт создан:")
    print("  invoice_id:", invoice["invoice_id"])
    print("  pay_url:   ", invoice["pay_url"])
    print()
    print("Откройте pay_url и оплатите (или просто оставьте — echo статус ниже).")
    print("Опрашиваю статус каждые 5 секунд, Ctrl+C для выхода...")

    while True:
        await asyncio.sleep(5)
        status = await platega_client.get_invoice(invoice["invoice_id"])
        print("  ->", status)
        if status and status.get("status") == "paid":
            print("Оплачено! Интеграция работает.")
            break


if __name__ == "__main__":
    asyncio.run(main())
