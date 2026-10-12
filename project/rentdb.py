"""SQLite-сховище бота (ТЗ: пошук за фільтрами, обране, збережені фільтри,
сповіщення, мітка "Вартість знижено!"). Замінює файлові JSON-сховища
(telegam/storage.py), які не годились для пошуку по тисячах оголошень.

Принцип пошуку (як і раніше в проєкті): якщо в оголошенні немає даних про
якусь ознаку (район, тварини, меблі…), воно проходить фільтр — відсіюємо
лише коли оголошення ЯВНО суперечить вибору. Краще показати зайве, ніж
сховати підходяще через неповні дані джерела.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import districts as dist
from i18n import AREA_RANGES, BUDGET_RANGES

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("RENT_DB_PATH", BASE_DIR / "data" / "rent.db"))
LEGACY_SUBSCRIBERS = BASE_DIR / "telegam" / "subscribers.json"

# Скільки днів оголошення вважається актуальним після останньої появи в парсингу.
ACTIVE_LISTING_DAYS = int(os.environ.get("ACTIVE_LISTING_DAYS", "10"))
# Через скільки днів без появи оголошення видаляються з бази остаточно.
PRUNE_AFTER_DAYS = int(os.environ.get("PRUNE_AFTER_DAYS", "60"))
NEW_LABEL_HOURS = 48
PRICE_CHANGE_THRESHOLD = 0.01  # зміна ціни менш ніж на 1% — шум округлення курсу

SORTS = ("date", "price_asc", "price_desc", "area")

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,
    source TEXT, url TEXT, origin_site TEXT,
    city TEXT NOT NULL, property_type TEXT NOT NULL,
    title TEXT, description TEXT, address TEXT,
    district TEXT, bank TEXT,
    rooms INTEGER, area REAL, floor INTEGER, floor_total INTEGER,
    price_uah REAL, price_usd REAL, price_currency TEXT,
    utilities_included INTEGER, pets INTEGER, kids INTEGER, furnished INTEGER, from_owner INTEGER,
    commission TEXT, deposit TEXT, heating TEXT, autonomy TEXT,
    elevator INTEGER, balcony INTEGER, parking INTEGER, ac INTEGER, internet INTEGER,
    metro TEXT,
    photos TEXT, contact TEXT, lat REAL, lon REAL, published_at TEXT,
    first_seen_at REAL NOT NULL, last_seen_at REAL NOT NULL,
    prev_price_uah REAL, price_dropped_at REAL,
    extracted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_search ON listings (city, property_type, last_seen_at);
CREATE INDEX IF NOT EXISTS idx_listings_first_seen ON listings (first_seen_at);

CREATE TABLE IF NOT EXISTS users (
    chat_id INTEGER PRIMARY KEY,
    lang TEXT,
    step TEXT,
    draft TEXT,
    sort TEXT NOT NULL DEFAULT 'date',
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    chat_id INTEGER NOT NULL,
    listing_id INTEGER NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (chat_id, listing_id)
);

CREATE TABLE IF NOT EXISTS saved_filters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    filters TEXT NOT NULL,
    notify INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_saved_filters_chat ON saved_filters (chat_id);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS rejected (
    external_id TEXT PRIMARY KEY,
    reason TEXT,
    rejected_at REAL NOT NULL
);
"""

_JSON_COLUMNS = ("photos", "autonomy")


def connect() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            conn.commit()
            _conn = conn
        return _conn


def close() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    for col in _JSON_COLUMNS:
        if col in data:
            try:
                data[col] = json.loads(data[col]) if data[col] else []
            except (TypeError, json.JSONDecodeError):
                data[col] = []
    return data


# --------------------------------------------------------------------- kv ----
def kv_get(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(key: str, value: str) -> None:
    with _lock:
        conn = connect()
        conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        conn.commit()


def is_bootstrapped() -> bool:
    return kv_get("bootstrap_done_at") is not None


def mark_bootstrapped() -> None:
    kv_set("bootstrap_done_at", str(time.time()))


def _bootstrap_done_at() -> float | None:
    raw = kv_get("bootstrap_done_at")
    return float(raw) if raw else None


# ------------------------------------------------------------------ users ----
def ensure_user(chat_id: int) -> dict[str, Any]:
    now = time.time()
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, created_at, last_active_at) VALUES (?, ?, ?)",
            (chat_id, now, now),
        )
        conn.execute("UPDATE users SET last_active_at = ? WHERE chat_id = ?", (now, chat_id))
        conn.commit()
    return get_state(chat_id)


