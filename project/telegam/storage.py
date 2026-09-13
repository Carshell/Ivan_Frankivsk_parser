"""Проста файлова персистенція для бота — тимчасово, до підключення Postgres/Redis (п.11 ТЗ).

Зберігає:
  subscribers.json      -- {"<chat_id>": {"paused": bool}}
  seen.json             -- список external_id оголошень, які вже надсилались
                            (щоб не дублювати між циклами парсингу)
  matched_listings.json -- оголошення, що пройшли хард-фільтр і поріг скорингу
                            Claude — щоб показати їх новому підписнику одразу
                            на /start, без повторного парсингу чи виклику Claude
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
SEEN_FILE = DATA_DIR / "seen.json"
MATCHED_FILE = DATA_DIR / "matched_listings.json"
MAX_STORED_MATCHED = 200  # запобіжник, щоб файл не ріс нескінченно


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _save_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def add_subscriber(chat_id: int) -> None:
    subs = _load_json(SUBSCRIBERS_FILE, {})
    subs.setdefault(str(chat_id), {"paused": False})
    _save_json(SUBSCRIBERS_FILE, subs)


def set_paused(chat_id: int, paused: bool) -> None:
    subs = _load_json(SUBSCRIBERS_FILE, {})
    subs.setdefault(str(chat_id), {})["paused"] = paused
    _save_json(SUBSCRIBERS_FILE, subs)


def active_subscriber_ids() -> list[int]:
    subs = _load_json(SUBSCRIBERS_FILE, {})
    return [int(chat_id) for chat_id, info in subs.items() if not info.get("paused")]


def is_seen(external_id: str) -> bool:
    return external_id in _load_json(SEEN_FILE, [])


def mark_seen(external_id: str) -> None:
    seen = _load_json(SEEN_FILE, [])
    if external_id not in seen:
        seen.append(external_id)
        _save_json(SEEN_FILE, seen)


def mark_seen_bulk(external_ids: list[str]) -> None:
    seen = _load_json(SEEN_FILE, [])
    seen_set = set(seen)
    new_ids = [eid for eid in external_ids if eid not in seen_set]
    if new_ids:
        seen.extend(new_ids)
        _save_json(SEEN_FILE, seen)


def _is_remote_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def _delete_local_photos(listing: dict[str, Any]) -> None:
    """Видаляє локально завантажені фото (Telegram-джерело) з диску. Для
    remote URL (lun.ua тощо) нічого не робить — там нема що видаляти."""
    for photo in listing.get("photos") or []:
        if not _is_remote_url(photo):
            Path(photo).unlink(missing_ok=True)


def add_matched(listings: list[dict[str, Any]]) -> None:
    """Зберігає оголошення, що пройшли хард-фільтр+скоринг (main.score_new_listings).

    Фото (включно з локальними файлами з Telegram-джерел) зберігаються як є —
    щоб /start новому підписнику показував їх з реальними фото, а не текстом.
    Локальні файли видаляються з диску лише тоді, коли оголошення випадає зі
    сховища за лімітом MAX_STORED_MATCHED (див. нижче), а не одразу після
    першої розсилки.
    """
    if not listings:
        return
    stored = _load_json(MATCHED_FILE, [])
    existing_ids = {item["external_id"] for item in stored}

    for listing in listings:
        if listing["external_id"] in existing_ids:
            continue
        stored.append(dict(listing))
        existing_ids.add(listing["external_id"])

    if len(stored) > MAX_STORED_MATCHED:
        evicted, stored = stored[: len(stored) - MAX_STORED_MATCHED], stored[-MAX_STORED_MATCHED:]
        for listing in evicted:
            _delete_local_photos(listing)

    _save_json(MATCHED_FILE, stored)


def get_matched(limit: int = 20) -> list[dict[str, Any]]:
    """Останні (за часом додавання) оголошення, що пройшли хард-фільтр+скоринг."""
    stored = _load_json(MATCHED_FILE, [])
    return stored[-limit:] if limit else stored
