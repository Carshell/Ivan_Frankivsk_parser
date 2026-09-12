"""Парсер flatfy.ua через Playwright.

flatfy.ua — той самий продукт/агрегатор, що й lun.ua (спільний JSON-LD формат
ItemList/RealEstateListing, спільний CDN фото market-images.lunstatic.net,
той самий data-event-options="site:XXX|page_id:NNN" з оригінальним джерелом),
але під іншим доменом/брендом і з ІНШОЮ (не хешованою!) версткою карток —
семантичні класи на кшталт "realty-preview", "realty-preview-title__link".

На відміну від lun.ua, тут повний опис і всі property-рядки вже є прямо в
картці пошуку — окрему сторінку оголошення відвідувати не треба.

Важливий наслідок: значна частина оголошень тут буде дублювати те, що вже
надійшло з lun.ua (той самий агрегатор), — дедуп за external_id тут не
допоможе (ID у flatfy.ua свої), потрібен буде дедуп за посиланням на
оригінальне джерело (origin_site + там same third-party page_id) чи за
адресою/фото, коли з'явиться відповідний модуль (п.7 ТЗ).

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.flatfy
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

import currency

logger = logging.getLogger(__name__)

SOURCE_NAME = "flatfy.ua"
BASE_URL = "https://flatfy.ua"
DEFAULT_SEARCH_URL = (
    "https://flatfy.ua/uk/search?currency=USD&geo_id=10008717&has_eoselia=false"
    "&is_without_fee=false&price_max=7000&price_min=700&price_sqm_currency=USD"
    "&section_id=4&sort=relevance"
)

CARD_SELECTOR = "article.realty-preview"
MAX_SCROLL_ATTEMPTS = 6  # інфініт-скрол без явної пагінації — best-effort, див. docstring parse()

MONTHS_UK = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

UA_LAT_RANGE = (43.0, 53.0)
UA_LON_RANGE = (20.0, 41.0)

_JS_EXTRACT_CARDS = """
(rootSelector) => {
  const cards = document.querySelectorAll(rootSelector);
  return Array.from(cards).map(card => {
    const optsEl = card.querySelector('[data-event-options*="page_id"]');
    const opts = optsEl ? optsEl.getAttribute('data-event-options') : null;
    if (!opts) return null;

    const priceEl = card.querySelector('.realty-preview-price--main');
    const titleEl = card.querySelector('.realty-preview-title__link, .realty-preview-title');
    const subTitleEl = card.querySelector('.realty-preview-sub-title');
    const descEl = card.querySelector('.realty-preview-description__text');
    const rows = Array.from(card.querySelectorAll('.realty-preview-properties-item .realty-preview-info'))
      .map(s => s.textContent.trim())
      .filter(Boolean);
    const dateSpans = Array.from(card.querySelectorAll('.realty-preview-dates__value'))
      .map(s => s.textContent.trim());
    const labels = Array.from(card.querySelectorAll('.realty-preview__label-holder .realty-preview__label'))
      .map(s => s.textContent.trim())
      .filter(Boolean);

    return {
      id: card.id,
      opts,
      price_text: priceEl ? priceEl.textContent.trim() : null,
      title: titleEl ? titleEl.textContent.trim() : null,
      sub_title: subTitleEl ? subTitleEl.textContent.trim() : null,
      description: descEl ? descEl.textContent.trim() : null,
      rows,
      date_texts: dateSpans,
      labels,
    };
  }).filter(Boolean);
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
    result: dict[str, str] = {}
    for part in opts.split("|"):
        if ":" in part:
            key, _, value = part.partition(":")
            result[key] = value
    return result


def _parse_price(text: str) -> tuple[float | None, float | None]:
    m = re.search(r"([\d\s ]+)\s*(\$|€|₴)|(\$|€|₴)\s*([\d\s ]+)", text)
    if not m:
        return None, None
    amount_str = m.group(1) or m.group(4)
    symbol = m.group(2) or m.group(3)
    amount = float(amount_str.replace(" ", "").replace(" ", ""))
    if symbol == "$":
        return amount, currency.usd_to_uah(amount)
    if symbol == "₴":
        return currency.uah_to_usd(amount), amount
    return None, None  # € — немає курсу в currency.py, не вигадуємо


def _parse_property_rows(rows: list[str]) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "rooms": None, "area": None, "area_living": None, "area_kitchen": None, "floor_total": None,
    }
    for text in rows:
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
        m = re.search(r"(\d+)-поверховий", text)
        if m:
            result["floor_total"] = int(m.group(1))
            continue
        m = re.search(r"^([\d.]+)\s*м²?\s*$", text.strip())
        if m and result["area"] is None:
            result["area"] = float(m.group(1))
    return result


