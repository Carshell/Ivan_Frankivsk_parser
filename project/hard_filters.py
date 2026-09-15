"""Хард-фільтри до Claude (п.3 ТЗ): регіон, тип нерухомості, ціна.

Дедуплікація "чи вже надсилали раніше" — окрема відповідальність
(telegam/storage.py, за external_id), тут не дублюється.

Тип нерухомості перевіряється по-різному залежно від джерела:
- lun.ua, dom.ria, olx, m2bomber, flatfy.ua самі фільтрують по типу на рівні
  URL/query (houses/realty_type=0/doma/house-rent/section_id=4) — їм довіряємо
  без текстового аналізу (дехто, як dom.ria, взагалі не пише "будинок" явно в
  адресі — просто "вулиця Х", і текстовий пошук ключових слів там завжди
  провалювався б).
- Telegram-канали, Instagram і rieltor_ua (зараз налаштований на flats-rent)
  такого попереднього фільтра не мають — для них тип визначається текстовою
  евристикою (шукаємо ключові слова в заголовку/адресі/описі), так само, як у
  telegam/channels.py.

Загальний принцип: коли даних бракує (немає ціни, немає чіткої згадки регіону
чи типу нерухомості) — краще пропустити оголошення далі, ніж мовчки відсіяти
щось підходяще через неповні дані джерела. Відсіюємо лише коли є явний
НЕГАТИВНИЙ сигнал (точно вказано "квартира" без жодної згадки будинку, точно
вказано інше велике місто, ціна явно поза діапазоном) — не за відсутністю
позитивного сигналу.
"""

from __future__ import annotations

from typing import Any

REGION_LOCALITIES = (
    "івано-франківськ", "крихівці", "ямниця", "угорники", "черніїв",
    "тисменичани", "вовчинець", "вовчинецька", "микитинці", "опришівці",
    "опришiвцi", "княгинин", "пасічна", "будівельників",
)

# Явно ІНШІ великі міста — якщо згадане одне з них і жодної нашої локації,
# це справжній негативний сигнал (не просто "бракує даних").
OTHER_MAJOR_CITIES = (
    "київ", "львів", "одеса", "харків", "дніпро", "запоріжжя", "вінниця",
    "тернопіль", "чернівці", "ужгород", "хмельницький", "рівне", "луцьк",
    "полтава", "черкаси", "суми", "житомир", "миколаїв", "херсон",
    "кропивницький", "чернігів",
)

# Джерела, де тип нерухомості вже відфільтрований на рівні URL самого сайту.
HOUSE_FILTERED_SOURCES = {"lun.ua", "dom.ria", "olx", "m2bomber", "flatfy.ua"}

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


def matches_region(listing: dict[str, Any]) -> bool:
    blob = _text_blob(listing)
    if any(loc in blob for loc in REGION_LOCALITIES):
        return True
    # Немає нашої локації в тексті — це відмова, лише якщо натомість явно
    # назване інше велике місто. Просто відсутність згадки (короткий заголовок,
    # порожня адреса тощо) — не привід відсіювати.
    return not any(city in blob for city in OTHER_MAJOR_CITIES)


def matches_house_type(listing: dict[str, Any]) -> bool:
    if listing.get("source") in HOUSE_FILTERED_SOURCES:
        return True

    blob = _text_blob(listing)
    title = (listing.get("title") or "").lower()
    looks_like_house = any(k in blob for k in HOUSE_KEYWORDS)
    # Відсіюємо тільки явний негативний сигнал: заголовок прямо каже "квартира"
    # і ніде нема згадки будинку. Якщо взагалі немає чіткого сигналу (ні
    # "будинок", ні "квартира") — пропускаємо, а не відсіюємо.
    looks_like_apartment_only = any(m in title for m in APARTMENT_MARKERS) and not looks_like_house
    return not looks_like_apartment_only


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


def passes_hard_filters(listing: dict[str, Any]) -> bool:
    return matches_region(listing) and matches_house_type(listing) and matches_price(listing)
