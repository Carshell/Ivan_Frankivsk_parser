"""Одноразовий інтерактивний вхід у Telegram-акаунт для Telethon.

Запусти це ОДИН РАЗ САМ у своєму терміналі (не через Claude — код підтвердження
прийде тобі в Telegram і ввести його має саме людина):

    cd project
    python telegam/login_telegram.py

Скрипт запитає номер телефону, код підтвердження і, якщо увімкнена 2FA, пароль.
Після успішного входу створюється telegam/channels_session.session — далі
channels.py використовує його автоматично, без повторних запитів.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from telethon.sync import TelegramClient  # .sync патчить методи клієнта на синхронні поза asyncio-циклом

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_PATH = BASE_DIR / "channels_session"

if __name__ == "__main__":
    with TelegramClient(str(SESSION_PATH), API_ID, API_HASH) as client:
        me = client.get_me()
        print(f"Успішно увійшли як: {me.first_name} (@{me.username})")
