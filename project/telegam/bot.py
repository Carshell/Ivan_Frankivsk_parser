"""Telegram-бот (aiogram): команди, форматування та відправка оголошень.

Сам виклик парсерів і планування циклів лежить у main.py — цей модуль лише
знає, як показати оголошення користувачу і як відповідати на команди.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import FSInputFile, InputMediaPhoto, Message

import dedup
from telegam import storage

logger = logging.getLogger(__name__)

router = Router()

SITES_CONFIG = Path(__file__).parent.parent / "web_pages" / "sites.json"
MAX_PHOTOS_PER_ALBUM = 5
DIGEST_LIMIT = 10  # /digest показує топ-N за скорингом, а не все підряд
DIGEST_WINDOW_HOURS = 48  # "найкращі за останні два дні" — а не тільки цей прогін
TELEGRAM_MEDIA_CAPTION_LIMIT = 1024  # ліміт Telegram для підпису фото/альбому
TELEGRAM_MESSAGE_LIMIT = 4096  # ліміт Telegram для звичайного текстового повідомлення
SEND_DELAY_SECONDS = 1.2  # пауза між оголошеннями в одному чаті — щоб не ловити flood control

_T = TypeVar("_T")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _call_with_flood_retry(call: Callable[[], Awaitable[_T]], max_retries: int = 3) -> _T:
    """Викликає бот-метод і при TelegramRetryAfter (flood control) чекає стільки,
    скільки просить Telegram, і пробує ще раз — інакше оголошення просто губиться."""
    for attempt in range(max_retries):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            logger.warning(
                "Telegram flood control — чекаю %d сек і пробую ще раз (спроба %d/%d)",
                exc.retry_after, attempt + 1, max_retries,
            )
            await asyncio.sleep(exc.retry_after + 1)
    return await call()


def _score_key(listing: dict[str, Any]) -> float:
    score = (listing.get("claude") or {}).get("score")
    return score if isinstance(score, (int, float)) else -1  # без оцінки — в кінець списку


def _load_sites() -> list[dict]:
    return json.loads(SITES_CONFIG.read_text(encoding="utf-8"))


def format_listing_message(listing: dict[str, Any]) -> str:
    title = listing.get("title") or listing.get("address") or "Оренда"
    lines = [f"🏠 {title}"]

    address = listing.get("address")
    if address and address != title:
        # address = "title, ЖК..., район, місто" — прибираємо дублювання title з початку
        extra = address[len(title):].lstrip(", ") if address.startswith(title) else address
        if extra:
            lines.append(f"📍 {extra}")

    specs = []
    if listing.get("rooms"):
        specs.append(f"{listing['rooms']} кімнат")
    if listing.get("area"):
        specs.append(f"{listing['area']:.0f} м²")
    if listing.get("land_area_sotka"):
        specs.append(f"{listing['land_area_sotka']:.0f} сот. ділянки")
    if listing.get("floor") and listing.get("floor_total"):
        specs.append(f"поверх {listing['floor']}/{listing['floor_total']}")
    elif listing.get("floor_total"):
        specs.append(f"{listing['floor_total']}-поверховий")
    if specs:
        lines.append("📐 " + " · ".join(specs))

    price = listing.get("price_usd")
    lines.append(("💰 " + f"{price:,.0f} $/міс".replace(",", " ")) if price else "💰 ціна не вказана")

    if listing.get("features"):
        lines.append("✅ " + ", ".join(listing["features"]))

    source_note = f" (джерело: {listing['origin_site']})" if listing.get("origin_site") else ""
    lines.append(f"🔗 {listing['url']}{source_note}")

    claude = listing.get("claude")
    if claude:
        score = claude.get("score")
        if score is not None:
            lines.append(f"\n⭐ Скоринг: {score}/100")
        pros = claude.get("pros") or []
        if pros:
            lines.append("✅ " + ", ".join(pros))
        cons = claude.get("cons") or []
        if cons:
            lines.append("❌ " + ", ".join(cons))
        security = claude.get("security_review") or {}
        risk = security.get("risk_level")
        if risk:
            lines.append(f"\n🛡 Безпека: {risk.upper()} ризик")
        reasoning = security.get("reasoning")
        if reasoning:
            lines.append(reasoning)
        summary = claude.get("summary")
        if summary:
            lines.append(f"💬 {summary}")
    else:
        lines.append("\n⚠️ Оцінка Claude недоступна (перевір ANTHROPIC_API_KEY)")

    return "\n".join(lines)


def _is_remote_url(photo: str) -> bool:
    return photo.startswith(("http://", "https://"))


async def send_listing(bot: Bot, chat_id: int, listing: dict[str, Any]) -> None:
    """Надсилає оголошення. Фото можуть бути або URL (lun.ua тощо), або локальним
    файлом (Telegram-канали качають фото собі на диск, бо в них немає публічного URL).

    Локальні файли фото НЕ видаляються тут — вони лишаються на диску, щоб те
    саме оголошення можна було показати іншому підписнику пізніше (наприклад,
    новому користувачу на /start). Видалення — відповідальність
    telegam/storage.py, коли оголошення випадає зі сховища за лімітом.

    Три особливості Telegram, які інакше тихо "з'їдають" оголошення цілком:
      - підпис фото/альбому обмежений 1024 символами (звичайний текст — 4096) —
        довгий опис/скоринг Claude обрізаємо для підпису, а повний текст шлемо
        окремим повідомленням одразу після фото;
      - якщо Telegram сам не зміг завантажити фото за URL (WEBPAGE_CURL_FAILED —
        трапляється зі старими записами, збереженими ще до фіксу локального
        завантаження фото), оголошення все одно йде підписнику, просто без фото;
      - flood control (TelegramRetryAfter) — коли підряд шлеться багато
        оголошень (наприклад, з вимкненими фільтрами), Telegram тимчасово
        відмовляє і просить почекати N секунд; чекаємо і пробуємо ще раз
        замість того, щоб просто загубити оголошення.
    """
    caption = format_listing_message(listing)
    photos = (listing.get("photos") or [])[:MAX_PHOTOS_PER_ALBUM]

    try:
        if photos:
            media = [
                InputMediaPhoto(
                    media=photo if _is_remote_url(photo) else FSInputFile(photo),
                    caption=_truncate(caption, TELEGRAM_MEDIA_CAPTION_LIMIT) if i == 0 else None,
                )
                for i, photo in enumerate(photos)
            ]
            try:
                await _call_with_flood_retry(lambda: bot.send_media_group(chat_id, media=media))
            except TelegramBadRequest as exc:
                if "WEBPAGE_CURL_FAILED" not in str(exc):
                    raise
                logger.warning(
                    "Оголошення %s: Telegram не зміг завантажити фото за URL — надсилаю без фото",
                    listing.get("external_id"),
                )
                await _call_with_flood_retry(
                    lambda: bot.send_message(chat_id, _truncate(caption, TELEGRAM_MESSAGE_LIMIT))
                )
                return
            if len(caption) > TELEGRAM_MEDIA_CAPTION_LIMIT:
                await _call_with_flood_retry(
                    lambda: bot.send_message(chat_id, _truncate(caption, TELEGRAM_MESSAGE_LIMIT))
                )
        else:
            await _call_with_flood_retry(
                lambda: bot.send_message(chat_id, _truncate(caption, TELEGRAM_MESSAGE_LIMIT))
            )
    except Exception:
        logger.exception(
            "Не вдалося надіслати оголошення %s в чат %s",
            listing.get("external_id"), chat_id,
        )


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    storage.add_subscriber(message.chat.id)
    await message.answer(
        "Вітаю! Це бот моніторингу оренди будинків в Івано-Франківській області.\n\n"
        "/digest — перевірити оголошення зараз\n"
        "/pause — призупинити розсилку\n"
        "/resume — відновити розсилку\n"
        "/filters — активні джерела та фільтри\n\n"
        "Кожне оголошення проходить хард-фільтри (регіон/тип/ціна) і скоринг "
        "Claude за твоїм профілем — зі скорингом та блоком безпеки."
    )

    # Показуємо вже знайдені раніше оголошення одразу — без нового парсингу чи
    # повторного виклику Claude, просто те, що вже накопичено в matched_listings.json.
    matched = storage.get_matched()
    if matched:
        await message.answer(f"Ось {len(matched)} вже знайдених оголошень, що відповідають фільтрам:")
        for listing in matched:
            await send_listing(message.bot, message.chat.id, listing)
            await asyncio.sleep(SEND_DELAY_SECONDS)
        return

    # Бази ще немає (перший запуск, або щойно очистили reset_matched_db.py) —
    # не чекаємо плановий цикл (до 30 хв), а одразу запускаємо парсинг.
    await message.answer("Оголошень ще немає в базі — запускаю перший парсинг зараз, це може зайняти кілька хвилин...")
    from main import run_all_parsers, score_new_listings

    listings, _source_stats = await run_all_parsers()
    unique_listings, _duplicate_count = dedup.filter_duplicates(listings)
    to_send, _score_stats = await score_new_listings(unique_listings)
    storage.mark_seen_bulk([item["external_id"] for item in listings])
    if to_send:
        storage.add_matched(to_send)

    if not to_send:
        await message.answer("Поки що нічого не знайдено. Спробуй пізніше або команду /digest.")
        return

    await message.answer(f"Знайдено {len(to_send)} оголошень:")
    for listing in to_send:
        await send_listing(message.bot, message.chat.id, listing)
        await asyncio.sleep(SEND_DELAY_SECONDS)


@router.message(Command("pause"))
async def cmd_pause(message: Message) -> None:
    storage.set_paused(message.chat.id, True)
    await message.answer("Розсилку призупинено. Напиши /resume, щоб відновити.")


@router.message(Command("resume"))
async def cmd_resume(message: Message) -> None:
    storage.set_paused(message.chat.id, False)
    await message.answer("Розсилку відновлено.")


@router.message(Command("filters"))
async def cmd_filters(message: Message) -> None:
    sites = _load_sites()
    lines = ["Активні джерела:"]
    for site in sites:
        if site.get("enabled"):
            lines.append(f"• {site['name']}: {site['search_url']}")
    if len(lines) == 1:
        lines.append("(жодного увімкненого джерела в web_pages/sites.json)")
    await message.answer("\n".join(lines))


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    # локальний імпорт — уникаємо циклічної залежності з main.py
    from main import run_all_parsers, score_new_listings

    await message.answer("Збираю поточні оголошення, це може зайняти хвилину...")
    listings, _source_stats = await run_all_parsers()
    unique_listings, _duplicate_count = dedup.filter_duplicates(listings)
    fresh, _score_stats = await score_new_listings(unique_listings)

    storage.mark_seen_bulk([item["external_id"] for item in listings])

    if fresh:
        storage.add_matched(fresh)

    # "Найкращі за останні два дні" — не тільки щойно спарсене, а й усе, що вже
    # пройшло фільтр+скоринг за останні DIGEST_WINDOW_HOURS годин (могло бути
    # надіслане раніше, це нормально для дайджесту-підсумку). Найкращі за
    # скорингом — згори.
    recent = storage.get_recent_matched(hours=DIGEST_WINDOW_HOURS)
    combined = {item["external_id"]: item for item in recent}
    combined.update({item["external_id"]: item for item in fresh})
    ranked = sorted(combined.values(), key=_score_key, reverse=True)[:DIGEST_LIMIT]

    if not ranked:
        await message.answer("Підходящих оголошень за останні два дні не знайдено (після хард-фільтрів і скорингу).")
        return

    await message.answer(f"Топ {len(ranked)} оголошень за останні два дні (за скорингом):")
    for listing in ranked:
        await send_listing(message.bot, message.chat.id, listing)
        await asyncio.sleep(SEND_DELAY_SECONDS)
