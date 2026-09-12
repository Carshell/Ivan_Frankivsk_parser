"""Одноразовий інтерактивний вхід у Instagram для instagrapi.

Запусти це ОДИН РАЗ САМ у своєму терміналі (не через Claude — якщо Instagram
запросить код підтвердження при першому вході з нового місця, він прийде на
пошту/SMS цього акаунта, і ввести його має саме людина):

    cd project
    python instagram/login_instagram.py

Після успішного входу створюється instagram/session.json — далі parser.py
використовує його автоматично, без повторного логіну (поки сесія жива).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from instagrapi import Client

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env")

USERNAME = os.environ["INSTAGRAM_USERNAME"]
PASSWORD = os.environ["INSTAGRAM_PASSWORD"]
SESSION_FILE = BASE_DIR / "session.json"


def _challenge_code_handler(username: str, choice) -> str:
    return input(
        f"Instagram надіслав код підтвердження для акаунта {username} "
        f"(SMS/email, спосіб: {choice}). Введи код: "
    ).strip()


if __name__ == "__main__":
    client = Client()
    client.challenge_code_handler = _challenge_code_handler

    if SESSION_FILE.exists():
        client.load_settings(SESSION_FILE)

    client.login(USERNAME, PASSWORD)
    client.dump_settings(SESSION_FILE)

    me = client.account_info()
    print(f"Успішно увійшли як: {me.username}")