def get_state(chat_id: int) -> dict[str, Any]:
    row = connect().execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    if row is None:
        return {"chat_id": chat_id, "lang": None, "step": None, "draft": {}, "sort": "date"}
    try:
        draft = json.loads(row["draft"]) if row["draft"] else {}
    except json.JSONDecodeError:
        draft = {}
    return {"chat_id": chat_id, "lang": row["lang"], "step": row["step"], "draft": draft, "sort": row["sort"] or "date"}


def save_state(chat_id: int, **fields: Any) -> None:
    """Оновлює лише передані поля: lang, step, draft (dict), sort."""
    allowed = {"lang", "step", "draft", "sort"}
    sets, params = [], []
    for key, value in fields.items():
        if key not in allowed:
            raise ValueError(f"unknown user field: {key}")
        sets.append(f"{key} = ?")
        params.append(json.dumps(value, ensure_ascii=False) if key == "draft" else value)
    if not sets:
        return
    ensure_user(chat_id)
    with _lock:
        conn = connect()
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE chat_id = ?", (*params, chat_id))
        conn.commit()


def all_user_ids() -> list[int]:
    return [r["chat_id"] for r in connect().execute("SELECT chat_id FROM users")]


# --------------------------------------------------------------- listings ----
def existing_external_ids(external_ids: list[str]) -> set[str]:
    """external_id, які вже в базі АБО раніше відхилені (продаж тощо) — щоб
    не обробляти їх (і не платити за виклик Claude) щоцикл заново."""
    found: set[str] = set()
    conn = connect()
    for table in ("listings", "rejected"):
        for i in range(0, len(external_ids), 500):
            chunk = external_ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = conn.execute(f"SELECT external_id FROM {table} WHERE external_id IN ({marks})", chunk).fetchall()
            found.update(r["external_id"] for r in rows)
    return found


def reject(external_id: str, reason: str) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT OR REPLACE INTO rejected (external_id, reason, rejected_at) VALUES (?, ?, ?)",
            (external_id, reason, time.time()),
        )
        conn.commit()


def reject_many(external_ids: list[str], reason: str) -> None:
    now = time.time()
    with _lock:
        conn = connect()
        conn.executemany(
            "INSERT OR REPLACE INTO rejected (external_id, reason, rejected_at) VALUES (?, ?, ?)",
            [(e, reason, now) for e in external_ids],
        )
        conn.commit()


def touch_existing(listings: list[dict[str, Any]]) -> dict[str, int]:
    """Оновлює last_seen_at для вже відомих оголошень і відстежує зміну ціни:
    нижча ціна -> мітка "Вартість знижено!" (prev_price_uah + price_dropped_at),
    вища -> мітка знімається. Повертає лічильники {"touched", "dropped"}."""
    now = time.time()
    touched = dropped = 0
    with _lock:
        conn = connect()
        for listing in listings:
            row = conn.execute(
                "SELECT id, price_uah FROM listings WHERE external_id = ?", (listing["external_id"],)
            ).fetchone()
            if row is None:
                continue
            touched += 1
            new_price = listing.get("price_uah")
            old_price = row["price_uah"]
            if new_price and old_price and abs(new_price - old_price) / old_price > PRICE_CHANGE_THRESHOLD:
                if new_price < old_price:
                    dropped += 1
                    conn.execute(
                        "UPDATE listings SET last_seen_at=?, price_uah=?, price_usd=?, prev_price_uah=?, price_dropped_at=? WHERE id=?",
                        (now, new_price, listing.get("price_usd"), old_price, now, row["id"]),
                    )
                else:
                    conn.execute(
                        "UPDATE listings SET last_seen_at=?, price_uah=?, price_usd=?, prev_price_uah=NULL, price_dropped_at=NULL WHERE id=?",
                        (now, new_price, listing.get("price_usd"), row["id"]),
                    )
            else:
                conn.execute("UPDATE listings SET last_seen_at=? WHERE id=?", (now, row["id"]))
        conn.commit()
    return {"touched": touched, "dropped": dropped}


