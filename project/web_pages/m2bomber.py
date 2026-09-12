"""Парсер m2bomber.com через Playwright.

m2bomber — теж агрегатор (та сама сторінка може показувати оголошення, вже
знайомі з dom.ria/olx/rieltor — перевірено: один і той самий "ID NNNNNNN" в
заголовку збігається з зовнішнім джерелом). Ще один шар дублів до майбутнього
крос-джерельного дедупу (п.7 ТЗ), як і з flatfy.ua.

Картки пошуку вже дають ціну (одразу в ₴/$/€ — не треба currency.py), кімнати/
площу/поверховість, короткий опис (часто повний, якщо у джерела він є) і
статус агентство/власник. Точні координати і повна фотогалерея — лише на
сторінці самого оголошення (є стабільний #map[data-map-lat/lon] і /storage/
obj/{id}/normal-images/ фото), тому на кожну картку окремо заходимо.

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.m2bomber
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
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import Page, async_playwright

logger = logging.getLogger(__name__)

SOURCE_NAME = "m2bomber"
BASE_URL = "https://ua.m2bomber.com"
DEFAULT_SEARCH_URL = (
    "https://ua.m2bomber.com/search?address=%D0%86%D0%B2%D0%B0%D0%BD%D0%BE-"
    "%D0%A4%D1%80%D0%B0%D0%BD%D0%BA%D1%96%D0%B2%D1%81%D1%8C%D0%BA%D0%B8%D0%B9"
    "+%D1%80%D0%B0%D0%B9%D0%BE%D0%BD%2C+%D0%86%D0%B2%D0%B0%D0%BD%D0%BE-"
    "%D0%A4%D1%80%D0%B0%D0%BD%D0%BA%D1%96%D0%B2%D1%81%D1%8C%D0%BA%D0%B8%D0%B9"
    "&type=house-rent&osmId=2362670&bn=&rooms=&area=&price=%D0%B2%D1%96%D0%B4"
    "+%E2%82%B450000&pricePerM2=false&year=&text=&photo=false&owner=false"
)

MAX_PAGES = 10
MAX_LISTINGS_PER_RUN = 60

MONTHS_UK_SHORT = {
    "січ": 1, "лют": 2, "бер": 3, "квіт": 4, "трав": 5, "черв": 6,
    "лип": 7, "серп": 8, "вер": 9, "жовт": 10, "лист": 11, "груд": 12,
}


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


def _with_page_param(url: str, page_num: int) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    query["page"] = [str(page_num)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _parse_relative_date(text: str, now: datetime | None = None) -> str | None:
    if not text:
        return None
    now = now or datetime.now()
    text = text.strip().lower()

    if text.startswith("сьогодні"):
        m = re.search(r"(\d{1,2}):(\d{2})", text)
        base = now
        if m:
            base = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return base.isoformat()
    if text.startswith("вчора"):
        base = now - timedelta(days=1)
        m = re.search(r"(\d{1,2}):(\d{2})", text)
        if m:
            base = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return base.isoformat()

    m = re.match(r"(\d{1,2})\s+([а-яїієґ]+)\.?\s+(\d{4})(?:,\s*(\d{1,2}):(\d{2}))?", text)
    if m:
        day, month_short, year, hour, minute = m.groups()
        month = MONTHS_UK_SHORT.get(month_short[:4]) or MONTHS_UK_SHORT.get(month_short[:3])
        if not month:
            return None
        try:
            dt = datetime(int(year), month, int(day), int(hour or 0), int(minute or 0))
        except ValueError:
            return None
        return dt.isoformat()

    return None


def _parse_price_buttons(price_text: str, currency_buttons: list[dict[str, str]]) -> tuple[float | None, float | None]:
    def _to_float(raw: str) -> float | None:
        digits = re.sub(r"[^\d.]", "", raw.replace(",", "."))
        return float(digits) if digits else None

    price_usd = price_uah = None
    for btn in currency_buttons:
        value = btn.get("value", "")
        if "$" in value:
            price_usd = _to_float(value)
        elif "₴" in value:
            price_uah = _to_float(value)
    if price_uah is None and "₴" in price_text:
        price_uah = _to_float(price_text)
    return price_usd, price_uah


def _parse_property_spans(spans: list[str]) -> dict[str, int | float | None]:
    result: dict[str, int | float | None] = {"rooms": None, "area": None, "floor_total": None}
    for text in spans:
        m = re.search(r"(\d+)\s*-?\s*кімн", text)
        if m:
            result["rooms"] = int(m.group(1))
            continue
        m = re.search(r"([\d.]+)\s*м²", text)
        if m:
            result["area"] = float(m.group(1))
            continue
        m = re.search(r"(\d+)\s*п\b", text)
        if m:
            result["floor_total"] = int(m.group(1))
    return result


_JS_EXTRACT_CARDS = """
() => {
  const cards = document.querySelectorAll('.item-card-long');
  return Array.from(cards).map(card => {
    const link = card.querySelector('a[href*="/obj/"]');
    const favBtn = card.querySelector('.add-to-favorites-aux');
    const priceEl = card.querySelector('.price-full');
    const currencyButtons = Array.from(card.querySelectorAll('.fullcard-price-currency button, [class*="price-currency"] button'))
      .map(b => ({ value: b.getAttribute('data-value') || '' }));
    const titleEl = card.querySelector('.item-card-long-title');
    const addressEl = card.querySelector('.item-card-long-address');
    const roomSpans = Array.from(card.querySelectorAll('.item-card-long-rooms span')).map(s => s.textContent.trim());
    const descEl = card.querySelector('.item-card-long-desc');
    const footerText = card.querySelector('.item-card-long-footer') ? card.querySelector('.item-card-long-footer').textContent.trim() : '';
    const timeEl = card.querySelector('.item-card-long-footer time');
    return {
      internal_id: favBtn ? favBtn.getAttribute('data-value') : null,
      url: link ? link.href : null,
      price_text: priceEl ? priceEl.textContent.trim() : '',
      currency_buttons: currencyButtons,
      title: titleEl ? titleEl.textContent.trim() : null,
      address: addressEl ? addressEl.textContent.trim() : null,
      room_spans: roomSpans,
      description: descEl ? descEl.textContent.trim() : null,
      is_agency: /від агентства/i.test(footerText),
      list_date_text: timeEl ? timeEl.textContent.trim() : null,
    };
  });
}
"""


async def _extract_search_page_cards(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate(_JS_EXTRACT_CARDS)


async def _extract_detail_extra(page: Page, url: str, internal_id: str) -> dict[str, Any]:
    extra: dict[str, Any] = {"lat": None, "lon": None, "photos": [], "published_at": None, "found_at": None}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector("#map", state="attached", timeout=30000)
    except Exception:
        logger.warning("m2bomber: не вдалося завантажити %s", url)
        return extra

    map_coords = await page.evaluate(
        """() => {
            const m = document.querySelector('#map');
            if (!m) return null;
            return { lat: parseFloat(m.getAttribute('data-map-lat')), lon: parseFloat(m.getAttribute('data-map-lon')) };
        }"""
    )
    if map_coords:
        extra["lat"] = map_coords.get("lat")
        extra["lon"] = map_coords.get("lon")

    photos = await page.evaluate(
        """(internalId) => {
            // Галерея на слайдері (slick) віддає реальний URL через data-lazy,
            // поки не проскролена — src лишається порожнім ("slick-loading").
            const imgs = Array.from(document.querySelectorAll('img'));
            const pattern = new RegExp('/storage/obj/' + internalId + '/normal-images/');
            const urls = imgs
                .map(i => i.getAttribute('data-lazy') || i.src || i.getAttribute('data-src'))
                .filter(Boolean)
                .filter(u => pattern.test(u));
            return [...new Set(urls)];
        }""",
        internal_id,
    )
    extra["photos"] = [f"{BASE_URL}{p}" if p.startswith("/") else p for p in (photos or [])]

    meta_items = await page.evaluate(
        """() => {
            const meta = document.querySelector('.fullcard-meta');
            if (!meta) return [];
            return Array.from(meta.querySelectorAll('li')).map(li => li.textContent.replace(/\\s+/g, ' ').trim());
        }"""
    )
    for item in meta_items:
        if item.startswith("доданий"):
            extra["found_at"] = _parse_relative_date(item.split(":", 1)[1] if ":" in item else "")
        elif item.startswith("оновлено"):
            extra["published_at"] = _parse_relative_date(item.split(":", 1)[1] if ":" in item else "")

    return extra


def _normalize(card: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any] | None:
    internal_id = card.get("internal_id")
    url = card.get("url")
    if not internal_id or not url:
        return None

    props = _parse_property_spans(card.get("room_spans", []))
    price_usd, price_uah = _parse_price_buttons(card.get("price_text") or "", card.get("currency_buttons") or [])

    address = card.get("address") or ""
    address = re.sub(r"\s*•\s*ID\s*\d+\s*$", "", address).strip() or None

    description = card.get("description") or None
    if description and card.get("title") and description.strip() == card["title"].strip():
        description = None  # опис — просто дублікат заголовка, реального опису немає

    published_at = extra.get("published_at") or _parse_relative_date(card.get("list_date_text") or "")
    found_at = extra.get("found_at")

    features = ["агентство" if card.get("is_agency") else "власник"]

    return asdict(
        Listing(
            source=SOURCE_NAME,
            url=url,
            external_id=str(internal_id),
            origin_site=SOURCE_NAME,
            published_at=published_at,
            found_at=found_at,
            title=card.get("title"),
            description=description,
            price_usd=price_usd,
            price_uah=price_uah,
            area=props["area"],
            area_living=None,
            area_kitchen=None,
            land_area_sotka=None,
            rooms=props["rooms"],
            floor=None,
            floor_total=props["floor_total"],
            address=address,
            lat=extra.get("lat"),
            lon=extra.get("lon"),
            photos=extra.get("photos") or [],
            contact=None,
            features=features,
            raw_snapshot={"card": card, "extra": extra},
        )
    )


async def parse(
    search_url: str = DEFAULT_SEARCH_URL,
    max_pages: int = MAX_PAGES,
    headless: bool = True,
) -> list[dict[str, Any]]:
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

            cards_by_id: dict[str, dict[str, Any]] = {}
            for page_num in range(1, max_pages + 1):
                target = search_url if page_num == 1 else _with_page_param(search_url, page_num)
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_selector(".item-card-long", state="attached", timeout=20000)
                except Exception:
                    if page_num == 1:
                        logger.warning("m2bomber: не знайдено жодної картки на %s", target)
                    break

                page_cards = await _extract_search_page_cards(page)
                new_cards = [c for c in page_cards if c.get("internal_id") and c["internal_id"] not in cards_by_id]
                if not new_cards:
                    break
                for c in new_cards:
                    cards_by_id[c["internal_id"]] = c
                if len(cards_by_id) >= MAX_LISTINGS_PER_RUN:
                    break

            logger.info("m2bomber: знайдено %d карток у видачі", len(cards_by_id))

            for internal_id, card in list(cards_by_id.items())[:MAX_LISTINGS_PER_RUN]:
                try:
                    extra = await _extract_detail_extra(page, card["url"], internal_id)
                    listing = _normalize(card, extra)
                    if listing:
                        listings[listing["external_id"]] = listing
                except Exception:
                    logger.exception("m2bomber: не вдалося обробити %s", card.get("url"))
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("m2bomber_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
