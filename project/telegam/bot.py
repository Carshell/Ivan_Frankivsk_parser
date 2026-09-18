"""Telegram-бот (aiogram): команди, форматування та відправка оголошень.

Сам виклик парсерів і планування циклів лежить у main.py — цей модуль лише
знає, як показати оголошення користувачу і як відповідати на команди.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

import dedup
import hard_filters
from telegam import storage

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)

router = Router()

SITES_CONFIG = Path(__file__).parent.parent / "web_pages" / "sites.json"
MAX_PHOTOS_PER_ALBUM = 5
DIGEST_LIMIT = 10  # /digest показує топ-N за скорингом, а не все підряд
DIGEST_WINDOW_HOURS = 48  # "найкращі за останні два дні" — а не тільки цей прогін
TELEGRAM_MEDIA_CAPTION_LIMIT = 1024  # ліміт Telegram для підпису фото/альбому
TELEGRAM_MESSAGE_LIMIT = 4096  # ліміт Telegram для звичайного текстового повідомлення
SEND_DELAY_SECONDS = 1.2  # пауза між оголошеннями в одному чаті — щоб не ловити flood control

_admin_chat_id_raw = os.environ.get("ADMIN_CHAT_ID")
ADMIN_CHAT_ID = int(_admin_chat_id_raw) if _admin_chat_id_raw else None

# Майстер вибору міста/типу нерухомості (одразу після /start, поки підписник
# ще не налаштований — див. OnboardingStates і cmd_start). Код (перший
# елемент кортежу) — те, що зберігається в storage і звіряється з
# listing["city"]/listing["property_type"]; підпис — те, що бачить користувач.
CITY_OPTIONS: list[tuple[str, str]] = [
    ("kyiv", "Київ"),
    ("chernivtsi", "Чернівці"),
    ("ivano-frankivsk", "Івано-Франківськ"),
]
PROPERTY_TYPE_OPTIONS: list[tuple[str, str]] = [
    ("apartment", "Квартири"),
    ("house", "Дома"),
]

# Постійна клавіатура-меню (кнопки під полем вводу, не inline) — щоб відкрити
# майстер зміни міст/типу нерухомості можна було одним тапом, а не пам'ятати
# команду /preferences. Прикріплюється до привітальних повідомлень (cmd_start,
# cmd_preferences) і лишається в чаті, поки Telegram-клієнт не отримає інший
# reply-keyboard або ReplyKeyboardRemove.
PREFERENCES_BUTTON_TEXT = "⚙️ Міста і тип нерухомості"
MAIN_MENU_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=PREFERENCES_BUTTON_TEXT)]],
    resize_keyboard=True,
)

_T = TypeVar("_T")


class OnboardingStates(StatesGroup):
    choosing_cities = State()
    choosing_property_types = State()


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


async def send_listing(
    bot: Bot, chat_id: int, listing: dict[str, Any], delete_token: str | None = None
) -> None:
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

    delete_token — якщо задано (розсилка з main.parser_loop, а не /start чи
    /digest), message_id усіх надісланих у цей чат повідомлень запам'ятовуються
    під цим токеном (telegam/storage.record_sent_listing), а адміну (ADMIN_CHAT_ID)
    додатково надсилається кнопка "Видалити в усіх" — щоб можна було одним
    натисканням прибрати оголошення з чатів усіх підписників, якщо воно
    виявилось помилковим/недоречним.
    """
    caption = format_listing_message(listing)
    photos = (listing.get("photos") or [])[:MAX_PHOTOS_PER_ALBUM]
    sent_message_ids: list[int] = []

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
                sent = await _call_with_flood_retry(lambda: bot.send_media_group(chat_id, media=media))
                sent_message_ids.extend(m.message_id for m in sent)
            except TelegramBadRequest as exc:
                if "WEBPAGE_CURL_FAILED" not in str(exc):
                    raise
                logger.warning(
                    "Оголошення %s: Telegram не зміг завантажити фото за URL — надсилаю без фото",
                    listing.get("external_id"),
                )
                sent = await _call_with_flood_retry(
                    lambda: bot.send_message(chat_id, _truncate(caption, TELEGRAM_MESSAGE_LIMIT))
                )
                sent_message_ids.append(sent.message_id)
            else:
                # else виконується лише якщо send_media_group НЕ впав з винятком —
                # тобто фото успішно пішли і тут ще не надсилали текст-фолбек вище.
                if len(caption) > TELEGRAM_MEDIA_CAPTION_LIMIT:
                    sent = await _call_with_flood_retry(
                        lambda: bot.send_message(chat_id, _truncate(caption, TELEGRAM_MESSAGE_LIMIT))
                    )
                    sent_message_ids.append(sent.message_id)
        else:
            sent = await _call_with_flood_retry(
                lambda: bot.send_message(chat_id, _truncate(caption, TELEGRAM_MESSAGE_LIMIT))
            )
            sent_message_ids.append(sent.message_id)
    except Exception:
        logger.exception(
            "Не вдалося надіслати оголошення %s в чат %s",
            listing.get("external_id"), chat_id,
        )
        return

    if delete_token and sent_message_ids:
        storage.record_sent_listing(delete_token, chat_id, sent_message_ids)
        if ADMIN_CHAT_ID is not None and chat_id == ADMIN_CHAT_ID:
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🗑 Видалити в усіх", callback_data=f"del:{delete_token}")]]
            )
            try:
                await bot.send_message(chat_id, "🛠 Це оголошення пішло всім підписникам.", reply_markup=keyboard)
            except Exception:
                logger.exception("Не вдалося надіслати адміну кнопку видалення для %s", listing.get("external_id"))


