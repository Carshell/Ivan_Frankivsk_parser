"""Одноразовий скрипт: повне очищення бази вже знайдених оголошень.

Прибирає matched_listings.json (включно зі старими записами з величезним
текстом від Claude і локальними фото-файлами, які на них посилались) і
очищує seen.json — щоб усе, що зараз реально є на ринку, наступний цикл
парсингу побачив як "нове" і прогнав через (тепер короткий) аналіз Claude
заново.

bootstrap_done.flag НЕ чіпаємо — залишається як є, тож наступний цикл НЕ
вважається "першим запуском" і одразу розсилає підписникам все знайдене
(а не тихо формує базову лінію без розсилки).

Запуск (з папки project/, або docker compose exec bot python reset_matched_db.py):
    python reset_matched_db.py
"""

from __future__ import annotations

import logging

from telegam import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    matched = storage._load_json(storage.MATCHED_FILE, [])
    for listing in matched:
        storage._delete_local_photos(listing)
    storage._save_json(storage.MATCHED_FILE, [])
    logger.info("Очищено %s (%d записів прибрано, локальні фото видалено)", storage.MATCHED_FILE, len(matched))

    seen = storage._load_json(storage.SEEN_FILE, [])
    storage._save_json(storage.SEEN_FILE, [])
    logger.info("Очищено %s (%d ID прибрано) — усе поточне наступний цикл побачить як нове", storage.SEEN_FILE, len(seen))

    logger.info("Готово. Наступний цикл парсингу (до 30 хв) розішле все знайдене підписникам заново.")


if __name__ == "__main__":
    main()
