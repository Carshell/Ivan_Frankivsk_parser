"""Картка об'єкта (ТЗ, п.5): текст з полями на окремих рядках + відправка
(альбом фото -> повідомлення з карткою і кнопками дій)."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto

import districts as dist
import rentdb
from i18n import t
from photos import hires_all

logger = logging.getLogger(__name__)

MAX_PHOTOS_PER_ALBUM = 5
CAPTION_LIMIT = 1024  # ліміт Telegram для підпису фото
MESSAGE_LIMIT = 4096
SEND_DELAY_SECONDS = 0.6  # між об'єктами в одному чаті — запобіжник від flood control
MANAGER_CONTACT_URL = os.environ.get("MANAGER_CONTACT_URL")  # необов'язково: кнопка "Менеджер"

_T = TypeVar("_T")


def fmt_money(value: float | None) -> str:
    return f"{value:,.0f}".replace(",", " ") if value is not None else "—"


def _yes_no(lang: str, value: int | None) -> str | None:
    if value is None:
        return None
    return t(lang, "allowed") if value else t(lang, "not_allowed")


def card_text(lang: str, listing: dict[str, Any]) -> str:
    """Поля картки — кожне на окремому рядку з іконкою; додаткова інформація
    показується лише якщо орендодавець її вказав."""
    lines: list[str] = []

    labels: list[str] = []
    if rentdb.is_price_dropped(listing):
        labels.append(t(lang, "label_price_drop"))
    if rentdb.is_new(listing):
        labels.append(t(lang, "label_new"))
    if listing.get("commission") == "none":
        labels.append(t(lang, "label_no_commission"))
    if labels:
        lines.append("  ".join(labels))

    lines.append(f"🆔 {t(lang, 'c_id')}: {listing['id']}")
    if listing.get("rooms"):
        lines.append(f"🛏 {t(lang, 'c_rooms')}: {t(lang, 'rooms_short', n=listing['rooms'])}")
    if listing.get("area"):
        lines.append(f"📐 {t(lang, 'c_area')}: {listing['area']:.0f} {t(lang, 'area_unit')}")
    if listing.get("floor") and listing.get("floor_total"):
        lines.append(f"🏢 {t(lang, 'c_floor')}: {listing['floor']}/{listing['floor_total']}")
    elif listing.get("floor_total"):
        lines.append(f"🏢 {t(lang, 'c_floor')}: {listing['floor_total']}")
    elif listing.get("floor"):
        lines.append(f"🏢 {t(lang, 'c_floor')}: {listing['floor']}")
    if listing.get("city") == "kyiv" and listing.get("bank"):
        lines.append(f"🌉 {t(lang, 'c_bank')}: {t(lang, 'bank_' + listing['bank'])}")

    place = [p for p in (dist.district_label(listing.get("district"), lang), listing.get("address")) if p]
    if place:
        lines.append(f"📍 {t(lang, 'c_district')}: {', '.join(place)}")

    price = listing.get("price_uah")
    if price:
        price_line = f"💰 {t(lang, 'c_price')}: {fmt_money(price)} {t(lang, 'unit_uah')}"
        if rentdb.is_price_dropped(listing):
            price_line += f" ({t(lang, 'was_price', price=fmt_money(listing['prev_price_uah']))})"
        if listing.get("utilities_included") is not None:
            utilities = "utilities_in" if listing["utilities_included"] else "utilities_out"
            price_line += f" · {t(lang, utilities)}"
        lines.append(price_line)

    pets = _yes_no(lang, listing.get("pets"))
    if pets:
        lines.append(f"🐾 {t(lang, 'c_pets')}: {pets}")
    kids = _yes_no(lang, listing.get("kids"))
    if kids:
        lines.append(f"👶 {t(lang, 'c_kids')}: {kids}")

    extra: list[str] = []
    commission = listing.get("commission")
    if commission:
        extra.append(f"💼 {t(lang, 'c_commission')}: {t(lang, 'no_commission') if commission == 'none' else commission}")
    deposit = listing.get("deposit")
    if deposit:
        extra.append(f"🔒 {t(lang, 'c_deposit')}: {t(lang, 'no_deposit') if deposit == 'none' else deposit}")
    if listing.get("heating"):
        extra.append(f"🔥 {t(lang, 'c_heating')}: {t(lang, 'heating_' + listing['heating'])}")
    autonomy = [t(lang, "au_" + a) for a in listing.get("autonomy") or []]
    if autonomy:
        extra.append(f"🔋 {t(lang, 'c_autonomy')}: {', '.join(autonomy)}")
    amenities = [
        t(lang, "am_" + key)
        for key in ("elevator", "balcony", "parking", "ac", "internet")
        if listing.get(key)
    ]
    if amenities:
        extra.append("✨ " + " · ".join(amenities))
    if listing.get("city") == "kyiv" and listing.get("metro"):
        extra.append(f"🚇 {t(lang, 'c_metro')}: {listing['metro']}")
    if listing.get("price_currency") == "USD" and listing.get("price_usd"):
        extra.append(f"💱 {t(lang, 'c_currency')}: USD ({fmt_money(listing['price_usd'])} $)")
    if extra:
        lines.append("")
        lines.append(f"ℹ️ {t(lang, 'c_extra')}:")
        lines.extend(extra)

    return "\n".join(lines)


def card_keyboard(
    lang: str,
    listing: dict[str, Any],
    is_fav: bool,
    with_more_offset: int | None = None,
    more_prefix: str = "more",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    contact: list[InlineKeyboardButton] = []
    if listing.get("url"):
        contact.append(InlineKeyboardButton(text=t(lang, "btn_contact"), url=listing["url"]))
    if MANAGER_CONTACT_URL:
        contact.append(InlineKeyboardButton(text=t(lang, "btn_manager"), url=MANAGER_CONTACT_URL))
    fav_button = InlineKeyboardButton(
        text=t(lang, "btn_fav_del" if is_fav else "btn_fav_add"),
        callback_data=f"fav:{'del' if is_fav else 'add'}:{listing['id']}",
    )
    rows.append([*contact, fav_button])
    if with_more_offset is not None:
        rows.append([InlineKeyboardButton(text=t(lang, "btn_more"), callback_data=f"{more_prefix}:{with_more_offset}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def call_with_flood_retry(call: Callable[[], Awaitable[_T]], max_retries: int = 3) -> _T:
    """Telegram при пачці повідомлень повертає TelegramRetryAfter — чекаємо
    вказаний час і пробуємо ще раз, а не губимо картку."""
    for attempt in range(max_retries):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            logger.warning("Flood control: чекаю %d с (спроба %d/%d)", exc.retry_after, attempt + 1, max_retries)
            await asyncio.sleep(exc.retry_after + 1)
    return await call()


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _photo_input(photo: str):
    return photo if photo.startswith(("http://", "https://")) else FSInputFile(photo)


async def send_card(
    bot: Bot,
    chat_id: int,
    lang: str,
    listing: dict[str, Any],
    is_fav: bool = False,
    with_more_offset: int | None = None,
    header: str | None = None,
    more_prefix: str = "more",
) -> bool:
    """Надсилає картку: альбом фото (до 5), далі повідомлення з текстом і
    кнопками (Telegram не дозволяє кнопки під альбомом). Одне фото — одним
    повідомленням (фото + підпис + кнопки). Якщо фото не вдалось відправити
    (Telegram не зміг сам завантажити URL) — картка йде текстом."""
    text = card_text(lang, listing)
    if header:
        text = f"{header}\n\n{text}"
    keyboard = card_keyboard(lang, listing, is_fav, with_more_offset, more_prefix)
    photos = hires_all(listing.get("photos"))[:MAX_PHOTOS_PER_ALBUM]

    async def send_text() -> None:
        await call_with_flood_retry(
            lambda: bot.send_message(chat_id, _truncate(text, MESSAGE_LIMIT), reply_markup=keyboard)
        )

    try:
        if len(photos) == 1:
            try:
                if len(text) <= CAPTION_LIMIT:
                    await call_with_flood_retry(
                        lambda: bot.send_photo(chat_id, _photo_input(photos[0]), caption=text, reply_markup=keyboard)
                    )
                else:
                    await call_with_flood_retry(lambda: bot.send_photo(chat_id, _photo_input(photos[0])))
                    await send_text()
            except TelegramBadRequest as exc:
                logger.warning("Фото картки %s не відправилось (%s) — шлю текстом", listing["id"], exc)
                await send_text()
        elif len(photos) > 1:
            media = [InputMediaPhoto(media=_photo_input(p)) for p in photos]
            try:
                await call_with_flood_retry(lambda: bot.send_media_group(chat_id, media=media))
            except TelegramBadRequest as exc:
                logger.warning("Альбом картки %s не відправився (%s) — шлю текстом", listing["id"], exc)
            await send_text()
        else:
            await send_text()
        return True
    except TelegramForbiddenError:
        logger.info("Чат %s заблокував бота — картку %s не надіслано", chat_id, listing["id"])
        return False
    except Exception:
        logger.exception("Не вдалося надіслати картку %s в чат %s", listing["id"], chat_id)
        return False


async def send_cards(
    bot: Bot,
    chat_id: int,
    lang: str,
    listings: list[dict[str, Any]],
    last_more_offset: int | None = None,
    header: str | None = None,
    more_prefix: str = "more",
) -> int:
    """Надсилає кілька карток поспіль; кнопка "Показати ще" (з offset) — лише
    під останньою. Повертає, скільки карток відправлено."""
    favs = set(rentdb.fav_ids(chat_id))
    sent = 0
    for index, listing in enumerate(listings):
        is_last = index == len(listings) - 1
        ok = await send_card(
            bot, chat_id, lang, listing,
            is_fav=listing["id"] in favs,
            with_more_offset=last_more_offset if is_last else None,
            header=header if index == 0 else None,
            more_prefix=more_prefix,
        )
        if ok:
            sent += 1
        if not is_last:
            await asyncio.sleep(SEND_DELAY_SECONDS)
    return sent


def compare_text(lang: str, listings: list[dict[str, Any]]) -> str:
    """Порівняння обраних: рядок на об'єкт + позначки найдешевшого/найбільшого."""
    priced = [l["price_uah"] for l in listings if l.get("price_uah")]
    areas = [l["area"] for l in listings if l.get("area")]
    cheapest = min(priced) if len(priced) > 1 else None
    largest = max(areas) if len(areas) > 1 else None

    blocks = [t(lang, "compare_title")]
    for listing in listings:
        parts = [f"🆔 {listing['id']}"]
        if listing.get("rooms"):
            parts.append(t(lang, "rooms_short", n=listing["rooms"]))
        area_part = f"{listing['area']:.0f} {t(lang, 'area_unit')}" if listing.get("area") else "—"
        if largest is not None and listing.get("area") == largest:
            area_part += f" ⬆️ {t(lang, 'compare_largest')}"
        parts.append(area_part)
        if listing.get("floor") and listing.get("floor_total"):
            parts.append(f"{listing['floor']}/{listing['floor_total']}")
        price_part = f"{fmt_money(listing.get('price_uah'))} {t(lang, 'unit_uah')}" if listing.get("price_uah") else "—"
        if cheapest is not None and listing.get("price_uah") == cheapest:
            price_part += f" ⬇️ {t(lang, 'compare_cheapest')}"
        parts.append(price_part)
        place = dist.district_label(listing.get("district"), lang) or dist.city_label(listing.get("city") or "", lang)
        parts.append(place)
        blocks.append("• " + " · ".join(parts))
    return "\n".join(blocks)