def _tri(value: Any) -> int | None:
    """True/False/None -> 1/0/NULL."""
    if value is None:
        return None
    return 1 if value else 0


def _valid_district(city: str, slug: Any) -> str | None:
    if isinstance(slug, str):
        d = dist.get_district(slug)
        if d and slug.startswith(f"{city}."):
            return slug
    return None


def _extraction_columns(city: str, ex: dict[str, Any], text_for_guess: str) -> dict[str, Any]:
    """Колонки, що походять із витягу Claude (район, тварини, меблі, комісія…)."""
    district = _valid_district(city, ex.get("district")) or dist.guess_district(city, text_for_guess)
    autonomy = [a for a in (ex.get("autonomy") or []) if isinstance(a, str)]
    return {
        "district": district,
        "bank": dist.bank_of(district) if city == "kyiv" else None,
        "utilities_included": _tri(ex.get("utilities_included")),
        "pets": _tri(ex.get("pets")),
        "kids": _tri(ex.get("kids")),
        "furnished": _tri(ex.get("furnished")),
        "from_owner": _tri(ex.get("from_owner")),
        "commission": ex.get("commission"),
        "deposit": ex.get("deposit"),
        "heating": ex.get("heating") if ex.get("heating") in ("central", "individual") else None,
        "autonomy": json.dumps(autonomy, ensure_ascii=False) if autonomy else None,
        "elevator": _tri(ex.get("elevator")),
        "balcony": _tri(ex.get("balcony")),
        "parking": _tri(ex.get("parking")),
        "ac": _tri(ex.get("ac")),
        "internet": _tri(ex.get("internet")),
        "metro": ex.get("metro"),
    }


def _guess_text(listing: dict[str, Any]) -> str:
    return " ".join(filter(None, [listing.get("address"), listing.get("title"), listing.get("description")]))


def insert_listing(listing: dict[str, Any], extracted: dict[str, Any] | None = None) -> int | None:
    """Додає нове оголошення. Структуровані значення від парсера мають пріоритет;
    те, чого парсер не дав, береться з витягу Claude (extracted). None — якщо
    такий external_id уже є або немає міста/типу."""
    ex = extracted or {}
    city = listing.get("city")
    ptype = listing.get("property_type") or ex.get("property_type")
    if not city or ptype not in ("apartment", "house"):
        return None

    def pick(key: str) -> Any:
        value = listing.get(key)
        return value if value not in (None, "") else ex.get(key)

    now = time.time()
    row = {
        **_extraction_columns(city, ex, _guess_text(listing)),
        "external_id": listing["external_id"],
        "source": listing.get("source"),
        "url": listing.get("url"),
        "origin_site": listing.get("origin_site"),
        "city": city,
        "property_type": ptype,
        "title": listing.get("title"),
        "description": listing.get("description"),
        "address": listing.get("address"),
        "rooms": pick("rooms"),
        "area": pick("area"),
        "floor": pick("floor"),
        "floor_total": pick("floor_total"),
        "price_uah": listing.get("price_uah"),
        "price_usd": listing.get("price_usd"),
        "price_currency": listing.get("price_currency") or ex.get("price_currency") or "UAH",
        "photos": json.dumps(listing.get("photos") or [], ensure_ascii=False),
        "contact": listing.get("contact"),
        "lat": listing.get("lat"),
        "lon": listing.get("lon"),
        "published_at": listing.get("published_at"),
        "first_seen_at": now,
        "last_seen_at": now,
        "extracted": 1 if extracted is not None else 0,
    }
    cols = ", ".join(row)
    marks = ", ".join(f":{k}" for k in row)
    with _lock:
        conn = connect()
        cur = conn.execute(f"INSERT OR IGNORE INTO listings ({cols}) VALUES ({marks})", row)
        conn.commit()
        return cur.lastrowid if cur.rowcount else None


