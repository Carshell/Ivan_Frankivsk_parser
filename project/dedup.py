"""Дедуплікація оголошень: той самий об'єкт часто публікується на кількох
майданчиках одночасно (OLX + dom.ria + Instagram тощо) або репостується —
кожна копія має свій external_id, тому без цього кроку пошук показував би
одне й те саме житло кілька разів.

Повноцінна дедуплікація за perceptual hash фото поки не реалізована. Зараз —
текстовий відбиток: значущі слова адреси (а не точний рядок — формулювання
адреси сильно різниться між джерелами) + ціна і площа, які не повинні
суперечити одна одній. Дублікатом вважаємо, коли суттєва частка слів адреси
збігається (а не обов'язково всі) — це свідомо евристика: без адреси взагалі
оголошення лишається унікальним (краще показати повторно, ніж помилково
змерджити два різні об'єкти).

Відомі оголошення беруться з бази (rentdb.active_listings_for_dedup), а не з
окремого файлу сигнатур: так відбиток з'являється лише тоді, коли оголошення
справді збережене, і падіння циклу посередині не "з'їдає" оголошення назавжди.
"""

from __future__ import annotations

import re
from typing import Any

ADDRESS_OVERLAP_THRESHOLD = 0.6  # частка спільних слів адреси відносно коротшого набору

_NOISE_WORDS = {
    "оренда", "здам", "здається", "здаю", "продам", "будинок", "будинку", "будинка",
    "котедж", "котеджу", "котеджне", "котеджі", "містечко", "смт", "довгостроково",
    "довгострокова", "терміново", "власник", "власника", "без", "комісії", "комісія",
    "посередників", "посередник", "оренду", "оренди", "здача", "вул", "вулиця", "район",
    "область", "місто", "село", "с", "м", "р-н", "квартира", "квартири", "квартиру",
    "кімнатна", "кімнатної", "кімнатну",
    # Назви міст: OLX і частина джерел дають як адресу лише місто — без цього
    # два різні оголошення "Київ" з близькою ціною й площею зливались би в одне.
    "київ", "києві", "києва", "івано", "франківськ", "франківську", "франківська", "франківський",
    "чернівці", "чернівцях", "чернівців", "україна", "області", "обл",
}
MIN_COMMON_TOKENS = 2  # одне спільне слово (наприклад, назва вулиці) — замало для висновку "дублікат"


def _tokenize(value: str | None) -> set[str]:
    if not value:
        return set()
    text = re.sub(r"[^\w\s]", " ", value.lower(), flags=re.UNICODE)
    # Номери будинків ("14") значущі, тож цифрові токени лишаємо навіть коротші за 3 символи.
    return {t for t in text.split() if t not in _NOISE_WORDS and (len(t) > 2 or t.isdigit())}


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
    common = fp["tokens"] & other["tokens"]
    return len(common) >= MIN_COMMON_TOKENS and len(common) / smaller >= ADDRESS_OVERLAP_THRESHOLD


def filter_duplicates(
    listings: list[dict[str, Any]], known: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], int]:
    """Повертає (унікальні оголошення, скільки відсіяно як дублікат).

    known — уже збережені оголошення (поля address/title/price_usd/area).
    Перше оголошення з кожним відбитком лишається; решта зі схожим відбитком,
    як і ті, що збігаються з known, — відсіюються."""
    if not listings:
        return listings, 0

    active = [fp for fp in (_fingerprint(item) for item in (known or [])) if fp is not None]

    unique: list[dict[str, Any]] = []
    duplicates = 0
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

    return unique, duplicates
