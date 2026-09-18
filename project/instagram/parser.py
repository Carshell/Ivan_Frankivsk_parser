"""Парсер Instagram: пости (фото/відео/Reels) + Stories конкретних акаунтів
(instagram/accounts.json), а також пошук постів/Reels за хештегами
(instagram/hashtags.json) — обидва списки city-теговані під три міста (ТЗ, п.6:
"Хештеги: #орендаіванофранківськ, #нерухомістьіванофранківськ..."; той самий
патерн застосовано і до Києва/Чернівців).

Stories НЕ підтягуються за хештегом — Instagram не дає стабільного публічного
API для пошуку Stories за тегом (на відміну від постів), і вони живуть лише
24 год, тож наздогнати їх пошуком все одно нереально; Stories й далі йдуть
лише з акаунтів зі списку (client.user_stories).

instagrapi синхронний під капотом — і хештег-пошук, і акаунти виконуються в
межах того самого гейту за інтервалом (INSTAGRAM_PARSE_INTERVAL_SECONDS), тож
хештеги додають обсяг запитів за один прогін, а не частоту звернень до
Instagram. Перед першим запуском потрібен один інтерактивний вхід — див.
login_instagram.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
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

# Аварійний вимикач: коли Instagram позначає акаунт як підозрілий
# (scraping_warning), кожна нова спроба логіну (навіть невдала) тільки
# продовжує "гріти" підозру. INSTAGRAM_ENABLED=false в .env повністю зупиняє
# будь-які звернення до Instagram (без логіну і без запитів) — щоб дати
# акаунту "охолонути" — і не потребує зміни коду чи перезбірки образу.
INSTAGRAM_ENABLED = os.environ.get("INSTAGRAM_ENABLED", "true").strip().lower() not in ("0", "false", "no")

# Окремий вимикач лише для хештег-пошуку (акаунти й Stories далі працюють) —
# якщо хештеги виявляться зайвим навантаженням/ризиком для акаунта, можна
# вимкнути без зачіпання решти instagram-парсингу.
INSTAGRAM_HASHTAG_SEARCH_ENABLED = os.environ.get(
    "INSTAGRAM_HASHTAG_SEARCH_ENABLED", "true"
).strip().lower() not in ("0", "false", "no")

# Instagram банить/попереджає за занадто часті запити з одного акаунта —
# рахувати його разом з класифайдами (кожні 30 хв, main.PARSE_INTERVAL_SECONDS)
# і без того було ризиковано. Instagram/FB — раз на кілька годин (ТЗ, п.7).
# +jitter, щоб інтервал не був підозріло рівним щоразу.
INSTAGRAM_PARSE_INTERVAL_SECONDS = int(os.environ.get("INSTAGRAM_PARSE_INTERVAL_SECONDS", str(3 * 60 * 60)))
INSTAGRAM_PARSE_JITTER_SECONDS = int(os.environ.get("INSTAGRAM_PARSE_JITTER_SECONDS", str(30 * 60)))
LAST_RUN_FILE = BASE_DIR / "last_run.json"

ACCOUNTS_CONFIG = BASE_DIR / "accounts.json"
HASHTAGS_CONFIG = BASE_DIR / "hashtags.json"
STATE_FILE = BASE_DIR / "state.json"  # {"<username>": "<pk найновішого обробленого поста>"}
MEDIA_CACHE_DIR = BASE_DIR / "media_cache"

SOURCE_NAME = "instagram"
POSTS_PER_ACCOUNT = 12  # скільки останніх постів перевіряти за один цикл
HASHTAG_MEDIA_LIMIT = 15  # скільки останніх постів/Reels перевіряти на один хештег за цикл


def _seconds_until_next_run() -> float:
    """>0 — ще зарано парсити знову; <=0 — можна. Читає з диска (не з пам'яті
    процесу), щоб рестарт контейнера не скидав таймер і не тригерив миттєвий
    повторний парсинг одразу після деплою."""
    if not LAST_RUN_FILE.exists():
        return 0.0
    try:
        data = json.loads(LAST_RUN_FILE.read_text(encoding="utf-8"))
        next_at = data["last_run_at"] + data["interval_used"]
    except (json.JSONDecodeError, KeyError):
        return 0.0
    return next_at - time.time()


def _mark_run_now() -> None:
    interval = INSTAGRAM_PARSE_INTERVAL_SECONDS + random.uniform(0, INSTAGRAM_PARSE_JITTER_SECONDS)
    LAST_RUN_FILE.write_text(
        json.dumps({"last_run_at": time.time(), "interval_used": interval}, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_accounts() -> list[dict[str, Any]]:
    accounts = _load_json(ACCOUNTS_CONFIG, [])
    return [a for a in accounts if a.get("enabled")]


def _load_hashtags() -> list[dict[str, Any]]:
    hashtags = _load_json(HASHTAGS_CONFIG, [])
    return [h for h in hashtags if h.get("enabled")]


# Кешуємо клієнта на весь час роботи процесу — instagrapi логінитись НЕ треба
# щоцикл, якщо сесія й так жива. Логін (а тим паче парольний релогін) щоразу
# з одного пристрою — саме той патерн, який Instagram підозрює в автоматизації
# ("Підозра на автоматизовані дії", scraping_warning). Клієнт скидається в None
# лише коли реально ловимо LoginRequired під час роботи (див. parse()).
_client: Client | None = None

# Випадкова пауза (сек) перед кожним приватним запитом instagrapi — робить
# трафік менш схожим на бота, ніж рівномірний запит-у-запит без затримок.
DELAY_RANGE = [2, 6]

# Додаткова пауза (сек) між акаунтами (не між окремими запитами instagrapi
# всередині одного акаунта — це вже DELAY_RANGE) — щоб не бити по API суцільним
# потоком запитів навіть з урахуванням DELAY_RANGE.
ACCOUNT_DELAY_RANGE = (5, 12)

# Те саме між окремими хештегами.
HASHTAG_DELAY_RANGE = (5, 12)


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


def _normalize_post(
    client: Client, media: Media, username: str, city: str, found_via_hashtag: str | None = None
) -> dict[str, Any]:
    raw_snapshot: dict[str, Any] = {"media_type": media.media_type, "pk": str(media.pk), "username": username}
    if found_via_hashtag:
        raw_snapshot["found_via_hashtag"] = found_via_hashtag
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
        "city": city,
        "raw_snapshot": raw_snapshot,
    }


def _normalize_story(client: Client, story: Story, username: str, city: str) -> dict[str, Any]:
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
        "city": city,
        "raw_snapshot": {"pk": str(story.pk), "username": username},
    }


def _search_hashtag(client: Client, tag: str, city: str) -> list[dict[str, Any]]:
    """Останні пости/Reels за хештегом (client.hashtag_medias_recent охоплює
    обидва — Reels в instagrapi це звичайний Media з media_type=2, окремого
    ендпоінту для них немає)."""
    listings: list[dict[str, Any]] = []
    try:
        medias = client.hashtag_medias_recent(tag, amount=HASHTAG_MEDIA_LIMIT)
    except Exception:
        logger.exception("Не вдалося обробити хештег #%s", tag)
        return listings

    for media in medias:
        username = media.user.username if media.user else "unknown"
        listings.append(_normalize_post(client, media, username, city, found_via_hashtag=tag))
    logger.info("#%s (%s): перевірено %d постів", tag, city, len(medias))
    return listings


async def parse(accounts: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Забирає нові пости і поточні Stories кожного акаунта зі списку, а тоді —
    останні пости/Reels за хештегами (instagram/hashtags.json), якщо це не
    вимкнено окремо через INSTAGRAM_HASHTAG_SEARCH_ENABLED.

    instagrapi синхронний (не asyncio) — цей парсер має async-сигнатуру лише
    заради єдиного інтерфейсу з web_pages/lun.py та telegam/channels.py, а
    сам виклик блокуючий. Якщо це почне заважати event loop-у бота, обгорнути
    у asyncio.to_thread() — поки що обсяг акаунтів для цього замалий.
    """
    if not INSTAGRAM_ENABLED:
        logger.info("INSTAGRAM_ENABLED=false в .env — пропускаю instagram-парсер (без жодних звернень до Instagram)")
        return []

    wait_left = _seconds_until_next_run()
    if wait_left > 0:
        logger.info(
            "Instagram: ще ~%.0f хв до наступного дозволеного парсингу (інтервал ~%.1f год) — пропускаю цей цикл",
            wait_left / 60, INSTAGRAM_PARSE_INTERVAL_SECONDS / 3600,
        )
        return []

    # Позначаємо спробу ОДРАЗУ, ще до самого логіну — навіть невдалий логін це
    # вже звернення до Instagram і не повинен ретраїтись частіше за інтервал
    # (саме часті повторні спроби після невдачі і "гріють" підозру найбільше).
    _mark_run_now()

    client = _get_client()
    if client is None:
        return []

    accounts = accounts or _load_accounts()
    listings: list[dict[str, Any]] = []

    if accounts:
        state = _load_json(STATE_FILE, {})

        for idx, entry in enumerate(accounts):
            username = entry["username"]
            city = entry.get("city") or "ivano-frankivsk"
            if idx > 0:
                await asyncio.sleep(random.uniform(*ACCOUNT_DELAY_RANGE))
            try:
                user_id = client.user_id_from_username(username)
                last_pk = state.get(username)

                medias = client.user_medias(user_id, amount=POSTS_PER_ACCOUNT)
                for media in medias:
                    if last_pk and str(media.pk) == str(last_pk):
                        break  # дійшли до вже обробленого поста — далі все старе
                    listings.append(_normalize_post(client, media, username, city))
                if medias:
                    state[username] = str(medias[0].pk)  # найновіший — нова точка відліку

                stories = client.user_stories(user_id)
                for story in stories:
                    listings.append(_normalize_story(client, story, username, city))

                logger.info("%s: перевірено %d постів, %d сторіз", username, len(medias), len(stories))
            except LoginRequired:
                logger.warning(
                    "Instagram: сесія протухла посеред роботи (%s) — скидаю кеш клієнта до наступного циклу", username
                )
                _invalidate_client()
                _save_json(STATE_FILE, state)
                return listings
            except Exception:
                logger.exception("Не вдалося обробити акаунт %s", username)

        _save_json(STATE_FILE, state)

    if INSTAGRAM_HASHTAG_SEARCH_ENABLED:
        hashtags = _load_hashtags()
        for idx, entry in enumerate(hashtags):
            if idx > 0 or accounts:
                await asyncio.sleep(random.uniform(*HASHTAG_DELAY_RANGE))
            try:
                listings.extend(_search_hashtag(client, entry["tag"], entry.get("city") or "ivano-frankivsk"))
            except LoginRequired:
                logger.warning("Instagram: сесія протухла посеред хештег-пошуку — скидаю кеш клієнта до наступного циклу")
                _invalidate_client()
                break

    return listings


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = asyncio.run(parse())
    out_path = BASE_DIR / "instagram_output.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d записів збережено в %s", len(results), out_path)
