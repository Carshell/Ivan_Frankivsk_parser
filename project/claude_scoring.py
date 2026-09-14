"""Скоринг оголошень + ревью безпеки через Claude API (п.9 ТЗ).

Промпт (включно з профілем замовника, п.4) винесений у claude_prompt.txt —
редагується без передеплою, як і вимагає ТЗ. Цей файл лише викликає API і
парсить відповідь.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

API_KEY = os.environ.get("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-5"
MAX_TOKENS = 2048
PROMPT_FILE = BASE_DIR / "claude_prompt.txt"

# Поля оголошення, які реально потрібні Claude для аналізу — без raw_snapshot
# (сирі дані джерела, шумні й великі) і без внутрішньої бухгалтерії (found_at тощо).
_LISTING_FIELDS_FOR_CLAUDE = (
    "source", "url", "title", "description", "price_usd", "price_uah",
    "area", "land_area_sotka", "rooms", "floor", "floor_total",
    "address", "lat", "lon", "features",
)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic | None:
    global _client
    if not API_KEY:
        return None
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=API_KEY)
    return _client


def _load_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def _build_input(listing: dict[str, Any], nearby_objects: list[dict[str, Any]]) -> str:
    trimmed = {k: listing.get(k) for k in _LISTING_FIELDS_FOR_CLAUDE}
    return json.dumps({"listing": trimmed, "nearby_objects": nearby_objects}, ensure_ascii=False)


def _parse_response(text: str) -> dict[str, Any] | None:
    text = text.strip()
    # Claude інколи додає markdown-огорожу (```json ... ```) — іноді навіть лише
    # закриваючу без відкриваючої. Прибираємо з обох кінців незалежно одне від одного.
    if text.startswith("```"):
        text = text[3:]
        if text.lower().startswith("json"):
            text = text[4:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Остання спроба — вирізати підрядок від першої "{" до останньої "}"
    # (на випадок стороннього тексту навколо, який не впіймали огорожі вище).
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass

    # Логуємо повністю (без обрізання) — інакше неможливо відрізнити "Claude
    # дійсно обрізав відповідь через ліміт токенів" від "просто зайвий текст
    # навколо JSON", коли розбираємось з цим самим логом пізніше.
    logger.error("Не вдалося розпарсити відповідь Claude як JSON (довжина=%d): %s", len(text), text)
    return None


async def score_listing(listing: dict[str, Any], nearby_objects: list[dict[str, Any]]) -> dict[str, Any] | None:
    """{score, verdict, security_review, pros, cons, summary} або None при помилці/відсутньому ключі."""
    client = _get_client()
    if client is None:
        logger.warning("ANTHROPIC_API_KEY не задано в .env — пропускаю скоринг Claude")
        return None

    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=_load_prompt(),
            messages=[{"role": "user", "content": _build_input(listing, nearby_objects)}],
        )
    except Exception:
        logger.exception("Помилка виклику Claude API для %s", listing.get("url"))
        return None

    if response.stop_reason == "max_tokens":
        logger.error(
            "Відповідь Claude обрізана лімітом MAX_TOKENS=%d для %s — збільш ліміт",
            MAX_TOKENS, listing.get("url"),
        )

    text = "".join(block.text for block in response.content if block.type == "text")
    return _parse_response(text)
