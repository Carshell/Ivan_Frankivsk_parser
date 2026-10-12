"""Головний вхідний файл: піднімає Telegram-бота і цикл парсингу джерел.

Сам код парсингу/бота лежить у модулях (web_pages/*.py, telegam/*.py,
instagram/parser.py) — цей файл зв'язує їх докупи:
  1. читає web_pages/sites.json, Telegram-канали й Instagram, збирає оголошення;
  2. нові оголошення: відсів продажу -> дедуплікація -> витяг полів через Claude
     (район, тварини, меблі…) -> збереження в базу (rentdb); відомі — оновлюють
     "востаннє бачили", і відстежується зниження ціни;
  3. підписникам зі збереженими фільтрами шле сповіщення про НОВІ об'єкти;
  4. піднімає polling Telegram-бота (діалог пошуку на кнопках, telegam/bot.py).

Запуск (Linux/Windows, з папки project/):
    pip install -r requirements.txt
    playwright install --with-deps chromium
    # покласти токен бота в .env (див. .env.example)
    python main.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
from pathlib import Path
from typing import Any

from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

import claude_extract
import currency
import dedup
import hard_filters
import pipeline_log
from photos import hires_all
import rentdb
from instagram import parser as instagram_parser
from telegam import channels as telegram_channels
from telegam import notify
from telegam.bot import router, setup_commands

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SITES_CONFIG = BASE_DIR / "web_pages" / "sites.json"
PARSE_INTERVAL_SECONDS = 30 * 60  # класифайди — раз на 30 хв

# Скільки Playwright-браузерів (сайтів) парсити одночасно, а не по черзі.
# Забагато одночасних Chromium — ризик вичерпати RAM сервера. Підбирай під
# реальні ресурси сервера через .env, без правки коду.
MAX_CONCURRENT_SITE_PARSERS = int(os.environ.get("MAX_CONCURRENT_SITE_PARSERS", "3"))

# Скільки оголошень обробляти Claude одночасно (а не по одному).
CLAUDE_CONCURRENCY = int(os.environ.get("CLAUDE_CONCURRENCY", "5"))
# Скільки раніше збережених без витягу оголошень дозаповнювати за один цикл.
RETRY_EXTRACTION_PER_CYCLE = 100


def _load_sites() -> list[dict]:
    return json.loads(SITES_CONFIG.read_text(encoding="utf-8"))


async def _run_site_parser(site: dict, semaphore: asyncio.Semaphore) -> tuple[list[dict], dict]:
    """Парсить одне джерело з web_pages/sites.json під семафором (обмежує
    кількість одночасно відкритих Playwright-браузерів).

    Кожне оголошення тегується city/property_type ІЗ КОНФІГА джерела (а не
    з тексту) — це надійно, бо кожен під-пошук у sites.json вже фільтрує по
    URL/query саме те місто й тип, на відміну від вільного тексту
    Telegram-каналів чи Instagram (там тип визначається евристикою, див.
    hard_filters.detect_property_type, а місто завжди ivano-frankivsk).

    Виняток — джерела зі змішаним типом на одній сторінці (orenda_if_ua: і
    квартири, і будинки разом): їхній property_type у sites.json навмисно
    null, а сам парсер виставляє тип на кожне оголошення індивідуально
    (надійніший сигнал від самого сайту) — тут це НЕ перезаписується."""
    module_name = Path(site["module"]).stem
    async with semaphore:
        try:
            module = importlib.import_module(f"web_pages.{module_name}")
            listings = await module.parse(search_url=site["search_url"])
            for listing in listings:
                listing["city"] = site.get("city")
                if site.get("property_type"):
                    listing["property_type"] = site["property_type"]
            logger.info("%s: знайдено %d оголошень", site["name"], len(listings))
            pipeline_log.source_result(site["name"], found=len(listings))
            return listings, {"name": site["name"], "found": len(listings), "error": None}
        except Exception as exc:
            logger.exception("Парсер %s впав, пропускаю цей цикл", site["name"])
            pipeline_log.source_result(site["name"], error=str(exc))
            return [], {"name": site["name"], "found": 0, "error": str(exc)}


async def run_all_parsers() -> tuple[list[dict], list[dict]]:
    """Викликає parse() кожного увімкненого джерела з web_pages/sites.json.

    Сайти парсяться ПАРАЛЕЛЬНО (до MAX_CONCURRENT_SITE_PARSERS одночасно) —
    послідовний прохід через 19+ джерел ризикував не вкластись у
    PARSE_INTERVAL_SECONDS. Telegram-канали і Instagram лишаються
    послідовними після сайтів (інша технологія — Telethon/instagrapi, не
    Playwright, і instagrapi під капотом блокуючий, тож паралелити його з
    рештою без окремого потоку немає сенсу).

    Падіння одного парсера не зупиняє інші — просто логується і пропускається.
    Повертає (усі оголошення, статистика по кожному джерелу) — друге потрібне
    для звіту адміну (хто впав, скільки знайдено), не тільки для логу.
    """
    all_listings: list[dict] = []
    source_stats: list[dict] = []

    sites = [site for site in _load_sites() if site.get("enabled") and site.get("module")]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_SITE_PARSERS)
    results = await asyncio.gather(*(_run_site_parser(site, semaphore) for site in sites))
    for listings, stat in results:
        source_stats.append(stat)
        all_listings.extend(listings)

    try:
        tg_listings = await telegram_channels.parse()
        # city вже виставлено в channels.py (кожен канал у channels.json має
        # своє місто); тип нерухомості тут вільний текст — тегуємо евристикою,
        # а якщо вона не впоралась (None), тип визначить Claude при витягу полів.
        for listing in tg_listings:
            listing["property_type"] = hard_filters.detect_property_type(listing)
        logger.info("telegram-канали: знайдено %d оголошень", len(tg_listings))
        pipeline_log.source_result("telegram", found=len(tg_listings))
        source_stats.append({"name": "telegram", "found": len(tg_listings), "error": None})
        all_listings.extend(tg_listings)
    except Exception as exc:
        logger.exception("Парсер telegram-каналів впав, пропускаю цей цикл")
        pipeline_log.source_result("telegram", error=str(exc))
        source_stats.append({"name": "telegram", "found": 0, "error": str(exc)})

    try:
        ig_listings = await instagram_parser.parse()
        # city вже виставлено в instagram/parser.py (кожен акаунт і кожен
        # хештег у accounts.json/hashtags.json має своє місто).
        for listing in ig_listings:
            listing["property_type"] = hard_filters.detect_property_type(listing)
        logger.info("instagram: знайдено %d оголошень", len(ig_listings))
        pipeline_log.source_result("instagram", found=len(ig_listings))
        source_stats.append({"name": "instagram", "found": len(ig_listings), "error": None})
        all_listings.extend(ig_listings)
    except Exception as exc:
        logger.exception("Парсер instagram впав, пропускаю цей цикл")
        pipeline_log.source_result("instagram", error=str(exc))
        source_stats.append({"name": "instagram", "found": 0, "error": str(exc)})

    return all_listings, source_stats


def _normalize_prices(listing: dict[str, Any]) -> None:
    """Гарантує обидві ціни (UAH і USD) за курсом НБУ — фільтр бюджету в ТЗ у
    грн/міс, а частина джерел (Telegram, dom.ria) дає лише долари."""
    uah, usd = listing.get("price_uah"), listing.get("price_usd")
    if uah is None and usd is not None:
        listing["price_uah"] = currency.usd_to_uah(usd)
        listing["price_currency"] = "USD"
    elif usd is None and uah is not None:
        listing["price_usd"] = currency.uah_to_usd(uah)


async def _extract_many(listings: list[dict]) -> list[dict | None]:
    # Виклик Claude займає секунди; послідовно сотні оголошень (особливо на
    # першому запуску) оброблялись би годинами. Паралелимо з обмеженням
    # (CLAUDE_CONCURRENCY), щоб не впертись у rate limit API.
    semaphore = asyncio.Semaphore(CLAUDE_CONCURRENCY)

    async def _one(listing: dict) -> dict | None:
        async with semaphore:
            return await claude_extract.extract(listing)

    return list(await asyncio.gather(*(_one(listing) for listing in listings)))


async def ingest(listings: list[dict]) -> tuple[dict[str, int], list[int]]:
    """Обробляє результат парсингу: оновлює відомі оголошення, нові проганяє
    через відсів продажу, дедуплікацію і витяг полів та зберігає в базу.

    Повертає (статистика, id щойно доданих оголошень)."""
    stats = {
        "parsed": len(listings), "known": 0, "new": 0, "sale_rejected": 0, "duplicates": 0,
        "extracted": 0, "extract_failed": 0, "not_rental": 0, "inserted": 0,
        "price_drops": 0, "retried": 0,
    }
    for listing in listings:
        _normalize_prices(listing)
        listing["photos"] = hires_all(listing.get("photos"))

    known = rentdb.existing_external_ids([item["external_id"] for item in listings])
    existing = [item for item in listings if item["external_id"] in known]
    new_all = [item for item in listings if item["external_id"] not in known]
    stats["known"], stats["new"] = len(existing), len(new_all)

    touched = rentdb.touch_existing(existing)
    stats["price_drops"] = touched["dropped"]

    # Завжди: замовника цікавить лише оренда — продаж відсіюється ще до Claude.
    sale = [item for item in new_all if hard_filters.is_sale_listing(item)]
    if sale:
        rentdb.reject_many([item["external_id"] for item in sale], "sale")
    stats["sale_rejected"] = len(sale)
    sale_ids = {item["external_id"] for item in sale}
    candidates = [item for item in new_all if item["external_id"] not in sale_ids]

    unique, stats["duplicates"] = dedup.filter_duplicates(candidates, rentdb.active_listings_for_dedup())
    pipeline_log.duplicates_result(stats["duplicates"], len(candidates))

    extracted = await _extract_many(unique)

    new_ids: list[int] = []
    for listing, ex in zip(unique, extracted):
        if ex is None:
            stats["extract_failed"] += 1
        else:
            stats["extracted"] += 1
            if not ex["is_rental"]:
                rentdb.reject(listing["external_id"], "not_rental")
                stats["not_rental"] += 1
                continue
        if not listing.get("property_type") and not (ex and ex.get("property_type")):
            listing["property_type"] = "apartment"  # тип не визначився ні евристикою, ні Claude
        listing_id = rentdb.insert_listing(listing, ex)
        if listing_id is not None:
            new_ids.append(listing_id)
    stats["inserted"] = len(new_ids)

    # Оголошення, збережені без витягу (Claude був недоступний), — дозаповнюємо.
    pending = rentdb.listings_needing_extraction(RETRY_EXTRACTION_PER_CYCLE)
    if pending:
        retried = await _extract_many(pending)
        for row, ex in zip(pending, retried):
            if ex is not None:
                rentdb.apply_extraction(row["id"], ex)
                stats["retried"] += 1

    pipeline_log.ingest_summary(stats)
    return stats, new_ids


async def _notify_admin(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        logger.exception("Не вдалося надіслати звіт адміну (chat_id=%s)", chat_id)


def _build_admin_report(source_stats: list[dict], stats: dict[str, int], notified: int) -> str:
    errors = [s for s in source_stats if s["error"]]
    lines = ["🔴 Були помилки в циклі!" if errors else "📊 Цикл парсингу завершено"]
    for s in source_stats:
        if s["error"]:
            lines.append(f"⚠️ {s['name']}: ПОМИЛКА — {s['error']}")
        else:
            lines.append(f"{s['name']}: знайдено {s['found']}")

    db_stats = rentdb.stats()
    lines.append(
        f"\n🆕 Нових: {stats['new']} (відомих: {stats['known']})\n"
        f"🚫 Продаж/не оренда: {stats['sale_rejected'] + stats['not_rental']}\n"
        f"🔁 Дублікатів: {stats['duplicates']}\n"
        f"🤖 Витяг полів Claude: {stats['extracted']}, недоступно: {stats['extract_failed']}, "
        f"дозаповнено раніше збережених: {stats['retried']}\n"
        f"💾 Додано в базу: {stats['inserted']}, знижено ціну: {stats['price_drops']}\n"
        f"🔔 Надіслано сповіщень: {notified}\n"
        f"📚 Усього в базі: {db_stats['total']}"
    )
    return "\n".join(lines)


def _delete_local_files(paths: list[str]) -> None:
    for path in paths:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Не вдалося видалити файл %s", path)


async def parser_loop(bot: Bot) -> None:
    """Фоновий цикл: раз на PARSE_INTERVAL_SECONDS парсить джерела, поповнює
    базу і сповіщає підписників зі збереженими фільтрами про нові об'єкти."""
    admin_chat_id_raw = os.environ.get("ADMIN_CHAT_ID")
    admin_chat_id = int(admin_chat_id_raw) if admin_chat_id_raw else None
    if not admin_chat_id:
        logger.warning("ADMIN_CHAT_ID не задано в .env — звіти про цикл нікуди не надсилатимуться")

    while True:
        try:
            pipeline_log.cycle_start()
            is_bootstrap_cycle = not rentdb.is_bootstrapped()
            if is_bootstrap_cycle:
                logger.info(
                    "Перший запуск: формуємо базову лінію (усе поточне зберігаємо в базу, "
                    "але сповіщень про це не шлемо)"
                )

            listings, source_stats = await run_all_parsers()
            stats, new_ids = await ingest(listings)

            notified = 0
            if is_bootstrap_cycle:
                rentdb.mark_bootstrapped()
            elif new_ids:
                notified = await notify.notify_new_listings(bot, new_ids)

            _delete_local_files(rentdb.prune_old())
            pipeline_log.cycle_result(sent=notified)

            if admin_chat_id:
                report = _build_admin_report(source_stats, stats, notified)
                if is_bootstrap_cycle:
                    report = (
                        f"🚀 Перший запуск — базова лінія сформована ({stats['inserted']} оголошень "
                        "збережено, сповіщення про них не надсилались)\n\n" + report
                    )
                await _notify_admin(bot, admin_chat_id, report)
        except Exception as exc:
            logger.exception("Помилка в циклі парсингу")
            pipeline_log.cycle_error(exc)
            if admin_chat_id:
                await _notify_admin(bot, admin_chat_id, f"🔴 Цикл парсингу впав повністю: {exc}")

        await asyncio.sleep(PARSE_INTERVAL_SECONDS)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(BASE_DIR / ".env")

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Не задано TELEGRAM_BOT_TOKEN — перевір файл project/.env")

    rentdb.connect()
    migrated = rentdb.migrate_legacy_subscribers()
    if migrated:
        logger.info("Перенесено %d підписників зі старого subscribers.json у нову базу", migrated)

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)
    await setup_commands(bot)

    asyncio.create_task(parser_loop(bot))

    logger.info("Бот запущений, чекаю на повідомлення...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
