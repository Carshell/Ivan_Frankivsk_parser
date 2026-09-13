"""Telegram-бот (aiogram): команди, форматування та відправка оголошень.

Сам виклик парсерів і планування циклів лежить у main.py — цей модуль лише
знає, як показати оголошення користувачу і як відповідати на команди.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from telegam import storage

logger = logging.getLogger(__name__)

router = Router()

SITES_CONFIG = Path(__file__).parent.parent / "web_pages" / "sites.json"
MAX_PHOTOS_PER_ALBUM = 5


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
    """
    caption = format_listing_message(listing)
    photos = (listing.get("photos") or [])[:MAX_PHOTOS_PER_ALBUM]
    try:
        if photos:
            media = [
                InputMediaPhoto(
                    media=photo if _is_remote_url(photo) else FSInputFile(photo),
                    caption=caption if i == 0 else None,
                )
                for i, photo in enumerate(photos)
            ]
            await bot.send_media_group(chat_id, media=media)
        else:
            await bot.send_message(chat_id, caption)
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
    if not matched:
        await message.answer(
            "Поки що немає раніше знайдених оголошень — перший цикл парсингу "
            "запуститься найближчим часом (кожні 30 хв), або перевір просто зараз: /digest"
        )
        return

    await message.answer(f"Ось {len(matched)} вже знайдених оголошень, що відповідають фільтрам:")
    for listing in matched:
        await send_listing(message.bot, message.chat.id, listing)


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
    to_send, _score_stats = await score_new_listings(listings)

    storage.mark_seen_bulk([item["external_id"] for item in listings])

    if not to_send:
        await message.answer("Підходящих оголошень зараз не знайдено (після хард-фільтрів і скорингу).")
        return

    storage.add_matched(to_send)

    for listing in to_send:
        await send_listing(message.bot, message.chat.id, listing)

    await message.answer(f"Готово: {len(to_send)} оголошень.")
