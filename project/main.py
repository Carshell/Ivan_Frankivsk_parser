"""Головний вхідний файл: піднімає Telegram-бота і цикл парсингу джерел.

Сам код парсингу/бота лежить у модулях (web_pages/*.py, telegam/bot.py) —
цей файл лише викликає їхні функції та зв'язує їх докупи:
  1. читає web_pages/sites.json і викликає parse() кожного увімкненого парсера;
  2. нові (ще не надіслані) оголошення розсилає підписникам через telegam/bot.py;
  3. піднімає polling Telegram-бота (команди /start, /digest, /pause, /resume, /filters).

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

from aiogram import Bot, Dispatcher
from dotenv import load_dotenv

import claude_scoring
import dedup
import hard_filters
import pipeline_log
import security_reference
from instagram import parser as instagram_parser
from telegam import channels as telegram_channels
from telegam import storage
from telegam.bot import router, send_listing

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SITES_CONFIG = BASE_DIR / "web_pages" / "sites.json"
PARSE_INTERVAL_SECONDS = 30 * 60  # класифайди — раз на 30 хв (ТЗ, п.7)
SCORE_THRESHOLD = 50  # нижче — в архів, у Telegram не йде (ТЗ, п.7)

# Скільки Playwright-браузерів (сайтів) парсити одночасно, а не по черзі.
# З 19 джерелами послідовний прохід ризикував не вкластись у
# PARSE_INTERVAL_SECONDS; забагато одночасних Chromium — ризик вичерпати
# RAM сервера. Підбирай під реальні ресурси сервера через .env, без правки коду.
MAX_CONCURRENT_SITE_PARSERS = int(os.environ.get("MAX_CONCURRENT_SITE_PARSERS", "3"))

# Тимчасово (за проханням користувача): усе нове йде в бот без відсіву —
# Claude лише додає короткий аналіз/скоринг/безпеку до повідомлення, але
# нічого не відкидає. Поставити назад True, щоб повернути п.3/п.7 ТЗ.
ENABLE_HARD_FILTERS = False
ENABLE_SCORE_THRESHOLD = False


def _load_sites() -> list[dict]:
    return json.loads(SITES_CONFIG.read_text(encoding="utf-8"))


async def _run_site_parser(site: dict, semaphore: asyncio.Semaphore) -> tuple[list[dict], dict]:
    """Парсить одне джерело з web_pages/sites.json під семафором (обмежує
    кількість одночасно відкритих Playwright-браузерів)."""
    module_name = Path(site["module"]).stem
    async with semaphore:
        try:
            module = importlib.import_module(f"web_pages.{module_name}")
            listings = await module.parse(search_url=site["search_url"])
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
        logger.info("instagram: знайдено %d оголошень", len(ig_listings))
        pipeline_log.source_result("instagram", found=len(ig_listings))
        source_stats.append({"name": "instagram", "found": len(ig_listings), "error": None})
        all_listings.extend(ig_listings)
    except Exception as exc:
        logger.exception("Парсер instagram впав, пропускаю цей цикл")
        pipeline_log.source_result("instagram", error=str(exc))
        source_stats.append({"name": "instagram", "found": 0, "error": str(exc)})

    return all_listings, source_stats


async def score_new_listings(listings: list[dict]) -> tuple[list[dict], dict]:
    """Хард-фільтри (п.3) → довідник об'єктів безпеки (п.5) → скоринг Claude (п.9).

    Якщо Claude недоступний (немає ключа/збій API) — оголошення все одно йде
    далі (з claude=None), щоб мовчазна відмова API не глушила весь пайплайн;
    якщо ж Claude відповів, але score нижче порогу — це і є "в архів, у
    Telegram не йде" з п.7 ТЗ.

    Повертає (оголошення, що йдуть далі, статистика для логу/звіту адміну).
    """
    result = []
    hard_passed = 0
    sale_rejected = 0
    claude_unavailable = 0
    below_threshold = 0

    for listing in listings:
        # Завжди, незалежно від ENABLE_HARD_FILTERS — замовника продаж не
        # цікавить в принципі, це не той самий "тимчасово вимкнений" фільтр.
        if hard_filters.is_sale_listing(listing):
            sale_rejected += 1
            continue
        if ENABLE_HARD_FILTERS and not hard_filters.passes_hard_filters(listing):
            continue
        hard_passed += 1

        objects = security_reference.nearby_objects(listing.get("lat"), listing.get("lon"))
        claude_result = await claude_scoring.score_listing(listing, objects)
        listing["claude"] = claude_result

        if claude_result is None:
            claude_unavailable += 1
        elif ENABLE_SCORE_THRESHOLD:
            score = claude_result.get("score")
            if isinstance(score, (int, float)) and score < SCORE_THRESHOLD:
                below_threshold += 1
                continue

        result.append(listing)

    stats = {
        "considered": len(listings),
        "sale_rejected": sale_rejected,
        "hard_passed": hard_passed,
        "claude_scored": hard_passed - claude_unavailable,
        "claude_unavailable": claude_unavailable,
        "below_threshold": below_threshold,
    }
    if sale_rejected:
        pipeline_log.sale_filter_result(sale_rejected, len(listings))
    pipeline_log.hard_filter_result(hard_passed, len(listings))
    pipeline_log.claude_summary(
        scored=stats["claude_scored"],
        unavailable=claude_unavailable,
        below_threshold=below_threshold,
    )
    return result, stats


async def _notify_admin(bot: Bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        logger.exception("Не вдалося надіслати звіт адміну (chat_id=%s)", chat_id)


def _build_admin_report(
    source_stats: list[dict],
    new_counts: dict[str, int],
    total_new: int,
    duplicate_count: int,
    score_stats: dict,
    sent: int,
) -> str:
    errors = [s for s in source_stats if s["error"]]
    lines = ["🔴 Були помилки в циклі!" if errors else "📊 Цикл парсингу завершено"]

    for s in source_stats:
        if s["error"]:
            lines.append(f"⚠️ {s['name']}: ПОМИЛКА — {s['error']}")
        else:
            lines.append(f"{s['name']}: знайдено {s['found']}")

    if total_new:
        parts = ", ".join(f"{src}={n}" for src, n in new_counts.items())
        lines.append(f"\n🆕 Нових: {total_new} ({parts})")
        if duplicate_count:
            lines.append(f"🔁 Дублікатів (те саме джерело/об'єкт іншим сайтом): {duplicate_count}")
    else:
        lines.append("\n🆕 Нових оголошень немає")

    if score_stats.get("sale_rejected"):
        lines.append(f"🚫 Продаж (не оренда): {score_stats['sale_rejected']} відсіяно")

    lines.append(
        f"✅ Хард-фільтр: {score_stats['hard_passed']} із {score_stats['considered']} пройшли\n"
        f"🤖 Claude: оцінено {score_stats['claude_scored']}, "
        f"недоступно {score_stats['claude_unavailable']}, "
        f"нижче порогу {score_stats['below_threshold']}\n"
        f"📤 Надіслано підписникам: {sent}"
    )
    return "\n".join(lines)


async def parser_loop(bot: Bot) -> None:
    """Фоновий цикл: раз на PARSE_INTERVAL_SECONDS парсить джерела і шле нові оголошення."""
    admin_chat_id_raw = os.environ.get("ADMIN_CHAT_ID")
    admin_chat_id = int(admin_chat_id_raw) if admin_chat_id_raw else None
    if not admin_chat_id:
        logger.warning("ADMIN_CHAT_ID не задано в .env — звіти про цикл нікуди не надсилатимуться")

    while True:
        try:
            pipeline_log.cycle_start()
            is_bootstrap_cycle = not storage.is_bootstrapped()
            if is_bootstrap_cycle:
                logger.info(
                    "Перший запуск: формуємо базову лінію (усе поточне позначаємо "
                    "як 'вже бачене', без розсилки підписникам)"
                )

            listings, source_stats = await run_all_parsers()
            new_listings_all = [item for item in listings if not storage.is_seen(item["external_id"])]

            new_counts: dict[str, int] = {}
            for item in new_listings_all:
                new_counts[item["source"]] = new_counts.get(item["source"], 0) + 1
            pipeline_log.new_by_source(new_counts, len(new_listings_all))

            # Дедуплікація (п.7/п.8 ТЗ) — той самий будинок, викладений кількома
            # джерелами чи репостом, не повинен йти окремим повідомленням кожен
            # раз. Відсіяні дублікати все одно позначаються "вже баченими" нижче
            # (за new_listings_all), щоб не перевірялись на дедуп щоцикл заново.
            new_listings, duplicate_count = dedup.filter_duplicates(new_listings_all)
            pipeline_log.duplicates_result(duplicate_count, len(new_listings_all))

            if new_listings:
                logger.info("Нових оголошень цього циклу (після дедуплікації): %d", len(new_listings))

            to_send, score_stats = await score_new_listings(new_listings)
            if to_send:
                logger.info("Пройшли хард-фільтри і поріг скорингу: %d", len(to_send))
                storage.add_matched(to_send)

            # На першому запуску (bootstrap) навмисно НЕ розсилаємо — інакше кожен,
            # хто вже написав /start, отримав би "потоп" з усього, що на цей момент
            # просто вже існує на ринку. Далі, з наступного циклу, розсилка йде як
            # звичайно — тільки те, що справді щойно з'явилось.
            #
            # Фото (включно з локальними з Telegram-джерел) навмисно НЕ видаляються
            # після відправки — вони лишаються на диску, щоб /start новим підписникам
            # міг показати ці ж оголошення з фото пізніше. Видаляються лише коли
            # оголошення випадає зі storage.matched_listings.json за лімітом.
            if not is_bootstrap_cycle:
                for listing in to_send:
                    for chat_id in storage.active_subscriber_ids():
                        await send_listing(bot, chat_id, listing)
                        await asyncio.sleep(1.2)  # уникнути flood control Telegram при пачці оголошень

            storage.mark_seen_bulk([item["external_id"] for item in new_listings_all])
            if is_bootstrap_cycle:
                storage.mark_bootstrapped()
            pipeline_log.cycle_result(sent=0 if is_bootstrap_cycle else len(to_send))

            if admin_chat_id:
                sent_count = 0 if is_bootstrap_cycle else len(to_send)
                report = _build_admin_report(
                    source_stats, new_counts, len(new_listings_all), duplicate_count, score_stats, sent_count
                )
                if is_bootstrap_cycle:
                    report = (
                        f"🚀 Перший запуск — сформована базова лінія ({len(to_send)} оголошень "
                        "збережено, розсилку підписникам пропущено)\n\n" + report
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

    bot = Bot(token=token)
    dp = Dispatcher()
    dp.include_router(router)

    asyncio.create_task(parser_loop(bot))

    logger.info("Бот запущений, чекаю на повідомлення...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
