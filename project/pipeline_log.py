"""Чистий, читабельний лог циклу парсингу — окремо від технічного логу
бібліотек (httpx/telethon/instagrapi/playwright), які засмічують консоль
власними деталями кожного HTTP-запиту.

Пишеться у logs/pipeline.log (ротація щоночі, зберігаються останні 14 днів)
і паралельно в консоль — щоб бачити те саме наживо через `docker compose logs`.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_FILE = LOG_DIR / "pipeline.log"

_logger = logging.getLogger("pipeline")
_logger.setLevel(logging.INFO)
_logger.propagate = False  # не змішувати з root-логером (там усі httpx/telethon деталі)


def _ensure_handlers() -> None:
    if _logger.handlers:
        return
    LOG_DIR.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE, when="midnight", backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    _logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    _logger.addHandler(console_handler)


_ensure_handlers()


def cycle_start() -> None:
    _logger.info("==================== ЦИКЛ ПАРСИНГУ ====================")


def source_result(name: str, found: int = 0, error: str | None = None) -> None:
    if error:
        _logger.info("%-14s | ПОМИЛКА: %s", name, error)
    else:
        _logger.info("%-14s | знайдено=%d", name, found)


def new_by_source(counts: dict[str, int], total_new: int) -> None:
    if not counts:
        _logger.info("%-14s | немає нових оголошень цього циклу", "нові")
        return
    parts = ", ".join(f"{src}={n}" for src, n in counts.items())
    _logger.info("%-14s | всього=%d (%s)", "нові", total_new, parts)


def duplicates_result(duplicates: int, total: int) -> None:
    _logger.info("%-14s | %d із %d відсіяно як дублікат іншого джерела", "дедуп", duplicates, total)


def sale_filter_result(rejected: int, total: int) -> None:
    _logger.info("%-14s | %d із %d відсіяно як ПРОДАЖ (не оренда)", "продаж", rejected, total)


def hard_filter_result(passed: int, total: int) -> None:
    _logger.info("%-14s | %d із %d пройшли", "хард-фільтр", passed, total)


def claude_summary(scored: int, unavailable: int, below_threshold: int) -> None:
    _logger.info(
        "%-14s | оцінено=%d недоступно=%d нижче_порогу=%d",
        "claude", scored, unavailable, below_threshold,
    )


def cycle_result(sent: int) -> None:
    _logger.info("%-14s | надіслано=%d", "результат", sent)
    _logger.info("==================== ЦИКЛ ЗАВЕРШЕНО ====================")


def cycle_error(exc: BaseException) -> None:
    _logger.info("ПОМИЛКА ЦИКЛУ: %s", exc)
    _logger.info("==================== ЦИКЛ ЗАВЕРШЕНО (з помилкою) ====================")
