"""Парсер OLX (оренда будинків) через Playwright.

Список оголошень на сторінці пошуку беремо з JSON-LD (Product.offers.offers[])
— він уже дає дедуплікований список посилань. Але сама сторінка пошуку не має
опису/кімнат/площі/дати публікації — ці дані є лише на сторінці кожного
оголошення (частково в JSON-LD, частково в блоці характеристик
[data-testid="ad-parameters-container"], простий текст "Мітка: значення").

OLX не публікує координати чи точну адресу в жодному відкритому форматі —
лише місто/область. Це обмеження самого сайту (приватність), не хиба парсера:
lat/lon тут завжди None.

Ціна на сторінках завжди в гривні — конвертація в USD через курс НБУ (currency.py).

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.olx
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import Page, async_playwright

import currency

logger = logging.getLogger(__name__)

SOURCE_NAME = "olx"
BASE_URL = "https://www.olx.ua"
DEFAULT_SEARCH_URL = "https://www.olx.ua/uk/nedvizhimost/doma/arenda-domov/ivano-frankovsk/"

MAX_PAGES = 10
MAX_LISTINGS_PER_RUN = 60  # кожне оголошення = окремий перехід на сторінку, обмежуємо тривалість циклу

MONTHS_UK = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
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


async def _extract_search_page_urls(page: Page) -> list[str]:
    scripts = await page.eval_on_selector_all(
        'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
    )
    for raw in scripts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "Product":
            offers = ((data.get("offers") or {}).get("offers")) or []
            return [o["url"] for o in offers if o.get("url")]
    return []


def _parse_published_date(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\s+([а-яїієґ']+)\s+(\d{4})", text.lower())
    if not m:
        return None
    day, month_name, year = m.groups()
    month = MONTHS_UK.get(month_name)
    if not month:
        return None
    try:
        return datetime(int(year), month, int(day)).isoformat()
    except ValueError:
        return None


def _parse_characteristics(pairs_text: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for text in pairs_text:
        if ":" in text:
            key, _, value = text.partition(":")
            result[key.strip()] = value.strip()
    return result


async def _find_product_ld(page: Page) -> dict[str, Any] | None:
    scripts = await page.eval_on_selector_all(
        'script[type="application/ld+json"]', "els => els.map(e => e.textContent)"
    )
    for raw in scripts:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "Product":
            return data
    return None


async def _extract_detail(page: Page, url: str) -> dict[str, Any] | None:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector('script[type="application/ld+json"]', state="attached", timeout=20000)
    except Exception:
        logger.warning("olx: не вдалося завантажити %s", url)
        return None

    product = await _find_product_ld(page)
    if not product or not product.get("sku"):
        return None

    param_texts = await page.eval_on_selector_all(
        '[data-testid="ad-parameters-container"] p', "els => els.map(e => e.textContent)"
    )
    chars = _parse_characteristics(param_texts)

    posted_text = await page.eval_on_selector(
        '[data-testid="ad-posted-at"]', "el => el ? el.textContent : null"
    )
    published_at = _parse_published_date(posted_text) if posted_text else None

    area_m = re.search(r"([\d.]+)", chars.get("Загальна площа", ""))
    rooms_m = re.search(r"(\d+)", chars.get("Кількість кімнат", ""))
    land_m = re.search(r"([\d.]+)", chars.get("Площа ділянки", ""))
    floors_m = re.search(r"(\d+)", chars.get("Поверховість", ""))

    features: list[str] = []
    comfort = chars.get("Комфорт")
    if comfort:
        features.extend(p.strip() for p in comfort.split(",") if p.strip())
    blackout = chars.get("Автономність при блекауті")
    if blackout and blackout.strip().lower() not in ("немає", "ні"):
        features.append(f"автономність: {blackout}")
    pets = chars.get("Домашні улюбленці", "")
    if pets and not pets.lower().startswith("ні"):
        features.append("можна з тваринами")

    offers = product.get("offers") or {}
    price_uah = offers.get("price")
    price_usd = currency.uah_to_usd(price_uah) if price_uah else None

    city = ((offers.get("areaServed") or {}).get("name"))

    return asdict(
        Listing(
            source=SOURCE_NAME,
            url=url,
            external_id=str(product["sku"]),
            origin_site=SOURCE_NAME,
            published_at=published_at,
            found_at=None,
            title=product.get("name"),
            description=product.get("description") or None,
            price_usd=price_usd,
            price_uah=float(price_uah) if price_uah else None,
            area=float(area_m.group(1)) if area_m else None,
            area_living=None,
            area_kitchen=None,
            land_area_sotka=float(land_m.group(1)) if land_m else None,
            rooms=int(rooms_m.group(1)) if rooms_m else None,
            floor=None,
            floor_total=int(floors_m.group(1)) if floors_m else None,
            address=city or None,
            lat=None,  # OLX не публікує точні координати/адресу
            lon=None,
            photos=(product.get("image") or [])[:5],
            contact=None,  # телефон прихований за кнопкою "показати" — потребує окремого кліку
            features=features,
            raw_snapshot={"product": product, "characteristics": chars},
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

            detail_urls: list[str] = []
            for page_num in range(1, max_pages + 1):
                target = search_url if page_num == 1 else _with_page_param(search_url, page_num)
                try:
                    await page.goto(target, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_selector('script[type="application/ld+json"]', state="attached", timeout=20000)
                except Exception:
                    logger.warning("olx: не вдалося завантажити сторінку пошуку %s", target)
                    break

                page_urls = await _extract_search_page_urls(page)
                if not page_urls:
                    break
                new_urls = [u for u in page_urls if u not in detail_urls]
                if not new_urls:
                    break  # пагінація зациклилась/закінчилась
                detail_urls.extend(new_urls)
                if len(detail_urls) >= MAX_LISTINGS_PER_RUN:
                    break

            detail_urls = detail_urls[:MAX_LISTINGS_PER_RUN]
            logger.info("olx: знайдено %d посилань на оголошення", len(detail_urls))

            for url in detail_urls:
                try:
                    listing = await _extract_detail(page, url)
                    if listing:
                        listings[listing["external_id"]] = listing
                except Exception:
                    logger.exception("olx: не вдалося обробити %s", url)
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("olx_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
