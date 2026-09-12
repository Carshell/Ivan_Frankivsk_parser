"""
Парсер lun.ua (оренда, Івано-Франківськ) через Playwright.

lun.ua — агрегатор (позиції в списку посилаються на rieltor.ua / olx.ua тощо
через realty/{id}), тому це джерело одразу дає кілька майданчиків "з коробки".

Дані картки беруться з двох джерел на одній сторінці:
  1. JSON-LD (schema.org ItemList/Apartment) — опис, фото, ціна, гео, кімнати;
  2. DOM картки — стабільний атрибут data-event-options (site + page_id),
     дати, floor/поверх, мітки (наприклад "є укриття").
CSS-класи на сайті — хешовані CSS-модулі (напр. "...__tBtxOq__price") і
можуть змінюватись при кожному деплої, тому селектори побудовані на частинах
імені класу через [class*=...], а не на повних хешах.

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.lun
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

logger = logging.getLogger(__name__)

SOURCE_NAME = "lun.ua"
BASE_URL = "https://lun.ua"
DEFAULT_SEARCH_URL = "https://lun.ua/rent/if/flats?price_min=1300&price_max=4500&currency=USD"

CARD_ROOT_SELECTOR = '[class*="RealtyCard-module"][class*="__root"]'

MONTHS_UK = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

# Ukraine приблизний bounding box — використовується щоб виправити відомий
# баг lun.ua: у JSON-LD поля latitude/longitude заповнені навпаки
# (у "latitude" фактично довгота ~22-40, у "longitude" — широта ~44-53).
UA_LAT_RANGE = (43.0, 53.0)
UA_LON_RANGE = (20.0, 41.0)

# JS, що виконується в браузері: для кожної картки повертає тільки стабільні,
# нехешовані ознаки (data-атрибути, geo-хлібні крихти, текст блоків).
_JS_EXTRACT_CARDS = """
(rootSelector) => {
  const roots = document.querySelectorAll(rootSelector);
  const out = [];
  roots.forEach(r => {
    const linkBtn = r.querySelector('[class*="__link"]');
    const opts = linkBtn ? linkBtn.getAttribute('data-event-options') : null;
    if (!opts) return;

    const priceEl = r.querySelector('[class*="__price"]:not([class*="Sqm"])');
    const propertyItems = Array.from(r.querySelectorAll('[class*="__propertyItem"]'))
      .map(x => x.textContent.trim());
    const dateEls = Array.from(r.querySelectorAll('[class*="__propertyDate"]'))
      .map(x => x.textContent.trim());
    const titleEl = r.querySelector('[class*="RealtyCard-module"][class*="__title"]');
    const geoAnchors = Array.from(r.querySelectorAll('a[class*="RealtyCardSubGeoItem"]'))
      .map(a => a.textContent.trim());
    const labels = Array.from(r.querySelectorAll('[class*="__labelList"] *'))
      .map(x => x.textContent.trim())
      .filter(Boolean);

    out.push({
      opts,
      price_text: priceEl ? priceEl.textContent.trim() : null,
      property_items: propertyItems,
      date_texts: dateEls,
      title: titleEl ? titleEl.textContent.trim() : null,
      geo_anchors: geoAnchors,
      labels,
    });
  });
  return out;
}
"""


@dataclass
class Listing:
    source: str
    url: str
    external_id: str
    origin_site: str | None
    published_at: str | None
    found_at: str | None
    title: str | None
    description: str | None
    price_usd: float | None
    price_uah: float | None
    area: float | None
    area_living: float | None
    area_kitchen: float | None
    land_area_sotka: float | None
    rooms: int | None
    floor: int | None
    floor_total: int | None
    address: str | None
    lat: float | None
    lon: float | None
    photos: list[str] = field(default_factory=list)
    contact: str | None = None
    features: list[str] = field(default_factory=list)
    raw_snapshot: dict[str, Any] = field(default_factory=dict)


def _parse_card_opts(opts: str) -> dict[str, str]:
    """'site:rieltor.ua|page_id:123|generator:0' -> {"site": "rieltor.ua", "page_id": "123", ...}"""
    result: dict[str, str] = {}
    for part in opts.split("|"):
        if ":" in part:
            key, _, value = part.partition(":")
            result[key] = value
    return result


def _parse_property_items(items: list[str]) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "rooms": None,
        "area": None,
        "area_living": None,
        "area_kitchen": None,
        "land_area_sotka": None,
        "floor": None,
        "floor_total": None,
    }
    for text in items:
        m = re.search(r"(\d+)\s*кімнат", text)
        if m:
            result["rooms"] = int(m.group(1))
            continue
        m = re.search(r"([\d.]+)\s*/\s*([\d.]+|-)\s*/\s*([\d.]+|-)\s*м", text)
        if m:
            result["area"] = float(m.group(1))
            if m.group(2) != "-":
                result["area_living"] = float(m.group(2))
            if m.group(3) != "-":
                result["area_kitchen"] = float(m.group(3))
            continue
        m = re.search(r"поверх\s*(\d+)\s*з\s*(\d+)", text)
        if m:
            result["floor"] = int(m.group(1))
            result["floor_total"] = int(m.group(2))
            continue
        # Будинки: земельна ділянка ("10 сот") і загальна поверховість ("3 поверхи"),
        # на відміну від квартир, де це "поверх X з Y" (поточний/всього в будинку).
        m = re.search(r"([\d.]+)\s*сот", text)
        if m:
            result["land_area_sotka"] = float(m.group(1))
            continue
        m = re.search(r"^(\d+)\s*поверх", text)
        if m and result["floor_total"] is None:
            result["floor_total"] = int(m.group(1))
            continue
        # Будинки без розбивки на житлову/кухню показують площу одним числом ("360 м²").
        m = re.search(r"^([\d.]+)\s*м²?\s*$", text.strip())
        if m and result["area"] is None:
            result["area"] = float(m.group(1))
    return result


def _parse_relative_date(text: str, now: datetime | None = None) -> str | None:
    """'вчора о 12:44' / 'сьогодні о 9:03' / '8 вересня' -> ISO datetime string (best-effort)."""
    if not text:
        return None
    now = now or datetime.now()
    text = text.strip().lower().removeprefix("знайдено ").strip()

    m = re.match(r"(сьогодні|вчора)\s+о\s+(\d{1,2}):(\d{2})", text)
    if m:
        day_word, hour, minute = m.groups()
        dt = now if day_word == "сьогодні" else now - timedelta(days=1)
        return dt.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0).isoformat()

    m = re.match(r"(\d{1,2})\s+([а-яїієґ']+)(?:\s+(\d{4}))?", text)
    if m:
        day, month_name, year = m.groups()
        month = MONTHS_UK.get(month_name)
        if not month:
            return None
        year_int = int(year) if year else now.year
        try:
            dt = datetime(year_int, month, int(day))
        except ValueError:
            return None
        if not year and dt > now:
            dt = dt.replace(year=year_int - 1)
        return dt.isoformat()

    return None


def _fix_lat_lon(raw_lat: float | None, raw_lon: float | None) -> tuple[float | None, float | None]:
    if raw_lat is None or raw_lon is None:
        return None, None
    lat_in_range = UA_LAT_RANGE[0] <= raw_lat <= UA_LAT_RANGE[1]
    lon_in_range = UA_LON_RANGE[0] <= raw_lon <= UA_LON_RANGE[1]
    if lat_in_range and lon_in_range:
        return raw_lat, raw_lon
    # Відомий баг lun.ua: значення latitude/longitude в JSON-LD переплутані місцями.
    if UA_LAT_RANGE[0] <= raw_lon <= UA_LAT_RANGE[1] and UA_LON_RANGE[0] <= raw_lat <= UA_LON_RANGE[1]:
        return raw_lon, raw_lat
    return raw_lat, raw_lon


def _build_address(title: str | None, geo_anchors: list[str]) -> str | None:
    parts: list[str] = []
    if title and not re.match(r"^ЖК\s", title, re.IGNORECASE):
        parts.append(title)
    for anchor in geo_anchors:
        if anchor not in parts:
            parts.append(anchor)
    return ", ".join(parts) if parts else None


async def _extract_json_ld_items(page: Page) -> list[dict[str, Any]]:
    raw_scripts = await page.eval_on_selector_all(
        'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
    )
    for raw in raw_scripts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        types = data.get("@type")
        if isinstance(types, list) and "ItemList" in types:
            return [el.get("item", {}) for el in data.get("itemListElement", [])]
    return []


def _normalize(ld_item: dict[str, Any], dom_card: dict[str, Any]) -> dict[str, Any] | None:
    opts = _parse_card_opts(dom_card["opts"])
    external_id = opts.get("page_id")
    if not external_id:
        return None

    property_fields = _parse_property_items(dom_card.get("property_items", []))
    date_texts = dom_card.get("date_texts", [])
    published_at = _parse_relative_date(date_texts[0]) if len(date_texts) > 0 else None
    found_at = _parse_relative_date(date_texts[1]) if len(date_texts) > 1 else None

    geo = ld_item.get("geo") or {}
    lat, lon = _fix_lat_lon(geo.get("latitude"), geo.get("longitude"))

    offers = ld_item.get("offers") or {}
    price_usd = offers.get("price") if str(offers.get("priceCurrency", "")).lower() == "usd" else None

    rooms = ld_item.get("numberOfRooms") or property_fields["rooms"]
    features = list(dict.fromkeys(dom_card.get("labels", [])))  # унікальні, зберігаючи порядок

    title = dom_card.get("title") or (ld_item.get("name") or None)
    description = ld_item.get("description") or None

    return asdict(
        Listing(
            source=SOURCE_NAME,
            url=f"{BASE_URL}/realty/{external_id}",
            external_id=external_id,
            origin_site=opts.get("site"),
            published_at=published_at,
            found_at=found_at,
            title=title,
            description=description,
            price_usd=price_usd,
            price_uah=None,  # конвертація UAH<->USD виконується окремим модулем курсу НБУ
            area=property_fields["area"],
            area_living=property_fields["area_living"],
            area_kitchen=property_fields["area_kitchen"],
            land_area_sotka=property_fields["land_area_sotka"],
            rooms=rooms,
            floor=property_fields["floor"],
            floor_total=property_fields["floor_total"],
            address=_build_address(dom_card.get("title"), dom_card.get("geo_anchors", [])),
            lat=lat,
            lon=lon,
            photos=ld_item.get("image") or [],
            contact=None,  # телефон/контакт відкривається лише по кліку на детальній сторінці
            features=features,
            raw_snapshot={"json_ld": ld_item, "dom_card": dom_card},
        )
    )


async def _goto_next_page(page: Page) -> bool:
    """Клікає по кнопці "наступна сторінка" пагінації, якщо вона є.

    Best-effort: на момент написання тестовий запит повертав лише 1 сторінку
    результатів, тому селектор пагінації не був перевірений на реальному
    "наступна" контролі. Перед продакшеном перевірити на запиті з великою
    кількістю оголошень (наприклад категорія houses).
    """
    pagination = page.locator('[class*="RealtiesLayout-module"][class*="__pagination"]')
    if await pagination.count() == 0:
        return False

    next_control = pagination.locator(
        'a[aria-label*="аступ" i], button[aria-label*="аступ" i], '
        'a:has-text(">"), button:has-text(">")'
    )
    if await next_control.count() == 0:
        return False

    first_card_before = await page.locator(CARD_ROOT_SELECTOR).first.get_attribute("class")
    await next_control.first.click()
    try:
        await page.wait_for_function(
            """(args) => {
                const el = document.querySelector(args.selector);
                return el && el.getAttribute('class') !== args.before;
            }""",
            arg={"selector": CARD_ROOT_SELECTOR, "before": first_card_before},
            timeout=10000,
        )
    except Exception:
        return False
    return True


async def parse(
    search_url: str = DEFAULT_SEARCH_URL,
    max_pages: int = 10,
    headless: bool = True,
) -> list[dict[str, Any]]:
    """Парсить сторінку(и) пошуку lun.ua і повертає нормалізовані оголошення.

    Дедуплікація за external_id відбувається тут лише в межах одного прогону;
    "чи вже надсилали в бот раніше" — відповідальність окремого модуля дедупу/БД.
    """
    listings: dict[str, dict[str, Any]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            # lun.ua стоїть за Cloudflare: дефолтний UA/viewport Playwright ловить
            # non-interactive challenge, який ніколи не резолвиться. З реалістичним
            # десктопним UA challenge проходить сам за ~10-15с.
            page = await browser.new_page(
                locale="uk-UA",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
            )
            # "networkidle" ніколи не настає на цій сторінці (карта/аналітика тримають
            # з'єднання відкритими) — чекаємо появи карток окремо, нижче.
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)

            for page_num in range(1, max_pages + 1):
                try:
                    await page.wait_for_selector(CARD_ROOT_SELECTOR, timeout=30000)
                except Exception:
                    logger.warning("Не знайдено жодної картки на сторінці %s", page_num)
                    break
                # Дата картки ("вчора о ...") домальовується клієнтським JS з невеликим
                # лагом після появи самих карток — без цієї паузи date_texts порожні.
                await page.wait_for_timeout(800)

                ld_items = await _extract_json_ld_items(page)
                dom_cards = await page.evaluate(_JS_EXTRACT_CARDS, CARD_ROOT_SELECTOR)

                if len(ld_items) != len(dom_cards):
                    logger.warning(
                        "Кількість елементів JSON-LD (%d) не збігається з DOM-картками (%d) "
                        "на сторінці %s — можлива розсинхронізація парних даних",
                        len(ld_items), len(dom_cards), page_num,
                    )

                for ld_item, dom_card in zip(ld_items, dom_cards):
                    listing = _normalize(ld_item, dom_card)
                    if listing:
                        listings[listing["external_id"]] = listing

                logger.info("Сторінка %s: зібрано %d оголошень (всього унікальних: %d)",
                            page_num, len(dom_cards), len(listings))

                if not await _goto_next_page(page):
                    break
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("lun_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
