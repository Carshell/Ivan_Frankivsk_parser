"""Хард-фільтри до Claude (п.3 ТЗ): продаж/оренда, ціна, відповідність
вибору підписника (місто + тип нерухомості).

Регіон і тип нерухомості БІЛЬШЕ НЕ універсальний блокуючий хард-фільтр —
відколи з'явився вибір міста/типу в /start (кожен підписник обирає, що
саме йому показувати), ці два виміри стали персональним фільтром доставки
(matches_subscriber_preferences), а не спільним для всіх відсівом. Джерела
з web_pages/sites.json вже позначені точним "city"/"property_type" при
парсингу (main._run_site_parser) — довіряти тексту тут більше не треба.
Telegram-канали й Instagram не мають такого тегування на рівні джерела,
тому для них тип визначається текстовою евристикою (detect_property_type).

Дедуплікація "чи вже надсилали раніше" — окрема відповідальність
(telegam/storage.py, за external_id), тут не дублюється.

Загальний принцип (як і раніше): коли даних бракує — краще пропустити
оголошення далі, ніж мовчки відсіяти щось підходяще через неповні дані
джерела. Відсіюємо лише за явним НЕГАТИВНИМ сигналом, не за відсутністю
позитивного.
"""

from __future__ import annotations

from typing import Any

HOUSE_KEYWORDS = (
    "будинок", "будинку", "будинка", "будиночок", "будинки",
    "котедж", "котеджне містечко", "таунхаус", "напівособняк", "півбудинку",
    "частина будинку",
)
APARTMENT_MARKERS = ("квартир",)

PRICE_MIN_USD = 1300
PRICE_MAX_USD = 4000

# Замовник шукає ЛИШЕ оренду — продаж не повинен потрапляти в бот НІКОЛИ,
# незалежно від того, чи увімкнені інші хард-фільтри (main.ENABLE_HARD_FILTERS).
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
    тегування типу (Telegram-канали, Instagram; сайти з web_pages/sites.json
    вже мають listing["property_type"], сюди навіть не заходять — див.
    matches_subscriber_preferences).

    None навмисно не прирівнюється ні до house, ні до apartment: такі
    оголошення показуються підписникам з БУДЬ-яким вибором типу, а не
    ховаються через невизначеність."""
    blob = _text_blob(listing)
    title = (listing.get("title") or "").lower()
    looks_like_house = any(k in blob for k in HOUSE_KEYWORDS)
    looks_like_apartment = any(m in title for m in APARTMENT_MARKERS) and not looks_like_house
    if looks_like_apartment:
        return "apartment"
    if looks_like_house:
        return "house"
    return None


def matches_price(listing: dict[str, Any]) -> bool:
    price = listing.get("price_usd")
    if price is None:
        return True  # немає ціни (наприклад "договірна") — не можемо перевірити, пропускаємо
    return PRICE_MIN_USD <= price <= PRICE_MAX_USD


def is_sale_listing(listing: dict[str, Any]) -> bool:
    """True, якщо оголошення про ПРОДАЖ, а не оренду — має відсіюватись завжди,
    незалежно від ENABLE_HARD_FILTERS (замовника продаж не цікавить в принципі).

    Заголовок/перший рядок — пріоритетний сигнал: якщо там прямо написано
    "оренда"/"здам", довіряємо цьому, навіть якщо десь у тілі тексту
    згадується "продаж" (наприклад, "є можливість подальшого викупу").
    Немає жодного явного сигналу (ні оренда, ні продаж) — не відсіюємо,
    як і в решті хард-фільтрів цього модуля."""
    title = (listing.get("title") or "").lower()
    if any(k in title for k in RENT_KEYWORDS):
        return False
    if any(k in title for k in SALE_KEYWORDS):
        return True

    blob = _text_blob(listing)
    return any(k in blob for k in SALE_KEYWORDS) and not any(k in blob for k in RENT_KEYWORDS)


def matches_subscriber_preferences(
    listing: dict[str, Any], cities: list[str] | None, property_types: list[str] | None
) -> bool:
    """True, якщо оголошення підходить під вибір конкретного підписника
    (майстер налаштувань після /start — міста і тип нерухомості).

    cities/property_types порожні або None — вимір не звужений, усе підходить
    (підписник ще не проходив майстер, або явно нічого не обмежував).
    Оголошення без визначеного типу (detect_property_type() -> None,
    трапляється для Telegram/Instagram без чіткого сигналу) підходить під
    БУДЬ-який вибір типу — невизначеність не повинна ховати оголошення."""
    if cities:
        listing_city = listing.get("city")
        if listing_city and listing_city not in cities:
            return False

    if property_types:
        listing_type = listing.get("property_type") or detect_property_type(listing)
        if listing_type and listing_type not in property_types:
            return False

    return True


def passes_hard_filters(listing: dict[str, Any]) -> bool:
    return matches_price(listing)