def listings_needing_extraction(limit: int) -> list[dict[str, Any]]:
    """Оголошення, збережені без витягу (Claude був недоступний) — для повторної спроби."""
    rows = connect().execute(
        "SELECT * FROM listings WHERE extracted = 0 ORDER BY id LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def apply_extraction(listing_id: int, ex: dict[str, Any]) -> None:
    """Дозаповнює збережене оголошення витягом Claude (rooms/area/floor — лише
    якщо ще порожні)."""
    row = get_listing(listing_id)
    if row is None:
        return
    cols = _extraction_columns(row["city"], ex, _guess_text(row))
    cols["extracted"] = 1
    for key in ("rooms", "area", "floor", "floor_total"):
        if row.get(key) is None and ex.get(key) is not None:
            cols[key] = ex[key]
    if ex.get("price_currency") and row.get("price_currency") in (None, "UAH"):
        cols["price_currency"] = ex["price_currency"]
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    with _lock:
        conn = connect()
        conn.execute(f"UPDATE listings SET {sets} WHERE id = :_id", {**cols, "_id": listing_id})
        conn.commit()


def get_listing(listing_id: int) -> dict[str, Any] | None:
    return _row_to_dict(connect().execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone())


def get_listings(listing_ids: list[int]) -> list[dict[str, Any]]:
    if not listing_ids:
        return []
    marks = ",".join("?" * len(listing_ids))
    rows = connect().execute(f"SELECT * FROM listings WHERE id IN ({marks})", listing_ids).fetchall()
    by_id = {r["id"]: _row_to_dict(r) for r in rows}
    return [by_id[i] for i in listing_ids if i in by_id]


def active_listings_for_dedup() -> list[dict[str, Any]]:
    """Мінімальний набір полів для міжджерельної дедуплікації (dedup.py)."""
    cutoff = time.time() - ACTIVE_LISTING_DAYS * 86400
    rows = connect().execute(
        "SELECT address, title, price_usd, area FROM listings WHERE last_seen_at >= ?", (cutoff,)
    ).fetchall()
    return [dict(r) for r in rows]


def prune_old() -> list[str]:
    """Видаляє оголошення, які не з'являлись PRUNE_AFTER_DAYS днів. Повертає
    локальні файли фото (Telegram/Instagram), щоб викликач прибрав їх з диска."""
    cutoff = time.time() - PRUNE_AFTER_DAYS * 86400
    local_files: list[str] = []
    with _lock:
        conn = connect()
        rows = conn.execute("SELECT id, photos FROM listings WHERE last_seen_at < ?", (cutoff,)).fetchall()
        for row in rows:
            try:
                photos = json.loads(row["photos"] or "[]")
            except json.JSONDecodeError:
                photos = []
            local_files.extend(p for p in photos if not str(p).startswith(("http://", "https://")))
        ids = [r["id"] for r in rows]
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            conn.execute(f"DELETE FROM favorites WHERE listing_id IN ({marks})", chunk)
            conn.execute(f"DELETE FROM listings WHERE id IN ({marks})", chunk)
        conn.commit()
    return local_files


def stats() -> dict[str, Any]:
    conn = connect()
    total = conn.execute("SELECT COUNT(*) AS n FROM listings").fetchone()["n"]
    by_city = {
        f"{r['city']}/{r['property_type']}": r["n"]
        for r in conn.execute("SELECT city, property_type, COUNT(*) AS n FROM listings GROUP BY city, property_type")
    }
    return {"total": total, "by_city": by_city}


# ----------------------------------------------------------------- search ----
def _build_where(filters: dict[str, Any], now: float) -> tuple[list[str], list[Any]]:
    conds = ["last_seen_at >= ?"]
    params: list[Any] = [now - ACTIVE_LISTING_DAYS * 86400]

    ptype = filters.get("type")
    if ptype:
        conds.append("property_type = ?")
        params.append(ptype)
    city = filters.get("city")
    if city:
        conds.append("city = ?")
        params.append(city)

    rooms = filters.get("rooms") or []
    if rooms:
        parts = ["rooms IS NULL"]
        exact = [int(r) for r in rooms if int(r) < 4]
        if exact:
            parts.append(f"rooms IN ({','.join('?' * len(exact))})")
            params.extend(exact)
        if any(int(r) >= 4 for r in rooms):
            parts.append("rooms >= 4")
        conds.append("(" + " OR ".join(parts) + ")")

    selected = filters.get("districts") or []
    if selected and city:
        all_slugs = {d.slug for d in dist.districts_for(city)}
        if set(selected) < all_slugs:  # "обрати все" рівнозначне відсутності фільтра
            conds.append(f"(district IS NULL OR district IN ({','.join('?' * len(selected))}))")
            params.extend(selected)

    budget = filters.get("budget")
    if budget is not None:
        lo, hi = BUDGET_RANGES[int(budget)]
        if hi is None:
            conds.append("(price_uah IS NULL OR price_uah >= ?)")
            params.append(lo)
        else:
            conds.append("(price_uah IS NULL OR (price_uah >= ? AND price_uah < ?))")
            params.extend([lo, hi])

    for key, column in (("pets", "pets"), ("kids", "kids"), ("furn", "furnished")):
        value = filters.get(key)
        if value in ("yes", "no"):
            conds.append(f"({column} IS NULL OR {column} = ?)")
            params.append(1 if value == "yes" else 0)

    owner = filters.get("from")
    if owner in ("owner", "agency"):
        conds.append("(from_owner IS NULL OR from_owner = ?)")
        params.append(1 if owner == "owner" else 0)

    if ptype != "house":  # поверх для будинків не має сенсу
        floor = filters.get("floor")
        if floor in ("not_first", "not_edge"):
            conds.append("(floor IS NULL OR floor > 1)")
        if floor in ("not_last", "not_edge"):
            conds.append("(floor IS NULL OR floor_total IS NULL OR floor < floor_total)")

    area = filters.get("area")
    if area is not None:
        lo, hi = AREA_RANGES[int(area)]
        if hi is None:
            conds.append("(area IS NULL OR area >= ?)")
            params.append(lo)
        else:
            conds.append("(area IS NULL OR (area >= ? AND area < ?))")
            params.extend([lo, hi])

    return conds, params


_ORDER = {
    "date": "first_seen_at DESC, id DESC",
    "price_asc": "price_uah IS NULL, price_uah ASC, id DESC",
    "price_desc": "price_uah IS NULL, price_uah DESC, id DESC",
    "area": "area IS NULL, area DESC, id DESC",
}


def search(
    filters: dict[str, Any],
    sort: str = "date",
    offset: int = 0,
    limit: int = 5,
    only_ids: list[int] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Повертає (сторінка оголошень, загальна кількість за фільтрами)."""
    conds, params = _build_where(filters, time.time())
    if only_ids is not None:
        if not only_ids:
            return [], 0
        conds.append(f"id IN ({','.join('?' * len(only_ids))})")
        params.extend(only_ids)
    where = " AND ".join(conds)
    conn = connect()
    total = conn.execute(f"SELECT COUNT(*) AS n FROM listings WHERE {where}", params).fetchone()["n"]
    order = _ORDER.get(sort, _ORDER["date"])
    rows = conn.execute(
        f"SELECT * FROM listings WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", (*params, limit, offset)
    ).fetchall()
    return [_row_to_dict(r) for r in rows], total


def is_new(listing: dict[str, Any]) -> bool:
    """"Новий": уперше з'явилось уже ПІСЛЯ базової лінії (інакше після першого
    запуску "новим" було б усе) і не давніше NEW_LABEL_HOURS."""
    boot = _bootstrap_done_at()
    if boot is None or listing["first_seen_at"] < boot:
        return False
    return time.time() - listing["first_seen_at"] < NEW_LABEL_HOURS * 3600


def is_price_dropped(listing: dict[str, Any]) -> bool:
    return bool(listing.get("prev_price_uah")) and bool(listing.get("price_dropped_at"))


# -------------------------------------------------------------- favorites ----
def fav_add(chat_id: int, listing_id: int) -> None:
    with _lock:
        conn = connect()
        conn.execute(
            "INSERT OR IGNORE INTO favorites (chat_id, listing_id, added_at) VALUES (?, ?, ?)",
            (chat_id, listing_id, time.time()),
        )
        conn.commit()


def fav_remove(chat_id: int, listing_id: int) -> None:
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM favorites WHERE chat_id = ? AND listing_id = ?", (chat_id, listing_id))
        conn.commit()


def fav_ids(chat_id: int) -> list[int]:
    rows = connect().execute(
        "SELECT listing_id FROM favorites WHERE chat_id = ? ORDER BY added_at DESC", (chat_id,)
    ).fetchall()
    return [r["listing_id"] for r in rows]


def fav_page(chat_id: int, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
    ids = fav_ids(chat_id)
    page_ids = ids[offset : offset + limit]
    return get_listings(page_ids), len(ids)


# ---------------------------------------------------------- saved filters ----
def sf_count(chat_id: int) -> int:
    return connect().execute("SELECT COUNT(*) AS n FROM saved_filters WHERE chat_id = ?", (chat_id,)).fetchone()["n"]


def sf_create(chat_id: int, name: str, filters: dict[str, Any], notify: bool = True) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(
            "INSERT INTO saved_filters (chat_id, name, filters, notify, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, name, json.dumps(filters, ensure_ascii=False), 1 if notify else 0, time.time()),
        )
        conn.commit()
        return int(cur.lastrowid)


def _sf_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    try:
        data["filters"] = json.loads(data["filters"])
    except json.JSONDecodeError:
        data["filters"] = {}
    data["notify"] = bool(data["notify"])
    return data


def sf_list(chat_id: int) -> list[dict[str, Any]]:
    rows = connect().execute("SELECT * FROM saved_filters WHERE chat_id = ? ORDER BY id", (chat_id,)).fetchall()
    return [_sf_row(r) for r in rows]


def sf_get(chat_id: int, filter_id: int) -> dict[str, Any] | None:
    return _sf_row(
        connect().execute("SELECT * FROM saved_filters WHERE id = ? AND chat_id = ?", (filter_id, chat_id)).fetchone()
    )


def sf_delete(chat_id: int, filter_id: int) -> None:
    with _lock:
        conn = connect()
        conn.execute("DELETE FROM saved_filters WHERE id = ? AND chat_id = ?", (filter_id, chat_id))
        conn.commit()


def sf_toggle_notify(chat_id: int, filter_id: int) -> bool | None:
    with _lock:
        conn = connect()
        row = conn.execute(
            "SELECT notify FROM saved_filters WHERE id = ? AND chat_id = ?", (filter_id, chat_id)
        ).fetchone()
        if row is None:
            return None
        new_value = 0 if row["notify"] else 1
        conn.execute("UPDATE saved_filters SET notify = ? WHERE id = ?", (new_value, filter_id))
        conn.commit()
        return bool(new_value)


def sf_all_notifying() -> list[dict[str, Any]]:
    rows = connect().execute("SELECT * FROM saved_filters WHERE notify = 1 ORDER BY id").fetchall()
    return [_sf_row(r) for r in rows]


# -------------------------------------------------------------- migration ----
def migrate_legacy_subscribers() -> int:
    """Одноразово переносить підписників зі старого telegam/subscribers.json
    (вибір міст і типів) у користувачів + збережені фільтри зі сповіщеннями —
    щоб ті, хто вже був підписаний на розсилку, не втратили її після переходу
    на новий бот. Повертає кількість перенесених користувачів."""
    if kv_get("legacy_migrated") == "1":
        return 0
    migrated = 0
    if LEGACY_SUBSCRIBERS.exists():
        try:
            subscribers = json.loads(LEGACY_SUBSCRIBERS.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            subscribers = {}
        type_names = {"apartment": "Квартира", "house": "Будинок"}
        for chat_id_raw, info in subscribers.items():
            chat_id = int(chat_id_raw)
            ensure_user(chat_id)
            save_state(chat_id, lang="uk", step="menu")
            if info.get("paused"):
                continue
            for city in info.get("cities") or []:
                for ptype in info.get("property_types") or []:
                    filters = {"type": ptype, "city": city}
                    name = f"{type_names.get(ptype, ptype)} · {dist.city_label(city, 'uk')}"
                    sf_create(chat_id, name, filters, notify=True)
            migrated += 1
    kv_set("legacy_migrated", "1")
    return migrated
