"""Дедуплікація оголошень (п.7, п.8 ТЗ): один і той самий будинок часто
публікується на кількох майданчиках одночасно (OLX + dom.ria + Instagram
тощо) або репостується — кожна копія має свій external_id, тому без цього
кроку кожна копія йде окремим повідомленням у Telegram (саме це користувач
побачив як "спам однаковими оголошеннями").

Повноцінна дедуплікація з ТЗ (perceptual hash фото) поки не реалізована —
це наступний крок. Зараз використовується текстовий відбиток: значущі слова
адреси (а не точний рядок — формулювання адреси сильно різниться між
джерелами, "с. Крихівці, Івано-Франківський район" vs "Крихівці") + ціна і
площа, які не повинні суперечити одна одній. Дублікатом вважаємо, коли
суттєва частка слів адреси збігається (а не обов'язково всі) — це свідомо
евристика, а не точна перевірка: без адреси взагалі оголошення лишається
унікальним (краще показати повторно, ніж помилково змерджити два різні
будинки).
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).parent / "telegam"
SIGNATURES_FILE = DATA_DIR / "dedup_signatures.json"
MAX_STORED_SIGNATURES = 1000
ADDRESS_OVERLAP_THRESHOLD = 0.6  # частка спільних слів адреси відносно коротшого набору

_NOISE_WORDS = {
    "оренда", "здам", "здається", "здаю", "продам", "будинок", "будинку", "будинка",
    "котедж", "котеджу", "котеджне", "котеджі", "містечко", "смт", "довгостроково",
    "довгострокова", "терміново", "власник", "власника", "без", "комісії", "комісія",
    "посередників", "посередник", "оренду", "оренди", "здача", "вул", "вулиця", "район",
    "область", "місто", "село", "с", "м", "р-н",
}


def _tokenize(value: str | None) -> set[str]:
    if not value:
        return set()
    text = re.sub(r"[^\w\s]", " ", value.lower(), flags=re.UNICODE)
    return {t for t in text.split() if t not in _NOISE_WORDS and len(t) > 2}


def _bucket(value: float | None, step: float) -> str | None:
    if value is None:
        return None
    return str(round(value / step) * step)


def _fingerprint(listing: dict[str, Any]) -> dict[str, Any] | None:
    tokens = _tokenize(listing.get("address"))
    if not tokens:
        tokens = _tokenize(listing.get("title"))  # адреси нема — краще неточний натяк, ніж нічого
    if not tokens:
        return None
    return {
        "tokens": tokens,
        "price": _bucket(listing.get("price_usd"), 50),
        "area": _bucket(listing.get("area"), 10),
    }


def _matches(fp: dict[str, Any], other: dict[str, Any]) -> bool:
    # Ціна й площа, якщо відомі обидві, не повинні суперечити одна одній.
    if fp["price"] is not None and other["price"] is not None and fp["price"] != other["price"]:
        return False
    if fp["area"] is not None and other["area"] is not None and fp["area"] != other["area"]:
        return False

    smaller = min(len(fp["tokens"]), len(other["tokens"]))
    if smaller == 0:
        return False
    overlap = len(fp["tokens"] & other["tokens"]) / smaller
    return overlap >= ADDRESS_OVERLAP_THRESHOLD


def _load_stored() -> list[dict[str, Any]]:
    if not SIGNATURES_FILE.exists():
        return []
    try:
        return json.loads(SIGNATURES_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _save_stored(data: list[dict[str, Any]]) -> None:
    if len(data) > MAX_STORED_SIGNATURES:
        data = data[-MAX_STORED_SIGNATURES:]
    SIGNATURES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def filter_duplicates(listings: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Повертає (унікальні оголошення, скільки відсіяно як дублікат).

    Перше оголошення з кожним відбитком лишається (і запам'ятовується — щоб
    той самий будинок, показаний іншим джерелом чи репостом наступного
    циклу, теж впізнався); решта зі схожим відбитком — відсіюються (не йдуть
    у Telegram, не витрачають виклик Claude)."""
    if not listings:
        return listings, 0

    stored_raw = _load_stored()
    known = [{"tokens": set(item["tokens"]), "price": item["price"], "area": item["area"]} for item in stored_raw]

    unique: list[dict[str, Any]] = []
    duplicates = 0
    active = list(known)  # відомі + щойно додані в цьому ж прогоні
    new_entries: list[dict[str, Any]] = []

    for listing in listings:
        fp = _fingerprint(listing)
        if fp is None:
            unique.append(listing)
            continue
        if any(_matches(fp, existing) for existing in active):
            duplicates += 1
            continue
        active.append(fp)
        unique.append(listing)
        new_entries.append(
            {
                "tokens": sorted(fp["tokens"]),
                "price": fp["price"],
                "area": fp["area"],
                "url": listing.get("url"),
                "stored_at": time.time(),
            }
        )

    if new_entries:
        _save_stored(stored_raw + new_entries)

    return unique, duplicates
