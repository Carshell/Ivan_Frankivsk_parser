"""Витяг структурованих полів оголошення через Claude (ТЗ, п.3-5): район,
тварини, діти, меблі, від кого, комісія, застава, опалення, автономність,
зручності, метро, комунальні, валюта ціни.

Замінює старий скоринг (claude_scoring.py): нове ТЗ не передбачає ні оцінки
0-100, ні блоку безпеки — картка містить лише поля оголошення, а фільтри
працюють по цих структурованих полях. Промпт — у claude_extract_prompt.txt
(редагується без передеплою).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

import districts as dist

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
# Для вилучення полів із короткого тексту достатньо швидкої моделі (у рази
# дешевше й швидше за Sonnet); за потреби міняється через .env.
MODEL = os.environ.get("CLAUDE_EXTRACT_MODEL", "claude-haiku-4-5-20251001")
MAX_TOKENS = 700
PROMPT_FILE = BASE_DIR / "claude_extract_prompt.txt"

_LISTING_FIELDS = (
    "source", "title", "description", "address", "price_uah", "price_usd",
    "area", "rooms", "floor", "floor_total", "features", "city", "property_type",
)
AUTONOMY_TOKENS = ("generator", "inverter", "starlink", "shelter", "parking")
# Джерела, де всі оголошення гарантовано від агенції (для фільтра "Від кого").
AGENCY_SOURCES = {"orenda.if.ua"}

MAX_IMAGES_PER_REQUEST = 3
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_IMAGE_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic | None:
    global _client
    if not API_KEY:
        return None
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=API_KEY)
    return _client


def _load_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def _is_local_path(value: str) -> bool:
    return not value.startswith(("http://", "https://"))


def _image_block(path: str) -> dict[str, Any] | None:
    file_path = Path(path)
    media_type = _IMAGE_MEDIA_TYPES.get(file_path.suffix.lower())
    if media_type is None:
        return None
    try:
        data = file_path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        logger.warning("Фото %s завелике (%d байт) — пропускаю для Claude", path, len(data))
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.b64encode(data).decode("ascii")},
    }


def _build_content(listing: dict[str, Any]) -> list[dict[str, Any]]:
    trimmed = {k: listing.get(k) for k in _LISTING_FIELDS}
    allowed = [d.uk for d in dist.districts_for(listing.get("city") or "")]
    text = json.dumps({"listing": trimmed, "allowed_districts": allowed}, ensure_ascii=False)
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]

    # Instagram Stories не мають підпису — єдине джерело інформації це саме фото.
    if not (listing.get("description") or "").strip():
        photos = [p for p in (listing.get("photos") or []) if _is_local_path(p)]
        for photo in photos[:MAX_IMAGES_PER_REQUEST]:
            block = _image_block(photo)
            if block:
                content.append(block)
    return content


def _parse_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


# ------------------------------------------------------------ нормалізація ----
def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _true_or_none(value: Any) -> bool | None:
    return True if value is True else None


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:
        return int(value)
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value and value > 0:
        return float(value)
    return None


def _short_text(value: Any, limit: int = 60) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()[:limit]
    return None


def district_slug(city: str, name: Any) -> str | None:
    """Назва району з відповіді Claude -> slug (допускаємо одну з частин
    "Поділ/Воздвиженка", регістр і апостроф не важливі)."""
    if not isinstance(name, str) or not name.strip():
        return None
    wanted = dist.normalize_text(name).strip()
    for d in dist.districts_for(city):
        full = dist.normalize_text(d.uk)
        parts = [p.strip() for p in full.split("/")]
        if wanted == full or wanted in parts or wanted == dist.normalize_text(d.en):
            return d.slug
    return None


def normalize(listing: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    city = listing.get("city") or ""
    commission = raw.get("commission")
    deposit = raw.get("deposit")
    heating = raw.get("heating")
    autonomy = [a for a in (raw.get("autonomy") or []) if a in AUTONOMY_TOKENS] if isinstance(raw.get("autonomy"), list) else []
    currency = raw.get("price_currency") if raw.get("price_currency") in ("UAH", "USD", "EUR") else None
    ptype = raw.get("property_type") if raw.get("property_type") in ("apartment", "house") else None

    from_owner = _bool(raw.get("from_owner"))
    if from_owner is None and listing.get("source") in AGENCY_SOURCES:
        from_owner = False

    return {
        "is_rental": raw.get("is_rental") is not False,
        "property_type": ptype,
        "district": district_slug(city, raw.get("district")),
        "rooms": _int(raw.get("rooms")),
        "area": _num(raw.get("area")),
        "floor": _int(raw.get("floor")),
        "floor_total": _int(raw.get("floor_total")),
        "price_currency": currency,
        "utilities_included": _bool(raw.get("utilities_included")),
        "pets": _bool(raw.get("pets")),
        "kids": _bool(raw.get("kids")),
        "furnished": _bool(raw.get("furnished")),
        "from_owner": from_owner,
        "commission": "none" if commission == "none" else _short_text(commission, 40),
        "deposit": "none" if deposit == "none" else _short_text(deposit, 40),
        "heating": heating if heating in ("central", "individual") else None,
        "autonomy": autonomy,
        "elevator": _true_or_none(raw.get("elevator")),
        "balcony": _true_or_none(raw.get("balcony")),
        "parking": _true_or_none(raw.get("parking")),
        "ac": _true_or_none(raw.get("ac")),
        "internet": _true_or_none(raw.get("internet")),
        "metro": _short_text(raw.get("metro")) if city == "kyiv" else None,
    }


async def extract(listing: dict[str, Any]) -> dict[str, Any] | None:
    """Нормалізовані поля або None, якщо Claude недоступний/відповідь непридатна
    (тоді оголошення все одно зберігається — з extracted=0 — і дозаповнюється
    наступним циклом)."""
    client = _get_client()
    if client is None:
        logger.warning("ANTHROPIC_API_KEY не задано — пропускаю витяг полів")
        return None
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_load_prompt(),
            messages=[{"role": "user", "content": _build_content(listing)}],
        )
    except Exception:
        logger.exception("Помилка виклику Claude API для %s", listing.get("url"))
        return None

    text = "".join(block.text for block in response.content if block.type == "text")
    raw = _parse_json(text)
    if raw is None:
        logger.error("Не вдалося розпарсити відповідь Claude для %s: %s", listing.get("url"), text)
        return None
    return normalize(listing, raw)
