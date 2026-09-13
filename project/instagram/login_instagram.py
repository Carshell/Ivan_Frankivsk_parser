"""Одноразовий інтерактивний вхід у Instagram для instagrapi.

Запусти це ОДИН РАЗ САМ у своєму терміналі (не через Claude — якщо Instagram
запросить код підтвердження при першому вході з нового місця, він прийде на
пошту/SMS цього акаунта, і ввести його має саме людина):

    cd project
    python instagram/login_instagram.py

Після успішного входу створюється instagram/session.json — далі parser.py
використовує його автоматично, без повторного логіну (поки сесія жива).

Акаунт має двофакторну автентифікацію (2FA) через застосунок-автентифікатор —
код підтвердження генерується автоматично з TOTP-секрету (INSTAGRAM_TOTP_SECRET
у .env), ручне введення коду не потрібне.

Якщо звичайний логін падає з "Your version of Instagram is out of date" —
це поточний баг instagrapi (Instagram вимагає новішу версію застосунку, ніж
видає бібліотека; трапляється навіть на найновішому релізі, бо Instagram
змінює вимогу швидше, ніж встигають виходити фікси). Обхід — увійти через
sessionid із реального браузера замість пароля:
  1. Залогинься на instagram.com у звичайному браузері саме цим акаунтом
  2. DevTools → Application/Storage → Cookies → https://www.instagram.com → sessionid
  3. Постав INSTAGRAM_SESSIONID=<значення> у .env і запусти цей скрипт знову
"""

from __future__ import annotations

import os
from pathlib import Path

import pyotp
from dotenv import load_dotenv
from instagrapi import Client

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env")

USERNAME = os.environ["INSTAGRAM_USERNAME"]
PASSWORD = os.environ["INSTAGRAM_PASSWORD"]
SESSIONID = os.environ.get("INSTAGRAM_SESSIONID")
TOTP_SECRET = os.environ.get("INSTAGRAM_TOTP_SECRET")
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

    if SESSIONID:
        client.login_by_sessionid(SESSIONID)
    elif TOTP_SECRET:
        client.login(USERNAME, PASSWORD, verification_code=pyotp.TOTP(TOTP_SECRET).now())
    else:
        client.login(USERNAME, PASSWORD)

    client.dump_settings(SESSION_FILE)

    me = client.account_info()
    print(f"Успішно увійшли як: {me.username}")