@router.callback_query(F.data.startswith("del:"))
async def cb_delete_everywhere(callback: CallbackQuery) -> None:
    """Адмін натиснув «Видалити в усіх» під своєю копією розісланого
    оголошення — видаляє те саме оголошення з чатів УСІХ підписників, кому
    воно пішло (за message_id, записаними в send_listing)."""
    if ADMIN_CHAT_ID is None or callback.from_user is None or callback.from_user.id != ADMIN_CHAT_ID:
        await callback.answer("Лише адмін може це робити.", show_alert=True)
        return

    token = callback.data.split(":", 1)[1]
    records = storage.pop_sent_records(token)
    if not records:
        await callback.answer("Записів для видалення не знайдено (можливо, вже видалено раніше).", show_alert=True)
        return

    deleted, failed = 0, 0
    for record in records:
        for message_id in record["message_ids"]:
            try:
                await callback.bot.delete_message(record["chat_id"], message_id)
                deleted += 1
            except Exception:
                failed += 1

    if callback.message is not None:
        await callback.message.edit_text(
            f"🗑 Видалено з {len(records)} чатів ({deleted} повідомлень"
            + (f", {failed} не вдалося — вже видалені або застарі)" if failed else ")")
        )
    await callback.answer("Готово")


def _label_for(options: list[tuple[str, str]], code: str) -> str:
    return next((label for c, label in options if c == code), code)


