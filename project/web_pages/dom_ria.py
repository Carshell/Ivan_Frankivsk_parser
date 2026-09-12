"""Парсер dom.ria.com (оренда будинків) через Playwright.

На відміну від lun.ua тут не потрібен DOM-скрейпінг взагалі: сторінка пошуку
сама звертається до внутрішнього JSON API (перехоплюємо мережеву відповідь),
а по кожному ID оголошення є окремий ендпоінт з повністю структурованими
даними — /realty/data/{id}. Це набагато стабільніше за хешовані CSS-класи.

Запуск (production — Linux):
    playwright install --with-deps chromium
    python -m project.web_pages.dom_ria
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from playwright.async_api import Page, async_playwright

logger = logging.getLogger(__name__)

SOURCE_NAME = "dom.ria"
BASE_URL = "https://dom.ria.com"
DEFAULT_SEARCH_URL = (
    "https://dom.ria.com/uk/search?excludeSold=1&category=4&realty_type=0&operation=3"
    "&state_id=15&in_radius=30&price_cur=1&wo_dupl=1&sort=inspected_sort"
    "&firstIteraction=false&city_ids=15&client=searchV2&limit=20&type=list&ch=246_244"
)

SEARCH_API_MARKER = "/node/searchEngine/v2/"
PHOTO_CDN = "https://cdn.riastatic.com/photos"

MAX_PAGES = 10


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
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _photo_url(file_path: str) -> str:
    stem = re.sub(r"\.\w+$", "", file_path)
    return f"{PHOTO_CDN}/{stem}xl.webp"


def _parse_price_arr(data: dict[str, Any], key: str) -> float | None:
    """priceArr завжди містить перерахунок в усі валюти ("1"=usd, "2"=eur, "3"=uah),
    незалежно від того, в якій валюті виставлена сама ціна (currency_type)."""
    raw = (data.get("priceArr") or {}).get(key)
    if raw is None:
        return None
    try:
        return float(str(raw).replace(" ", "").replace("\xa0", ""))
    except ValueError:
        return None


def _normalize(data: dict[str, Any]) -> dict[str, Any] | None:
    realty_id = data.get("realty_id")
    if not realty_id or data.get("deleted_by"):
        return None  # оголошення знято (позначене cron-видаленням на боці сайту)

    beautiful_url = data.get("beautiful_url")
    url = f"{BASE_URL}/uk/{beautiful_url}" if beautiful_url else f"{BASE_URL}/realty/{realty_id}"

    photos = [
        _photo_url(photo["file"])
        for photo in sorted((data.get("photos") or {}).values(), key=lambda p: p.get("ordering", 0))
        if photo.get("file")
    ]

    address_parts = [
        part.strip()
        for part in (data.get("street_name_uk"), data.get("city_name_uk"), data.get("state_name_uk"))
        if part and part.strip()
    ]

    features = list(data.get("secondaryUtp") or [])
    if data.get("withAnimal"):
        features.append("можна з твариною")

    published_at = None
    pub_ts = data.get("publishing_date_ts")
    if pub_ts:
        published_at = datetime.fromtimestamp(pub_ts, tz=timezone.utc).isoformat()

    return asdict(
        Listing(
            source=SOURCE_NAME,
            url=url,
            external_id=str(realty_id),
            origin_site=SOURCE_NAME,
            published_at=published_at,
            found_at=None,
            title=address_parts[0] if address_parts else None,
            description=(data.get("description_uk") or data.get("description") or None),
            price_usd=_parse_price_arr(data, "1"),
            price_uah=_parse_price_arr(data, "3"),
            area=data.get("total_square_meters"),
            area_living=None,
            area_kitchen=None,
            land_area_sotka=data.get("ares_count"),
            rooms=data.get("rooms_count"),
            floor=data.get("floor"),
            floor_total=data.get("floors_count"),
            address=", ".join(address_parts) if address_parts else None,
            lat=data.get("latitude"),
            lon=data.get("longitude"),
            photos=photos,
            contact=None,
            features=features,
            raw_snapshot=data,
        )
    )


async def _capture_first_search_response(page: Page, search_url: str, timeout_s: float = 20.0) -> tuple[dict, str] | None:
    """Відкриває сторінку пошуку і перехоплює JSON-відповідь її власного API-запиту."""
    captured: dict[str, Any] = {}

    async def _on_response(response) -> None:
        if "data" in captured or SEARCH_API_MARKER not in response.url:
            return
        if response.status != 200:
            return
        try:
            body = await response.json()
        except Exception:
            return
        captured["data"] = body
        captured["url"] = response.url

    page.on("response", _on_response)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        waited = 0.0
        while "data" not in captured and waited < timeout_s:
            await page.wait_for_timeout(500)
            waited += 0.5
    finally:
        page.remove_listener("response", _on_response)

    if "data" not in captured:
        return None
    return captured["data"], captured["url"]


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

            captured = await _capture_first_search_response(page, search_url)
            if captured is None:
                logger.warning("dom.ria: не вдалося перехопити відповідь API пошуку (%s)", SEARCH_API_MARKER)
                return []

            search_data, search_api_url = captured
            all_ids: list[int] = list(search_data.get("items") or [])
            total_count = search_data.get("count", len(all_ids))

            page_num = 1
            while len(all_ids) < total_count and page_num < MAX_PAGES:
                next_url = _with_page_param(search_api_url, page_num)
                response = await page.request.get(next_url)
                if not response.ok:
                    break
                body = await response.json()
                new_ids = body.get("items") or []
                if not new_ids:
                    break
                all_ids.extend(new_ids)
                page_num += 1

            logger.info("dom.ria: у видачі %d оголошень (заявлено count=%s)", len(all_ids), total_count)

            for realty_id in all_ids:
                try:
                    response = await page.request.get(f"{BASE_URL}/realty/data/{realty_id}?lang_id=4")
                    if not response.ok:
                        logger.warning("dom.ria: %s відповів %s для realty_id=%s", "/realty/data/", response.status, realty_id)
                        continue
                    data = await response.json()
                    listing = _normalize(data)
                    if listing:
                        listings[listing["external_id"]] = listing
                except Exception:
                    logger.exception("dom.ria: не вдалося обробити realty_id=%s", realty_id)
        finally:
            await browser.close()

    return list(listings.values())


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    results = await parse()
    out_path = Path(__file__).with_name("dom_ria_output.json")
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Готово: %d оголошень збережено в %s", len(results), out_path)


if __name__ == "__main__":
    asyncio.run(_main())
