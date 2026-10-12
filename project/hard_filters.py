"""Відсів продажу і класифікація типу житла за текстом.

Бот працює лише на оренду (ТЗ, п.1: "продаж і «здати/продати» не передбачені"),
тому оголошення про продаж відсіюється завжди, ще до виклику Claude.

Тип нерухомості для джерел з web_pages/sites.json береться з конфігу (кожен
під-пошук — окремий тип). Для Telegram-каналів і Instagram (вільний текст)
тип визначається евристикою detect_property_type(); коли вона не впоралась
(None), тип визначає Claude при витягу полів.

Загальний принцип проєкту: коли даних бракує — не відсіюємо. Відсів — лише
за явним негативним сигналом.
"""

from __future__ import annotations

from typing import Any

HOUSE_KEYWORDS = (
    "будинок", "будинку", "будинка", "будиночок", "будинки",
    "котедж", "котеджне містечко", "таунхаус", "напівособняк", "півбудинку",
    "частина будинку",
)
APARTMENT_MARKERS = ("квартир",)

SALE_KEYWORDS = (
    "продаж", "продам", "продасться", "продається", "продати",
    "купівля-продаж", "терміновий продаж", "терміново продам", "на продаж",
)
RENT_KEYWORDS = ("оренда", "оренду", "оренди", "здам", "здається", "здаю", "rent")


def _text_blob(listing: dict[str, Any]) -> str:
    parts = [listing.get("title") or "", listing.get("address") or "", listing.get("description") or ""]
    return " ".join(parts).lower()


def detect_property_type(listing: dict[str, Any]) -> str | None:
    """"house" | "apartment" | None (неоднозначно) — для джерел без явного
    тегування типу (Telegram-канали, Instagram)."""
    blob = _text_blob(listing)
    title = (listing.get("title") or "").lower()
    looks_like_house = any(k in blob for k in HOUSE_KEYWORDS)
    looks_like_apartment = any(m in title for m in APARTMENT_MARKERS) and not looks_like_house
    if looks_like_apartment:
        return "apartment"
    if looks_like_house:
        return "house"
    return None


def is_sale_listing(listing: dict[str, Any]) -> bool:
    """True, якщо оголошення про ПРОДАЖ, а не оренду.

    Заголовок/перший рядок — пріоритетний сигнал: якщо там прямо написано
    "оренда"/"здам", довіряємо цьому, навіть якщо десь у тілі тексту
    згадується "продаж" (наприклад, "є можливість подальшого викупу").
    Немає жодного явного сигналу (ні оренда, ні продаж) — не відсіюємо."""
    title = (listing.get("title") or "").lower()
    if any(k in title for k in RENT_KEYWORDS):
        return False
    if any(k in title for k in SALE_KEYWORDS):
        return True

    blob = _text_blob(listing)
    return any(k in blob for k in SALE_KEYWORDS) and not any(k in blob for k in RENT_KEYWORDS)
