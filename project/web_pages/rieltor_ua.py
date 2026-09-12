"""Парсер rieltor.ua через Playwright.

rieltor.ua належить тій самій компанії, що й lun.ua (спільний CDN фото
market-images.lunstatic.net, спільний формат "N кімнати"/"X / Y / Z м²"/
"поверх X з Y"), але на відміну від lun.ua це старіший сервер-рендерений
сайт зі стабільними семантичними CSS-класами (.catalog-card, .catalog-card-*) —
без хешованих модулів і без потреби перехоплювати внутрішнє API.

Картки пошуку вже містять координати (data-latitude/data-longitude), ціну,
адресу, кімнати/площу/поверх і фото — але не повний опис і не блок
"Працює без світла" (енергонезалежність, критично важливо за профілем
замовника). За ними йдемо на сторінку кожного оголошення окремо.

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.rieltor_ua
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

import currency

logger = logging.getLogger(__name__)

SOURCE_NAME = "rieltor.ua"
BASE_URL = "https://rieltor.ua"
DEFAULT_SEARCH_URL = (
    "https://rieltor.ua/ivano-frankovsk/flats-rent/"
    "?currency=2&price_min=700&price_max=7000&radius=50&sort=-default&autonomy_power=1"
)

MAX_PAGES = 10
MAX_LISTINGS_PER_RUN = 60

_DATE_ROW_RE = re.compile(r"^(сьогодні|вчора|\d+\s*(?:год|дн|тиж|міс)\.?\s*тому)$", re.IGNORECASE)
_RESOLUTION_RE = re.compile(r"/\d+/\d+/images/")


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


def _upscale_photo(url: str) -> str:
    return _RESOLUTION_RE.sub("/1200/1200/images/", url)


def _parse_price(text: str) -> tuple[float | None, float | None]:
    """"1 200 $/міс" / "26 000 ₴/міс" -> (usd, uah), рахуючи курсом НБУ те, чого немає напряму."""
    m = re.search(r"([\d\s]+)\s*(\$|€|₴)", text)
    if not m:
        return None, None
    amount = float(m.group(1).replace(" ", "").replace("\xa0", ""))
    symbol = m.group(2)
    if symbol == "$":
        return amount, currency.usd_to_uah(amount)
    if symbol == "₴":
        return currency.uah_to_usd(amount), amount
    return None, None  # € — немає курсу в currency.py, не вигадуємо


def _parse_property_rows(rows: list[str]) -> dict[str, float | int | None]:
    result: dict[str, float | int | None] = {
        "rooms": None, "area": None, "area_living": None, "area_kitchen": None,
        "floor": None, "floor_total": None,
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
        m = re.search(r"поверх\s*(\d+)\s*з\s*(\d+)", text)
        if m:
            result["floor"] = int(m.group(1))
            result["floor_total"] = int(m.group(2))
    return result


def _parse_relative_date(text: str, now: datetime | None = None) -> str | None:
    now = now or datetime.now()
    text = text.strip().lower()
    if text == "сьогодні":
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    if text == "вчора":
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    m = re.match(r"(\d+)\s*(год|дн|тиж|міс)", text)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {
            "год": timedelta(hours=n), "дн": timedelta(days=n),
            "тиж": timedelta(weeks=n), "міс": timedelta(days=n * 30),
        }.get(unit)
        if delta:
            return (now - delta).isoformat()
    return None


_JS_EXTRACT_CARDS = """
() => {
  const cards = document.querySelectorAll('.catalog-card[data-catalog-item-id]');
  return Array.from(cards).map(card => {
    const link = card.querySelector('a[href*="/view/"]');
    const priceEl = card.querySelector('.catalog-card-price-title');
    const addressEl = card.querySelector('.catalog-card-address');
    const regionEl = card.querySelector('.catalog-card-region');
    const rows = Array.from(card.querySelectorAll('.catalog-card-details-row span')).map(s => s.textContent.trim());
    const chips = Array.from(card.querySelectorAll('.catalog-card-chip'))
      .map(c => c.textContent.trim())
      .filter(t => t && !/премі/i.test(t));
    const photos = Array.from(card.querySelectorAll('.offer-photo-slider-slide img'))
      .map(img => img.getAttribute('src'))
      .filter(Boolean);
    return {
      external_id: card.getAttribute('data-catalog-item-id'),
      lat: parseFloat(card.getAttribute('data-latitude')) || null,
      lon: parseFloat(card.getAttribute('data-longitude')) || null,
      url: link ? link.href : null,
      price_text: priceEl ? priceEl.textContent.trim() : null,
      address: addressEl ? addressEl.textContent.trim() : null,
      region: regionEl ? regionEl.textContent.trim() : null,
      rows,
      chips,
      photos,
    };
  });
}
"""


async def _extract_search_page_cards(page: Page) -> list[dict[str, Any]]:
    return await page.evaluate(_JS_EXTRACT_CARDS)


async def _extract_detail_extra(page: Page, url: str) -> dict[str, Any]:
    """Опис, комунікації (енергонезалежність), меблювання, дати — лише на сторінці оголошення."""
    extra: dict[str, Any] = {"description": None, "features": [], "published_at": None, "found_at": None}
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(".offer-view-section", state="attached", timeout=30000)
    except Exception:
        logger.warning("rieltor.ua: не вдалося завантажити %s", url)
        return extra

    description = await page.evaluate(
        """() => {
            const sections = Array.from(document.querySelectorAll('.offer-view-section'));
            const desc = sections.find(s =>
                !s.classList.contains('offer-view-communications') &&
                !s.classList.contains('offer-view-planning') &&
                !s.classList.contains('offer-view-address-block') &&
                s.querySelector('.offer-view-section-title')
            );
            if (!desc) return null;
            const clone = desc.cloneNode(true);
            const title = clone.querySelector('.offer-view-section-title');
            if (title) title.remove();
            return clone.textContent.trim();
        }"""
    )
    extra["description"] = description or None

    comm_items = await page.evaluate(
        """() => Array.from(document.querySelectorAll('.offer-view-communication-item'))
            .map(el => el.textContent.replace(/\\s+/g, ' ').trim())"""
    )
    features: list[str] = list(comm_items)

    amenity_items = await page.evaluate(
        """() => {
            const section = document.querySelector('.offer-view-p-furniture');
            if (!section) return [];
            return Array.from(section.querySelectorAll('li')).map(li => {
                const clone = li.cloneNode(true);
                clone.querySelectorAll('svg').forEach(el => el.remove());
                return clone.textContent.trim();
            }).filter(Boolean);
        }"""
    )
    features.extend(amenity_items)
    extra["features"] = features

    date_rows = await page.evaluate(
        """() => Array.from(document.querySelectorAll('.offer-view-details-row'))
            .map(el => el.textContent.trim())"""
    )
    date_texts = [row for row in date_rows if _DATE_ROW_RE.match(row)]
    if len(date_texts) >= 2:
        extra["found_at"] = _parse_relative_date(date_texts[0])  # "оновлено" — найсвіжіше
        extra["published_at"] = _parse_relative_date(date_texts[1])  # старіше — ближче до реальної публікації
    elif date_texts:
        extra["published_at"] = _parse_relative_date(date_texts[0])

    return extra


def _normalize(card: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any] | None:
    external_id = card.get("external_id")
    url = card.get("url")
    if not external_id or not url:
        return None

    props = _parse_property_rows(card.get("rows", []))
    price_usd, price_uah = _parse_price(card.get("price_text") or "")

    address_parts = [p for p in [card.get("address"), card.get("region")] if p]
    features = list(dict.fromkeys((card.get("chips") or []) + (extra.get("features") or [])))
    photos = [_upscale_photo(p) for p in (card.get("photos") or [])]

    return asdict(
        Listing(
            source=SOURCE_NAME,
            url=url,
            external_id=str(external_id),
            origin_site=SOURCE_NAME,
            published_at=extra.get("published_at"),
            found_at=extra.get("found_at"),
            title=card.get("address"),
            description=extra.get("description"),
            price_usd=price_usd,
            price_uah=price_uah,
            area=props["area"],
            area_living=props["area_living"],
            area_kitchen=props["area_kitchen"],
            land_area_sotka=None,
            rooms=props["rooms"],
            floor=props["floor"],
            floor_total=props["floor_total"],
            address=", ".join(address_parts) if address_parts else None,
            lat=card.get("lat"),
            lon=card.get("lon"),
            photos=photos,
            contact=None,  # телефон прихований за кнопкою "Показати телефон"
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
                    await page.wait_for_selector(".catalog-card", state="attached", timeout=20000)
                except Exception:
                    logger.warning("rieltor.ua: не вдалося завантажити сторінку пошуку %s", target)
                    break

                page_cards = await _extract_search_page_cards(page)
                new_cards = [c for c in page_cards if c.get("external_id") and c["external_id"] not in cards_by_id]
                if not new_cards:
                    break
                for c in new_cards:
                    cards_by_id[c["external_id"]] = c
                if len(cards_by_id) >= MAX_LISTINGS_PER_RUN:
                    break

            logger.info("rieltor.ua: знайдено %d карток у видачі", len(cards_by_id))

            for external_id, card in list(cards_by_id.items())[:MAX_LISTINGS_PER_RUN]:
                try:
                    extra = await _extract_detail_extra(page, card["url"])
                    listing = _normalize(card, extra)
                    if listing:
                        listings[listing["external_id"]] = listing
                except Exception:
                    logger.exception("rieltor.ua: не вдалося обробити %s", card.get("url"))
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("rieltor_ua_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
