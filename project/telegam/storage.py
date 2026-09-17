"""Проста файлова персистенція для бота — тимчасово, до підключення Postgres/Redis (п.11 ТЗ).

Зберігає:
  subscribers.json      -- {"<chat_id>": {"paused": bool, "cities": [...]|відсутнє,
                            "property_types": [...]|відсутнє}} — cities/property_types
                            з'являються лише після завершення майстра вибору в /start
                            (telegam/bot.py, OnboardingStates); відсутність — "ще не
                            обирав", а не "нічого не підходить" (див. has_preferences)
  seen.json             -- список external_id оголошень, які вже надсилались
                            (щоб не дублювати між циклами парсингу)
  matched_listings.json -- оголошення, що пройшли хард-фільтр і поріг скорингу
                            Claude — щоб показати їх новому підписнику одразу
                            на /start, без повторного парсингу чи виклику Claude
  bootstrap_done.flag   -- ознака "перший запуск вже сформував базову лінію"
                            (див. is_bootstrapped/mark_bootstrapped)
  sent_messages.json    -- {"<delete_token>": [{"chat_id", "message_ids"}, ...]}
                            куди пішло кожне оголошення під час розсилки — щоб
                            адмін міг видалити його в усіх чатах одразу
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
SEEN_FILE = DATA_DIR / "seen.json"
MATCHED_FILE = DATA_DIR / "matched_listings.json"
MAX_STORED_MATCHED = 200  # запобіжник, щоб файл не ріс нескінченно
BOOTSTRAP_FLAG_FILE = DATA_DIR / "bootstrap_done.flag"
SENT_MESSAGES_FILE = DATA_DIR / "sent_messages.json"
MAX_STORED_DELETE_TOKENS = 300  # запобіжник, щоб файл не ріс нескінченно


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


def set_city_preferences(chat_id: int, cities: list[str]) -> None:
    subs = _load_json(SUBSCRIBERS_FILE, {})
    subs.setdefault(str(chat_id), {"paused": False})["cities"] = cities
    _save_json(SUBSCRIBERS_FILE, subs)


def set_property_type_preferences(chat_id: int, property_types: list[str]) -> None:
    subs = _load_json(SUBSCRIBERS_FILE, {})
    subs.setdefault(str(chat_id), {"paused": False})["property_types"] = property_types
    _save_json(SUBSCRIBERS_FILE, subs)


def get_preferences(chat_id: int) -> dict[str, list[str] | None]:
    subs = _load_json(SUBSCRIBERS_FILE, {})
    info = subs.get(str(chat_id), {})
    return {"cities": info.get("cities"), "property_types": info.get("property_types")}


def has_preferences(chat_id: int) -> bool:
    """True, лише якщо підписник ПОВНІСТЮ пройшов майстер вибору (обидва
    кроки — міста і тип нерухомості). Інакше /start повторно запускає майстер."""
    prefs = get_preferences(chat_id)
    return bool(prefs["cities"]) and bool(prefs["property_types"])


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

    now = time.time()
    for listing in listings:
        if listing["external_id"] in existing_ids:
            continue
        copy = dict(listing)
        copy["_stored_at"] = now
        stored.append(copy)
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


def get_recent_matched(hours: float = 24) -> list[dict[str, Any]]:
    """Оголошення, додані в сховище за останні `hours` годин — для /digest
    ("за день"), а не тільки те, що знайшлось у цьому конкретному прогоні."""
    stored = _load_json(MATCHED_FILE, [])
    cutoff = time.time() - hours * 3600
    return [item for item in stored if item.get("_stored_at", 0) >= cutoff]


def is_bootstrapped() -> bool:
    """True, якщо перший запуск уже сформував базову лінію (все, що існувало
    на момент старту, позначене "вже бачене" — без розсилки підписникам)."""
    return BOOTSTRAP_FLAG_FILE.exists()


def mark_bootstrapped() -> None:
    BOOTSTRAP_FLAG_FILE.write_text("done", encoding="utf-8")


def record_sent_listing(delete_token: str, chat_id: int, message_ids: list[int]) -> None:
    """Запам'ятовує, куди (chat_id) і які message_id пішли для одного
    оголошення під час розсилки — щоб адмін міг пізніше видалити їх усі одним
    натисканням кнопки (див. telegam/bot.py, callback "del:<token>").

    Один delete_token = одне оголошення одного циклу розсилки; кожен виклик
    додає ще один чат до вже наявного списку для цього ж токена."""
    if not message_ids:
        return
    data = _load_json(SENT_MESSAGES_FILE, {})
    data.setdefault(delete_token, []).append({"chat_id": chat_id, "message_ids": message_ids})

    if len(data) > MAX_STORED_DELETE_TOKENS:
        for key in list(data.keys())[: len(data) - MAX_STORED_DELETE_TOKENS]:
            del data[key]

    _save_json(SENT_MESSAGES_FILE, data)


def pop_sent_records(delete_token: str) -> list[dict[str, Any]]:
    """Забирає (і видаляє з диска) записи про надіслані повідомлення для
    токена — використовується один раз, при натисканні кнопки видалення."""
    data = _load_json(SENT_MESSAGES_FILE, {})
    records = data.pop(delete_token, [])
    if records:
        _save_json(SENT_MESSAGES_FILE, data)
    return records