def _build_choice_keyboard(options: list[tuple[str, str]], selected: set[str], prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=(f"✅ {label}" if code in selected else label), callback_data=f"{prefix}:{code}")]
        for code, label in options
    ]
    rows.append([InlineKeyboardButton(text="Підтвердити", callback_data=f"{prefix}:confirm")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _start_onboarding(message: Message, state: FSMContext) -> None:
    await state.set_state(OnboardingStates.choosing_cities)
    await state.update_data(cities=[])
    await message.answer(
        "Обери місто(а), де шукати оренду (можна декілька — тицяєш, з'являється ✅), "
        "а тоді тисни «Підтвердити»:",
        reply_markup=_build_choice_keyboard(CITY_OPTIONS, set(), "city"),
    )


async def _deliver_initial_listings(bot: Bot, chat_id: int, prefs: dict[str, list[str] | None]) -> None:
    """Показує вже знайдені раніше оголошення, що підходять під вибір
    підписника, або (якщо таких ще нема) одразу запускає повний парсинг —
    спільна частина для cmd_start (уже налаштований підписник) і фіналу
    майстра вибору (щойно налаштувався)."""
    cities, property_types = prefs["cities"], prefs["property_types"]
    matched = [
        listing
        for listing in storage.get_matched(limit=0)
        if hard_filters.matches_subscriber_preferences(listing, cities, property_types)
    ]
    if matched:
        await bot.send_message(chat_id, f"Ось {len(matched)} вже знайдених оголошень під твій вибір:")
        for listing in matched:
            await send_listing(bot, chat_id, listing)
            await asyncio.sleep(SEND_DELAY_SECONDS)
        return

    await bot.send_message(
        chat_id, "Оголошень під твій вибір ще немає в базі — запускаю парсинг зараз, це може зайняти кілька хвилин..."
    )
    from main import run_all_parsers, score_new_listings

    listings, _source_stats = await run_all_parsers()
    unique_listings, _duplicate_count = dedup.filter_duplicates(listings)
    to_send, _score_stats = await score_new_listings(unique_listings)
    storage.mark_seen_bulk([item["external_id"] for item in listings])
    if to_send:
        storage.add_matched(to_send)

    filtered = [
        listing for listing in to_send if hard_filters.matches_subscriber_preferences(listing, cities, property_types)
    ]
    if not filtered:
        await bot.send_message(chat_id, "Поки що нічого підхожого не знайдено. Спробуй пізніше або команду /digest.")
        return

    await bot.send_message(chat_id, f"Знайдено {len(filtered)} оголошень під твій вибір:")
    for listing in filtered:
        await send_listing(bot, chat_id, listing)
        await asyncio.sleep(SEND_DELAY_SECONDS)


@router.callback_query(OnboardingStates.choosing_cities, F.data.startswith("city:"))
async def cb_choose_city(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None:
        return
    code = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("cities", []))

    if code == "confirm":
        if not selected:
            await callback.answer("Обери хоча б одне місто.", show_alert=True)
            return
        cities = sorted(selected)
        await state.update_data(cities=cities, property_types=[])
        await state.set_state(OnboardingStates.choosing_property_types)
        await callback.message.edit_text(
            "Міста: " + ", ".join(_label_for(CITY_OPTIONS, c) for c in cities) + " ✅"
        )
        await callback.message.answer(
            "Тепер обери тип нерухомості (можна обидва):",
            reply_markup=_build_choice_keyboard(PROPERTY_TYPE_OPTIONS, set(), "ptype"),
        )
        await callback.answer()
        return

    selected.symmetric_difference_update({code})
    await state.update_data(cities=list(selected))
    await callback.message.edit_reply_markup(reply_markup=_build_choice_keyboard(CITY_OPTIONS, selected, "city"))
    await callback.answer()


@router.callback_query(OnboardingStates.choosing_property_types, F.data.startswith("ptype:"))
async def cb_choose_property_type(callback: CallbackQuery, state: FSMContext) -> None:
    if callback.data is None or callback.message is None:
        return
    code = callback.data.split(":", 1)[1]
    data = await state.get_data()
    selected = set(data.get("property_types", []))

    if code == "confirm":
        if not selected:
            await callback.answer("Обери хоча б один тип.", show_alert=True)
            return
        cities = data.get("cities", [])
        property_types = sorted(selected)
        chat_id = callback.message.chat.id
        storage.set_city_preferences(chat_id, cities)
        storage.set_property_type_preferences(chat_id, property_types)
        await state.clear()

        await callback.message.edit_text(
            "Готово! Міста: " + ", ".join(_label_for(CITY_OPTIONS, c) for c in cities) + ".\n"
            "Типи: " + ", ".join(_label_for(PROPERTY_TYPE_OPTIONS, p) for p in property_types) + ".\n"
            "Налаштування збережено — надалі надсилатиму лише те, що підходить "
            "(змінити вибір можна командою /preferences)."
        )
        await callback.answer("Збережено")
        await _deliver_initial_listings(
            callback.message.bot, chat_id, {"cities": cities, "property_types": property_types}
        )
        return

    selected.symmetric_difference_update({code})
    await state.update_data(property_types=list(selected))
    await callback.message.edit_reply_markup(
        reply_markup=_build_choice_keyboard(PROPERTY_TYPE_OPTIONS, selected, "ptype")
    )
    await callback.answer()


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    storage.add_subscriber(message.chat.id)

    if not storage.has_preferences(message.chat.id):
        await message.answer(
            "Вітаю! Це бот моніторингу оренди нерухомості.\n\n"
            "Спершу оберемо, що саме тобі показувати — це займе два кроки.",
            reply_markup=MAIN_MENU_KEYBOARD,
        )
        await _start_onboarding(message, state)
        return

    await message.answer(
        "З поверненням!\n\n"
        "/digest — перевірити оголошення зараз\n"
        "/pause — призупинити розсилку\n"
        "/resume — відновити розсилку\n"
        "/filters — активні джерела\n"
        f"«{PREFERENCES_BUTTON_TEXT}» (кнопка знизу) або /preferences — змінити вибір міст і типу нерухомості\n\n"
        "Кожне оголошення проходить хард-фільтр (продаж/ціна) і скоринг "
        "Claude за профілем замовника — зі скорингом та блоком безпеки.",
        reply_markup=MAIN_MENU_KEYBOARD,
    )

    prefs = storage.get_preferences(message.chat.id)
    await _deliver_initial_listings(message.bot, message.chat.id, prefs)


@router.message(Command("preferences"))
@router.message(F.text == PREFERENCES_BUTTON_TEXT)
async def cmd_preferences(message: Message, state: FSMContext) -> None:
    await message.answer("Обираємо заново.", reply_markup=MAIN_MENU_KEYBOARD)
    await _start_onboarding(message, state)


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
    prefs = storage.get_preferences(message.chat.id)
    lines = []
    if prefs["cities"] or prefs["property_types"]:
        cities_txt = ", ".join(_label_for(CITY_OPTIONS, c) for c in prefs["cities"] or []) or "—"
        types_txt = ", ".join(_label_for(PROPERTY_TYPE_OPTIONS, p) for p in prefs["property_types"] or []) or "—"
        lines.append(f"Твій вибір: міста — {cities_txt}; тип — {types_txt} (/preferences — змінити)\n")

    lines.append("Активні джерела:")
    site_lines = [
        f"• {site['name']} [{site.get('city')}/{site.get('property_type')}]: {site['search_url']}"
        for site in _load_sites()
        if site.get("enabled")
    ]
    lines.extend(site_lines or ["(жодного увімкненого джерела в web_pages/sites.json)"])
    await message.answer("\n".join(lines))


@router.message(Command("digest"))
async def cmd_digest(message: Message) -> None:
    if not storage.has_preferences(message.chat.id):
        await message.answer("Спершу пройди налаштування через /start — обери міста і тип нерухомості.")
        return
    prefs = storage.get_preferences(message.chat.id)

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
    # скорингом — згори. Фільтруємо під вибір саме цього підписника (місто/тип).
    recent = storage.get_recent_matched(hours=DIGEST_WINDOW_HOURS)
    combined = {item["external_id"]: item for item in recent}
    combined.update({item["external_id"]: item for item in fresh})
    matching = [
        listing
        for listing in combined.values()
        if hard_filters.matches_subscriber_preferences(listing, prefs["cities"], prefs["property_types"])
    ]
    ranked = sorted(matching, key=_score_key, reverse=True)[:DIGEST_LIMIT]

    if not ranked:
        await message.answer("Підходящих оголошень за останні два дні не знайдено (після хард-фільтрів і скорингу).")
        return

    await message.answer(f"Топ {len(ranked)} оголошень за останні два дні (за скорингом):")
    for listing in ranked:
        await send_listing(message.bot, message.chat.id, listing)
        await asyncio.sleep(SEND_DELAY_SECONDS)
