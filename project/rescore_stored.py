"""Одноразовий скрипт: дошуковує вже збережені оголошення (matched_listings.json)
без оцінки Claude (claude=None) і пересканує їх заново.

Такі записи трапляються, коли оголошення потрапило в базу під час збою Claude
API (немає ключа, обрізана відповідь, ліміт токенів тощо) — /start і /digest
завжди беруть дані зі storage як є, без повторного виклику Claude, тож без
цього скрипта такий запис назавжди лишався б з "Оцінка Claude недоступна".

Запуск (з папки project/, або docker compose exec bot python rescore_stored.py):
    python rescore_stored.py
"""

from __future__ import annotations

import asyncio
import logging

import claude_scoring
import security_reference
from telegam import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    stored = storage._load_json(storage.MATCHED_FILE, [])
    missing = [item for item in stored if item.get("claude") is None]

    if not missing:
        logger.info("Усі %d збережених оголошень вже мають оцінку Claude — нічого робити", len(stored))
        return

    logger.info("Оцінки Claude бракує у %d із %d збережених оголошень — пересканую", len(missing), len(stored))
    for listing in missing:
        objects = security_reference.nearby_objects(listing.get("lat"), listing.get("lon"))
        result = await claude_scoring.score_listing(listing, objects)
        listing["claude"] = result
        logger.info("%s: %s", listing.get("url"), "OK" if result else "все ще недоступно")

    storage._save_json(storage.MATCHED_FILE, stored)
    logger.info("Готово, оновлено %s", storage.MATCHED_FILE)


if __name__ == "__main__":
    asyncio.run(main())
