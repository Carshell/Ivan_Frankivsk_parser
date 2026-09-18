"""Парсер orenda.if.ua (агентство RITM.home, Івано-Франківськ) через Playwright.

Локальний сайт одного агентства (не агрегатор) — власна верстка, без JSON-LD.
Список (/all-properties/, сторінки через ?page=N) уже містить оголошення з
УСІХ районів одразу впереміш (Центр, Пасічна, Передмістя, Чорновола-Бандери,
Набережна-Княгинин, Позитрон-Каскад, Івасюка-Надрічна, Район міського озера,
Опришівці тощо) — окремого проходу по кожному району не треба, "Район"
береться прямо з картки й пишеться в адресу.

Картка списку дає лише title/кімнати/район/адресу/поверх/ціну/статус — площа,
опис, повний список зручностей, фотогалерея і внутрішній ID оголошення є лише
на сторінці самого оголошення, тому (як і в web_pages/olx.py) спершу збираємо
посилання зі списку, а тоді відкриваємо кожне окремо.

Тип нерухомості визначається НАДІЙНО (без евристики по тексту) — сайт сам
прямо пише "Кімнат: будинок" замість числа кімнат для будинків. Через це
property_type виставляється тут-таки в JSON (не через web_pages/sites.json,
як для інших джерел, — там один сайт = один тип; тут кожне оголошення різне).
main.py при заповненні city/property_type з sites.json не перезаписує
property_type, якщо парсер уже його виставив.

Ціна буває і в $, і в ₴ — конвертація через курс НБУ (currency.py). Оголошення
зі статусом "Заброньована" (уже здане) пропускаються — вони не про доступну
оренду.

Координат сайт не публікує (лише посилання на Google Maps за адресою, без
самих координат у розмітці) — lat/lon завжди None.

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.orenda_if_ua
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import Page, async_playwright

import currency

logger = logging.getLogger(__name__)

SOURCE_NAME = "orenda.if.ua"
BASE_URL = "https://www.orenda.if.ua"
DEFAULT_SEARCH_URL = "https://www.orenda.if.ua/all-properties?sort=newest&currency=uah"

MAX_PAGES = 10
MAX_LISTINGS_PER_RUN = 80  # кожне оголошення = окремий перехід на сторінку, обмежуємо тривалість циклу

CARD_SELECTOR = ".cards-content__card"
BOOKED_STATUS_MARKERS = ("заброньован",)  # вже здане — не про доступну оренду


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


def _parse_price(text: str) -> tuple[float | None, float | None]:
    """"900 $" -> (900.0, None); "20000 ₴" -> (None, 20000.0) -> USD дораховується викликаючою стороною."""
    if not text:
        return None, None
    cleaned = text.replace("\xa0", " ").strip()
    m = re.search(r"([\d\s]+(?:[.,]\d+)?)", cleaned)
    if not m:
        return None, None
    try:
        value = float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None, None
    if "$" in cleaned:
        return value, None
    return None, value  # ₴/грн — інша валюта в тексті не зустрічається


async def _extract_list_page(page: Page) -> list[str]:
    """Посилання на кожне оголошення з поточної сторінки списку."""
    hrefs = await page.eval_on_selector_all(
        f"{CARD_SELECTOR} .card__title a",
        "els => els.map(e => e.getAttribute('href'))",
    )
    return [BASE_URL + h if h and h.startswith("/") else h for h in hrefs if h]


async def _extract_detail(page: Page, url: str) -> dict[str, Any] | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(".card-single__options", state="attached", timeout=20000)
    except Exception:
        logger.warning("orenda.if.ua: не вдалося завантажити %s", url)
        return None

    data = await page.evaluate(
        """
        () => {
            const text = (sel) => { const el = document.querySelector(sel); return el ? el.textContent.trim() : null; };
            const opts = document.querySelector('.card-single__options');
            const optsText = opts ? opts.textContent.replace(/\\s+/g, ' ').trim() : '';

            const idxEl = document.querySelector('.card-single__index');
            const statusEl = document.querySelector('[class*="card-single__status"]') ||
                              document.querySelector('[class*="status"]');
            const tags = Array.from(document.querySelectorAll('.card-single__tags .card-single__tag'))
                .map(e => e.textContent.trim()).filter(Boolean);
            const photos = Array.from(document.querySelectorAll('.swiper-slide:not(.swiper-slide-duplicate) img'))
                .map(e => e.src).filter(Boolean);
            const phoneEl = document.querySelector('a[href^="tel:"]');

            return {
                index: idxEl ? idxEl.textContent.trim() : null,
                title: text('h1'),
                price: text('.card-single__price'),
                status: statusEl ? statusEl.textContent.trim() : null,
                optsText: optsText,
                description: text('.card-single__description'),
                tags: tags,
                photos: [...new Set(photos)],
                phone: phoneEl ? phoneEl.textContent.trim() : null,
            };
        }
        """
    )

    if not data.get("index"):
        logger.warning("orenda.if.ua: не знайдено внутрішній ID на %s — пропускаю", url)
        return None

    status = (data.get("status") or "").lower()
    if any(marker in status for marker in BOOKED_STATUS_MARKERS):
        return None  # вже здане — не показуємо як доступну оренду

    opts_text = data.get("optsText") or ""
    rooms_m = re.search(r"Кімнат:\s*([^\s].*?)(?:Район:|Адреса:|Поверх:|Площа:|$)", opts_text)
    district_m = re.search(r"Район:\s*([^\s].*?)(?:Адреса:|Поверх:|Площа:|$)", opts_text)
    address_m = re.search(r"Адреса:\s*([^\s].*?)(?:Поверх:|Площа:|$)", opts_text)
    floor_m = re.search(r"Поверх:\s*(\d+)\\(\d+)", opts_text)
    area_m = re.search(r"Площа:\s*([\d.,]+)\s*m", opts_text)

    rooms_raw = (rooms_m.group(1).strip() if rooms_m else "") or ""
    is_house = "будинок" in rooms_raw.lower()
    rooms_num_m = re.search(r"\d+", rooms_raw)

    district = district_m.group(1).strip() if district_m else None
    street = address_m.group(1).strip() if address_m else None
    address_parts = [p for p in (street, f"район «{district}»" if district else None, "Івано-Франківськ") if p]

    price_usd, price_uah = _parse_price(data.get("price") or "")
    if price_usd is None and price_uah is not None:
        price_usd = currency.uah_to_usd(price_uah)
    elif price_uah is None and price_usd is not None:
        price_uah = currency.usd_to_uah(price_usd)

    listing = asdict(
        Listing(
            source=SOURCE_NAME,
            url=url,
            external_id=str(data["index"]),
            origin_site=SOURCE_NAME,
            published_at=None,  # дата публікації ніде на сторінці не вказана
            found_at=None,
            title=data.get("title"),
            description=data.get("description"),
            price_usd=price_usd,
            price_uah=price_uah,
            area=float(area_m.group(1).replace(",", ".")) if area_m else None,
            area_living=None,
            area_kitchen=None,
            land_area_sotka=None,
            rooms=int(rooms_num_m.group()) if rooms_num_m else None,
            floor=int(floor_m.group(1)) if floor_m else None,
            floor_total=int(floor_m.group(2)) if floor_m else None,
            address=", ".join(address_parts) if address_parts else None,
            lat=None,
            lon=None,
            photos=data.get("photos") or [],
            contact=data.get("phone"),
            features=data.get("tags") or [],
            raw_snapshot=data,
        )
    )
    # Надійний сигнал від самого сайту ("Кімнат: будинок") — точніший за
    # текстову евристику hard_filters.detect_property_type(), тому виставляємо
    # тип нерухомості прямо тут (main.py не перезаписує, якщо вже є).
    listing["property_type"] = "house" if is_house else "apartment"
    return listing


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

            detail_urls: list[str] = []
            for page_num in range(1, max_pages + 1):
                target = search_url if page_num == 1 else _with_page_param(search_url, page_num)
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_selector(CARD_SELECTOR, state="attached", timeout=20000)
                except Exception:
                    if page_num == 1:
                        logger.warning("orenda.if.ua: не вдалося завантажити сторінку пошуку %s", target)
                    break  # сторінок більше нема (типовий кінець пагінації) — не помилка

                page_urls = await _extract_list_page(page)
                if not page_urls:
                    break
                new_urls = [u for u in page_urls if u not in detail_urls]
                if not new_urls:
                    break  # пагінація зациклилась/закінчилась
                detail_urls.extend(new_urls)
                if len(detail_urls) >= MAX_LISTINGS_PER_RUN:
                    break

            detail_urls = detail_urls[:MAX_LISTINGS_PER_RUN]
            logger.info("orenda.if.ua: знайдено %d посилань на оголошення", len(detail_urls))

            for url in detail_urls:
                try:
                    listing = await _extract_detail(page, url)
                    if listing:
                        listings[listing["external_id"]] = listing
                except Exception:
                    logger.exception("orenda.if.ua: не вдалося обробити %s", url)
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("orenda_if_ua_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
