"""Парсер Telegram-каналів/груп оренди через Telethon (від імені звичайного акаунта).

На відміну від web_pages/lun.py тут немає структурованих даних (JSON-LD/API) —
лише вільний текст повідомлення, тому price/rooms/area витягуються евристично
регулярками і не завжди присутні. Пости без розпізнаної ціни або про добову
оренду відсіюються тут же. Тип нерухомості (будинок/квартира) НЕ фільтрується
на цьому рівні — обидва типи тепер підтримуються (вибір підписника, п.
hard_filters.matches_subscriber_preferences); класифікація тексту робиться
пізніше, в main.py, через hard_filters.detect_property_type().

Перед першим запуском потрібен один інтерактивний вхід — див. login_telegram.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import Message, MessageMediaPhoto

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR.parent / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"]) if os.environ.get("TELEGRAM_API_ID") else None
API_HASH = os.environ.get("TELEGRAM_API_HASH")
SESSION_PATH = BASE_DIR / "channels_session"

CHANNELS_CONFIG = BASE_DIR / "channels.json"
STATE_FILE = BASE_DIR / "channels_state.json"  # {"<username>": останній оброблений message_id}
MEDIA_CACHE_DIR = BASE_DIR / "media_cache"

SOURCE_NAME = "telegram"

PRICE_USD_RE = re.compile(r"(\d[\d\s]{2,6})\s*\$|\$\s*(\d[\d\s]{2,6})")
PRICE_UAH_RE = re.compile(r"(\d[\d\s]{3,7})\s*(?:грн|уах|uah)", re.IGNORECASE)
ROOMS_RE = re.compile(r"(\d+)[\s-]*(?:кімн|к\.|км)", re.IGNORECASE)
AREA_RE = re.compile(r"(\d{2,4}(?:[.,]\d+)?)\s*(?:м²|кв\.?\s*м|m2|м2)", re.IGNORECASE)

EXCLUDE_KEYWORDS = ("подобово", "почасово", "погодинно")  # добова оренда — не наш профіль


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_channels() -> list[dict[str, Any]]:
    channels = _load_json(CHANNELS_CONFIG, [])
    return [c for c in channels if c.get("enabled")]


def _parse_price(text: str) -> tuple[float | None, float | None]:
    price_usd = price_uah = None
    m = PRICE_USD_RE.search(text)
    if m:
        raw = (m.group(1) or m.group(2)).replace(" ", "")
        try:
            price_usd = float(raw)
        except ValueError:
            pass
    m = PRICE_UAH_RE.search(text)
    if m:
        raw = m.group(1).replace(" ", "")
        try:
            price_uah = float(raw)
        except ValueError:
            pass
    return price_usd, price_uah


def _is_daily_rental(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in EXCLUDE_KEYWORDS)


async def _fetch_channel_messages(client: TelegramClient, username: str, last_id: int) -> list[Message]:
    messages: list[Message] = []
    if last_id:
        async for m in client.iter_messages(username, min_id=last_id, limit=200, reverse=True):
            messages.append(m)
    else:
        # Перший запуск: без історії, беремо лише останні пости як стартову точку.
        async for m in client.iter_messages(username, limit=50):
            messages.append(m)
    return messages


def _group_by_album(messages: list[Message]) -> list[list[Message]]:
    """Групує повідомлення одного альбому (grouped_id) в один пост-оголошення."""
    groups: dict[Any, list[Message]] = {}
    order: list[Any] = []
    for m in messages:
        key = m.grouped_id or m.id
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)
    return [groups[k] for k in order]


async def _normalize_group(
    client: TelegramClient, group: list[Message], channel_username: str, city: str
) -> dict[str, Any] | None:
    text = ""
    for m in group:
        if m.message:
            text = m.message
            break
    if not text or _is_daily_rental(text):
        return None

    price_usd, price_uah = _parse_price(text)
    if price_usd is None and price_uah is None:
        return None  # без ціни неможливо застосувати хард-фільтр по бюджету

    rooms_match = ROOMS_RE.search(text)
    area_match = AREA_RE.search(text)

    photos: list[str] = []
    MEDIA_CACHE_DIR.mkdir(exist_ok=True)
    for m in group:
        if isinstance(m.media, MessageMediaPhoto):
            path = MEDIA_CACHE_DIR / f"{channel_username}_{m.id}.jpg"
            try:
                await client.download_media(m, file=str(path))
                photos.append(str(path))
            except Exception:
                logger.exception("Не вдалося завантажити фото %s з %s", m.id, channel_username)

    primary = group[0]
    return {
        "source": SOURCE_NAME,
        "url": f"https://t.me/{channel_username}/{primary.id}",
        "external_id": f"{channel_username}:{primary.grouped_id or primary.id}",
        "origin_site": channel_username,
        "published_at": primary.date.isoformat() if primary.date else None,
        "found_at": None,
        "title": text.splitlines()[0][:120],
        "description": text,
        "price_usd": price_usd,
        "price_uah": price_uah,
        "area": float(area_match.group(1).replace(",", ".")) if area_match else None,
        "area_living": None,
        "area_kitchen": None,
        "rooms": int(rooms_match.group(1)) if rooms_match else None,
        "floor": None,
        "floor_total": None,
        "address": None,
        "lat": None,
        "lon": None,
        "photos": photos,
        "contact": None,
        "features": [],
        "city": city,
        "raw_snapshot": {"text": text, "channel": channel_username, "message_ids": [m.id for m in group]},
    }


async def parse(channels: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Забирає нові повідомлення (від останнього обробленого message_id) з кожного каналу/групи.

    Кожен канал у channels.json має власне "city" (канали для Києва/Чернівців
    додались пізніше, поруч із початковими івано-франківськими) — тегується
    прямо тут, на відміну від property_type, який усе ще визначається
    евристикою пізніше в main.py (текст каналів надто вільний, щоб надійно
    прив'язати тип нерухомості до конкретного каналу заздалегідь)."""
    if not API_ID or not API_HASH:
        logger.warning("TELEGRAM_API_ID / TELEGRAM_API_HASH не задані в .env — пропускаю telegram-парсер")
        return []
    if not SESSION_PATH.with_suffix(".session").exists():
        logger.warning(
            "Немає файлу сесії %s — спершу запусти 'python telegam/login_telegram.py' один раз вручну",
            SESSION_PATH.with_suffix(".session"),
        )
        return []

    channels = channels or _load_channels()
    if not channels:
        return []

    state = _load_json(STATE_FILE, {})
    listings: list[dict[str, Any]] = []

    client = TelegramClient(str(SESSION_PATH), API_ID, API_HASH)
    await client.start()
    try:
        for entry in channels:
            username = entry["username"]
            city = entry.get("city") or "ivano-frankivsk"
            try:
                last_id = state.get(username, 0)
                messages = await _fetch_channel_messages(client, username, last_id)
                if messages:
                    state[username] = max(m.id for m in messages)
                for group in _group_by_album(messages):
                    listing = await _normalize_group(client, group, username, city)
                    if listing:
                        listings.append(listing)
                logger.info("%s: перевірено %d повідомлень", username, len(messages))
            except Exception:
                logger.exception("Не вдалося обробити канал %s", username)
    finally:
        await client.disconnect()

    _save_json(STATE_FILE, state)
    return listings


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = asyncio.run(parse())
    out_path = BASE_DIR / "channels_output.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)
