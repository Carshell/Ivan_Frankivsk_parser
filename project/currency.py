"""Курс НБУ (UAH -> USD), з кешем на день — щоб не бити публічне API на кожне оголошення.

Використовується парсерами джерел, які показують ціну лише в гривні (OLX тощо),
на відміну від lun.ua/dom.ria, де сайт сам віддає перерахунок у долар.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent / "currency_cache.json"
NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?valcode=USD&json"


def _load_cache() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save_cache(data: dict) -> None:
    CACHE_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _fetch_rate() -> float | None:
    try:
        with urllib.request.urlopen(NBU_URL, timeout=10) as response:
            data = json.loads(response.read().decode("utf-8"))
        return float(data[0]["rate"])
    except Exception:
        logger.exception("Не вдалося отримати курс НБУ")
        return None


def get_usd_uah_rate() -> float | None:
    """Скільки гривень за 1 долар за курсом НБУ. None — якщо немає ні мережі, ні кешу."""
    cache = _load_cache()
    today = date.today().isoformat()
    if cache.get("date") == today and cache.get("rate"):
        return cache["rate"]

    rate = _fetch_rate()
    if rate is not None:
        _save_cache({"date": today, "rate": rate})
        return rate

    if cache.get("rate"):
        logger.warning(
            "Не вдалося оновити курс НБУ, використовую останній відомий (%s від %s)",
            cache["rate"], cache.get("date"),
        )
        return cache["rate"]

    return None


def uah_to_usd(uah: float) -> float | None:
    rate = get_usd_uah_rate()
    return round(uah / rate, 2) if rate else None


def usd_to_uah(usd: float) -> float | None:
    rate = get_usd_uah_rate()
    return round(usd * rate, 2) if rate else None
