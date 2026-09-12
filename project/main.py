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

from instagram import parser as instagram_parser
from telegam import channels as telegram_channels
from telegam import storage
from telegam.bot import cleanup_local_photos, router, send_listing

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
SITES_CONFIG = BASE_DIR / "web_pages" / "sites.json"
PARSE_INTERVAL_SECONDS = 30 * 60  # класифайди — раз на 30 хв (ТЗ, п.7)


def _load_sites() -> list[dict]:
    return json.loads(SITES_CONFIG.read_text(encoding="utf-8"))


async def run_all_parsers() -> list[dict]:
    """Викликає parse() кожного увімкненого джерела з web_pages/sites.json.

    Падіння одного парсера не зупиняє інші — просто логується і пропускається.
    """
    all_listings: list[dict] = []
    for site in _load_sites():
        if not site.get("enabled") or not site.get("module"):
            continue
        module_name = Path(site["module"]).stem
        try:
            module = importlib.import_module(f"web_pages.{module_name}")
            listings = await module.parse(search_url=site["search_url"])
            logger.info("%s: знайдено %d оголошень", site["name"], len(listings))
            all_listings.extend(listings)
        except Exception:
            logger.exception("Парсер %s впав, пропускаю цей цикл", site["name"])

    try:
        tg_listings = await telegram_channels.parse()
        logger.info("telegram-канали: знайдено %d оголошень", len(tg_listings))
        all_listings.extend(tg_listings)
    except Exception:
        logger.exception("Парсер telegram-каналів впав, пропускаю цей цикл")

    try:
        ig_listings = await instagram_parser.parse()
        logger.info("instagram: знайдено %d оголошень", len(ig_listings))
        all_listings.extend(ig_listings)
    except Exception:
        logger.exception("Парсер instagram впав, пропускаю цей цикл")

    return all_listings


async def parser_loop(bot: Bot) -> None:
    """Фоновий цикл: раз на PARSE_INTERVAL_SECONDS парсить джерела і шле нові оголошення."""
    while True:
        try:
            listings = await run_all_parsers()
            new_listings = [item for item in listings if not storage.is_seen(item["external_id"])]

            if new_listings:
                logger.info("Нових оголошень цього циклу: %d", len(new_listings))

            for listing in new_listings:
                for chat_id in storage.active_subscriber_ids():
                    await send_listing(bot, chat_id, listing)
                cleanup_local_photos(listing)

            storage.mark_seen_bulk([item["external_id"] for item in new_listings])
        except Exception:
            logger.exception("Помилка в циклі парсингу")

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