def _parse_relative_date(text: str, now: datetime | None = None) -> str | None:
    if not text:
        return None
    now = now or datetime.now()
    text = text.strip().lower().removeprefix("створено").strip()

    if text == "сьогодні":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if text == "вчора":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

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
    """Той самий запобіжник, що й у lun.py — на випадок, якщо ці два продукти
    колись поділять і цей баг (переплутані місцями latitude/longitude)."""
    if raw_lat is None or raw_lon is None:
        return None, None
    if UA_LAT_RANGE[0] <= raw_lat <= UA_LAT_RANGE[1] and UA_LON_RANGE[0] <= raw_lon <= UA_LON_RANGE[1]:
        return raw_lat, raw_lon
    if UA_LAT_RANGE[0] <= raw_lon <= UA_LAT_RANGE[1] and UA_LON_RANGE[0] <= raw_lat <= UA_LON_RANGE[1]:
        return raw_lon, raw_lat
    return raw_lat, raw_lon


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
    external_id = dom_card.get("id") or opts.get("page_id")
    if not external_id:
        return None

    props = _parse_property_rows(dom_card.get("rows", []))
    price_usd, price_uah = _parse_price(dom_card.get("price_text") or "")

    date_texts = dom_card.get("date_texts", [])
    found_at = _parse_relative_date(date_texts[0]) if len(date_texts) > 0 else None
    published_at = _parse_relative_date(date_texts[1]) if len(date_texts) > 1 else found_at

    geo = ld_item.get("geo") or {}
    lat, lon = _fix_lat_lon(geo.get("latitude"), geo.get("longitude"))

    rooms = props["rooms"] or ld_item.get("numberOfRooms")
    address_locality = ((ld_item.get("address") or {}).get("addressLocality"))
    address_parts = [p for p in [dom_card.get("sub_title"), address_locality] if p]
    address = ", ".join(dict.fromkeys(address_parts)) if address_parts else None

    return asdict(
        Listing(
            source=SOURCE_NAME,
            url=f"{BASE_URL}/uk/realty/{external_id}",
            external_id=str(external_id),
            origin_site=opts.get("site"),
            published_at=published_at,
            found_at=found_at,
            title=dom_card.get("title") or ld_item.get("name"),
            description=dom_card.get("description") or ld_item.get("description") or None,
            price_usd=price_usd,
            price_uah=price_uah,
            area=props["area"],
            area_living=props["area_living"],
            area_kitchen=props["area_kitchen"],
            land_area_sotka=None,
            rooms=rooms,
            floor=None,
            floor_total=props["floor_total"],
            address=address,
            lat=lat,
            lon=lon,
            photos=ld_item.get("image") or [],
            contact=None,
            features=list(dict.fromkeys(dom_card.get("labels", []))),
            raw_snapshot={"json_ld": ld_item, "dom_card": dom_card},
        )
    )


async def _try_load_more(page: Page) -> bool:
    """flatfy.ua не має видимої пагінації (ні ?page=, ні кнопки) — лише щось
    схоже на лінивий довантаж списку. Best-effort: імітуємо реальний скрол
    колесом миші кілька разів; якщо кількість карток не зросла — здаємось.
    Непротестовано на запиті з великою кількістю сторінок.
    """
    before = len(await page.query_selector_all(CARD_SELECTOR))
    for _ in range(MAX_SCROLL_ATTEMPTS):
        await page.mouse.wheel(0, 4000)
        await page.wait_for_timeout(700)
    after = len(await page.query_selector_all(CARD_SELECTOR))
    return after > before


async def parse(search_url: str = DEFAULT_SEARCH_URL, headless: bool = True) -> list[dict[str, Any]]:
    listings: dict[str, dict[str, Any]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        try:
            page = await browser.new_page(
                locale="uk-UA",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
            )
            await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector(CARD_SELECTOR, state="attached", timeout=30000)
            except Exception:
                logger.warning("flatfy.ua: не знайдено жодної картки на %s", search_url)
                return []

            await _try_load_more(page)

            ld_items = await _extract_json_ld_items(page)
            dom_cards = await page.evaluate(_JS_EXTRACT_CARDS, CARD_SELECTOR)

            if len(ld_items) != len(dom_cards):
                logger.warning(
                    "flatfy.ua: кількість елементів JSON-LD (%d) не збігається з DOM-картками (%d) "
                    "— можлива розсинхронізація парних даних",
                    len(ld_items), len(dom_cards),
                )

            for ld_item, dom_card in zip(ld_items, dom_cards):
                listing = _normalize(ld_item, dom_card)
                if listing:
                    listings[listing["external_id"]] = listing

            logger.info("flatfy.ua: зібрано %d оголошень", len(listings))
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("flatfy_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
