"""Парсер Instagram-акаунтів (instagrapi): пости (фото/відео/текст) + Stories.

Каркасний етап — без хард-фільтрів (ціна/тип нерухомості). Профіль замовника
і скоринг — окремий модуль Claude (п.9 ТЗ), буде додано пізніше.

Перед першим запуском потрібен один інтерактивний вхід — див. login_instagram.py.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from instagrapi import Client
from instagrapi.exceptions import LoginRequired
from instagrapi.types import Media, Story

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env")

USERNAME = os.environ.get("INSTAGRAM_USERNAME")
PASSWORD = os.environ.get("INSTAGRAM_PASSWORD")
SESSIONID = os.environ.get("INSTAGRAM_SESSIONID")
SESSION_FILE = BASE_DIR / "session.json"

ACCOUNTS_CONFIG = BASE_DIR / "accounts.json"
STATE_FILE = BASE_DIR / "state.json"  # {"<username>": "<pk найновішого обробленого поста>"}
MEDIA_CACHE_DIR = BASE_DIR / "media_cache"

SOURCE_NAME = "instagram"
POSTS_PER_ACCOUNT = 12  # скільки останніх постів перевіряти за один цикл


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_accounts() -> list[str]:
    accounts = _load_json(ACCOUNTS_CONFIG, [])
    return [a["username"] for a in accounts if a.get("enabled")]


# Кешуємо клієнта на весь час роботи процесу — instagrapi логінитись НЕ треба
# щоцикл, якщо сесія й так жива. Логін (а тим паче парольний релогін) щоразу
# з одного пристрою — саме той патерн, який Instagram підозрює в автоматизації
# ("Підозра на автоматизовані дії", scraping_warning). Клієнт скидається в None
# лише коли реально ловимо LoginRequired під час роботи (див. parse()).
_client: Client | None = None

# Випадкова пауза (сек) перед кожним приватним запитом instagrapi — робить
# трафік менш схожим на бота, ніж рівномірний запит-у-запит без затримок.
DELAY_RANGE = [1, 3]


def _build_client() -> Client | None:
    if not USERNAME or not PASSWORD:
        logger.warning("INSTAGRAM_USERNAME / INSTAGRAM_PASSWORD не задані в .env — пропускаю instagram-парсер")
        return None
    if not SESSION_FILE.exists():
        logger.warning(
            "Немає файлу сесії %s — спершу запусти 'python instagram/login_instagram.py' один раз вручну",
            SESSION_FILE,
        )
        return None

    client = Client()
    client.delay_range = DELAY_RANGE
    client.load_settings(SESSION_FILE)
    try:
        client.login(USERNAME, PASSWORD)  # з валідною сесією instagrapi не робить повторний повний логін
    except Exception:
        # Збережена сесія протухла — instagrapi сам спробував релогін через
        # пароль, а це падає з "Your version of Instagram is out of date"
        # (поточний баг instagrapi, див. login_instagram.py). Якщо є
        # INSTAGRAM_SESSIONID — пробуємо відновитись через нього автоматично,
        # той самий обхід, що і при першому ручному логіні.
        if not SESSIONID:
            logger.exception(
                "Логін Instagram не вдався і INSTAGRAM_SESSIONID не задано в .env — "
                "пропускаю instagram-парсер цього циклу"
            )
            return None
        logger.warning("Звичайний логін Instagram не вдався, пробую відновити сесію через INSTAGRAM_SESSIONID")
        try:
            client.login_by_sessionid(SESSIONID)
        except Exception:
            logger.exception(
                "INSTAGRAM_SESSIONID теж не спрацював — потрібен новий вручну "
                "(python instagram/login_instagram.py)"
            )
            return None
        client.dump_settings(SESSION_FILE)  # зберігаємо оновлену робочу сесію
        logger.info("Instagram: сесію відновлено через INSTAGRAM_SESSIONID")

    return client


def _get_client() -> Client | None:
    global _client
    if _client is not None:
        return _client
    _client = _build_client()
    return _client


def _invalidate_client() -> None:
    """Скидає кешованого клієнта — наступний parse() спробує залогінитись заново."""
    global _client
    _client = None


def _download_media_item(client: Client, item: Any, filename: str) -> str | None:
    """Качає фото/відео на диск через авторизовану сесію instagrapi.

    Instagram CDN не дає Telegram-серверу самому підтягнути ці URL напряму
    (запит падає з "WEBPAGE_CURL_FAILED") — тому качаємо самі і завантажуємо
    в Telegram уже готові байти, як і з Telegram-каналів (telegam/channels.py)."""
    MEDIA_CACHE_DIR.mkdir(exist_ok=True)
    try:
        if item.video_url:
            path = client.video_download_by_url(str(item.video_url), filename=filename, folder=MEDIA_CACHE_DIR)
        elif item.thumbnail_url:
            path = client.photo_download_by_url(str(item.thumbnail_url), filename=filename, folder=MEDIA_CACHE_DIR)
        else:
            return None
        return str(path)
    except Exception:
        logger.exception("Не вдалося завантажити медіа %s", filename)
        return None


def _media_photos(client: Client, media: Media) -> list[str]:
    """Всі фото/відео поста (включно з каруселлю-альбомом), завантажені локально."""
    items = media.resources or [media]
    paths: list[str] = []
    for idx, item in enumerate(items):
        path = _download_media_item(client, item, f"post_{media.pk}_{idx}")
        if path:
            paths.append(path)
    return paths


def _normalize_post(client: Client, media: Media, username: str) -> dict[str, Any]:
    return {
        "source": SOURCE_NAME,
        "url": f"https://www.instagram.com/p/{media.code}/",
        "external_id": f"post:{media.pk}",
        "origin_site": username,
        "published_at": media.taken_at.isoformat() if media.taken_at else None,
        "found_at": None,
        "title": media.caption_text.splitlines()[0][:120] if media.caption_text else None,
        "description": media.caption_text or None,
        "price_usd": None,
        "price_uah": None,
        "area": None,
        "area_living": None,
        "area_kitchen": None,
        "rooms": None,
        "floor": None,
        "floor_total": None,
        "address": media.location.name if media.location else None,
        "lat": media.location.lat if media.location else None,
        "lon": media.location.lng if media.location else None,
        "photos": _media_photos(client, media),
        "contact": None,
        "features": [],
        "raw_snapshot": {"media_type": media.media_type, "pk": str(media.pk), "username": username},
    }


def _normalize_story(client: Client, story: Story, username: str) -> dict[str, Any]:
    photo = _download_media_item(client, story, f"story_{story.pk}")
    return {
        "source": SOURCE_NAME,
        "url": f"https://www.instagram.com/stories/{username}/{story.pk}/",
        "external_id": f"story:{story.pk}",
        "origin_site": username,
        "published_at": story.taken_at.isoformat() if story.taken_at else None,
        "found_at": None,
        "title": f"Story: {username}",
        "description": None,  # Stories не мають звичайного тексту-підпису, як пости
        "price_usd": None,
        "price_uah": None,
        "area": None,
        "area_living": None,
        "area_kitchen": None,
        "rooms": None,
        "floor": None,
        "floor_total": None,
        "address": None,
        "lat": None,
        "lon": None,
        "photos": [photo] if photo else [],
        "contact": None,
        "features": [],
        "raw_snapshot": {"pk": str(story.pk), "username": username},
    }


async def parse(accounts: list[str] | None = None) -> list[dict[str, Any]]:
    """Забирає нові пости і поточні Stories кожного акаунта зі списку.

    instagrapi синхронний (не asyncio) — цей парсер має async-сигнатуру лише
    заради єдиного інтерфейсу з web_pages/lun.py та telegam/channels.py, а
    сам виклик блокуючий. Якщо це почне заважати event loop-у бота, обгорнути
    у asyncio.to_thread() — поки що обсяг акаунтів для цього замалий.
    """
    client = _get_client()
    if client is None:
        return []

    accounts = accounts or _load_accounts()
    if not accounts:
        return []

    state = _load_json(STATE_FILE, {})
    listings: list[dict[str, Any]] = []

    for username in accounts:
        try:
            user_id = client.user_id_from_username(username)
            last_pk = state.get(username)

            medias = client.user_medias(user_id, amount=POSTS_PER_ACCOUNT)
            for media in medias:
                if last_pk and str(media.pk) == str(last_pk):
                    break  # дійшли до вже обробленого поста — далі все старе
                listings.append(_normalize_post(client, media, username))
            if medias:
                state[username] = str(medias[0].pk)  # найновіший — нова точка відліку

            stories = client.user_stories(user_id)
            for story in stories:
                listings.append(_normalize_story(client, story, username))

            logger.info("%s: перевірено %d постів, %d сторіз", username, len(medias), len(stories))
        except LoginRequired:
            logger.warning("Instagram: сесія протухла посеред роботи (%s) — скидаю кеш клієнта до наступного циклу", username)
            _invalidate_client()
            break
        except Exception:
            logger.exception("Не вдалося обробити акаунт %s", username)

    _save_json(STATE_FILE, state)
    return listings


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = asyncio.run(parse())
    out_path = BASE_DIR / "instagram_output.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d записів збережено в %s", len(results), out_path)
