"""Проста файлова персистенція для бота — тимчасово, до підключення Postgres/Redis (п.11 ТЗ).

Зберігає:
  subscribers.json  -- {"<chat_id>": {"paused": bool}}
  seen.json         -- список external_id оголошень, які вже надсилались
                       (щоб не дублювати між циклами парсингу)
"""

from __future__ import annotations

import json
from pathlib import Path

DATA_DIR = Path(__file__).parent
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.json"
SEEN_FILE = DATA_DIR / "seen.json"


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
