"""Telegram-бот пошуку оренди (ТЗ): покроковий діалог на inline-кнопках.

Стан діалогу (крок + чернетка фільтрів) зберігається в базі (rentdb.users), а
не в пам'яті процесу: callback-кнопки кодують лише дію, тож бот переживає
рестарт, а застарілі кнопки розпізнаються й відхиляються, не ламаючи діалог.

Кроки: мова -> тип -> місто -> кімнати -> райони -> бюджет -> додаткові
фільтри -> видача. На кожному кроці (крім першого) є "Назад" і "Почати спочатку".
"""

from __future__ import annotations

import logging
import os
from typing import Any

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

import districts as dist
import rentdb
from i18n import AREA_RANGES, BUDGET_RANGES, LANGS, area_label, budget_label, t
from telegam.cards import call_with_flood_retry, card_keyboard, compare_text, send_cards

logger = logging.getLogger(__name__)

router = Router()

PAGE_SIZE = int(os.environ.get("PAGE_SIZE", "5"))
SAVED_FILTERS_LIMIT = 10
COMPARE_MAX = 6
_admin_raw = os.environ.get("ADMIN_CHAT_ID")
ADMIN_CHAT_ID = int(_admin_raw) if _admin_raw else None

ORDER = ["lang", "type", "city", "rooms", "districts", "budget", "extras"]
ROOM_OPTIONS = [1, 2, 3, 4]  # 4 = "4+"
LEGACY_MENU_BUTTON = "⚙️ Міста і тип нерухомості"  # стара клавіатура попередньої версії бота

EXTRAS: list[tuple[str, list[tuple[str, str]]]] = [
    ("pets", [("yes", "pets_yes"), ("no", "pets_no")]),
    ("kids", [("yes", "kids_yes"), ("no", "kids_no")]),
    ("floor", [("not_first", "floor_not_first"), ("not_last", "floor_not_last"), ("not_edge", "floor_not_edge")]),
    ("area", [(str(i), "") for i in range(len(AREA_RANGES))]),
    ("furn", [("yes", "furn_yes"), ("no", "furn_no")]),
    ("from", [("owner", "from_owner"), ("agency", "from_agency")]),
]
SORT_MODES = [("date", "sort_date"), ("price_asc", "sort_price_asc"), ("price_desc", "sort_price_desc"), ("area", "sort_area")]
FILTER_KEYS = ("type", "city", "rooms", "districts", "budget", "pets", "kids", "floor", "area", "furn", "from")


# ----------------------------------------------------------------- чернетка ----
def new_draft() -> dict[str, Any]:
    return {"type": None, "city": None, "rooms": [], "districts": [], "budget": None,
            "pets": None, "kids": None, "floor": None, "area": None, "furn": None, "from": None, "edit": False}


def filters_of(draft: dict[str, Any]) -> dict[str, Any]:
    return {k: draft.get(k) for k in FILTER_KEYS if draft.get(k) not in (None, [])}


def lang_of(state: dict[str, Any]) -> str:
    return state["lang"] if state.get("lang") in LANGS else "uk"


def extras_visible(draft: dict[str, Any]) -> list[tuple[str, list[tuple[str, str]]]]:
    return [e for e in EXTRAS if not (e[0] == "floor" and draft.get("type") == "house")]


def extra_option_label(lang: str, key: str, value: str, label_key: str) -> str:
    return area_label(lang, int(value)) + f" {t(lang, 'area_unit')}" if key == "area" else t(lang, label_key)


def extra_value_label(lang: str, key: str, value: Any) -> str | None:
    if value in (None, ""):
        return None
    for val, label_key in extras_options(key):
        if str(value) == val:
            return extra_option_label(lang, key, val, label_key)
    return None


# ------------------------------------------------------------------ тексти -----
def summary_text(lang: str, draft: dict[str, Any]) -> str:
    city = draft.get("city")
    selected = draft.get("districts") or []
    all_slugs = [d.slug for d in dist.districts_for(city or "")]
    if not selected or set(selected) == set(all_slugs):
        districts_txt = t(lang, "all_districts")
    elif len(selected) <= 3:
        districts_txt = ", ".join(dist.district_label(s, lang) or s for s in selected)
    else:
        districts_txt = f"{len(selected)}/{len(all_slugs)}"

    rooms = draft.get("rooms") or []
    rooms_txt = ", ".join(("4+" if r >= 4 else str(r)) for r in sorted(rooms)) or t(lang, "not_set")
    type_txt = t(lang, f"type_{draft['type']}") if draft.get("type") else t(lang, "not_set")
    budget = draft.get("budget")
    budget_txt = f"{budget_label(lang, budget)} {t(lang, 'unit_uah')}" if budget is not None else t(lang, "not_set")

    extras: list[str] = []
    for key, _options in extras_visible(draft):
        label = extra_value_label(lang, key, draft.get(key))
        if label:
            extras.append(f"{t(lang, 'x_' + key)}: {label}")

    lines = [
        t(lang, "step_summary"),
        f"• {t(lang, 's_type')}: {type_txt}",
        f"• {t(lang, 's_city')}: {dist.city_label(city, lang) if city else t(lang, 'not_set')}",
        f"• {t(lang, 's_rooms')}: {rooms_txt}",
        f"• {t(lang, 's_districts')}: {districts_txt}",
        f"• {t(lang, 's_budget')}: {budget_txt}",
        f"• {t(lang, 's_extras')}: {'; '.join(extras) if extras else t(lang, 'not_set')}",
    ]
    return "\n".join(lines)


def filter_name(lang: str, filters: dict[str, Any]) -> str:
    parts: list[str] = []
    if filters.get("type"):
        parts.append(t(lang, f"type_name_{filters['type']}"))
    if filters.get("city"):
        parts.append(dist.city_label(filters["city"], lang))
    rooms = filters.get("rooms") or []
    if rooms:
        parts.append("/".join(("4+" if r >= 4 else str(r)) for r in sorted(rooms)) + t(lang, "rooms_short", n="").strip())
    if filters.get("budget") is not None:
        parts.append(budget_label(lang, int(filters["budget"])))
    return " · ".join(parts) or "—"


# ---------------------------------------------------------------- клавіатури ---
def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def nav_row(lang: str, back: bool = True) -> list[InlineKeyboardButton]:
    row = []
    if back:
        row.append(btn(t(lang, "btn_back"), "nav:back"))
    row.append(btn(t(lang, "btn_restart"), "nav:restart"))
    return row


def mark(selected: bool, text: str) -> str:
    return f"✅ {text}" if selected else text


def kb_lang() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(t("uk", "lang_uk"), "lang:uk"), btn(t("en", "lang_en"), "lang:en")]])


def kb_type(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "type_apartment"), "type:apartment")],
        [btn(t(lang, "type_house"), "type:house")],
        nav_row(lang),
    ])


def kb_city(lang: str) -> InlineKeyboardMarkup:
    rows = [[btn(dist.city_label(code, lang), f"city:{code}")] for code, _uk, _en in dist.CITIES]
    rows.append(nav_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_rooms(lang: str, selected: list[int]) -> InlineKeyboardMarkup:
    row = [btn(mark(r in selected, "4+" if r == 4 else str(r)), f"room:{r}") for r in ROOM_OPTIONS]
    return InlineKeyboardMarkup(inline_keyboard=[row, [btn(t(lang, "btn_next"), "room:next")], nav_row(lang)])


def kb_districts(lang: str, city: str, selected: list[str]) -> InlineKeyboardMarkup:
    items = dist.districts_for(city)
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    for index, d in enumerate(items):
        pair.append(btn(mark(d.slug in selected, d.label(lang)), f"dist:{index}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    if city == "kyiv":
        rows.append([
            btn(mark(all(s in selected for s in dist.slugs_by_bank(city, "right")), t(lang, "bank_right")), "dist:bank:right"),
            btn(mark(all(s in selected for s in dist.slugs_by_bank(city, "left")), t(lang, "bank_left")), "dist:bank:left"),
        ])
    all_selected = len(selected) == len(items)
    rows.append([btn(mark(all_selected, t(lang, "all_plain")), "dist:all")])
    rows.append([btn(t(lang, "btn_next"), "dist:next")])
    rows.append(nav_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_budget(lang: str, current: int | None) -> InlineKeyboardMarkup:
    rows, pair = [], []
    for index in range(len(BUDGET_RANGES)):
        pair.append(btn(mark(current == index, budget_label(lang, index)), f"bud:{index}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append(nav_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_extras(lang: str, draft: dict[str, Any]) -> InlineKeyboardMarkup:
    rows = []
    any_set = False
    for key, _options in extras_visible(draft):
        label = extra_value_label(lang, key, draft.get(key))
        any_set = any_set or label is not None
        text = t(lang, f"x_{key}") + (f": {label}" if label else "")
        rows.append([btn(text, f"x:{key}")])
    rows.append([btn(t(lang, "btn_show") if any_set else t(lang, "btn_skip"), "x:done")])
    rows.append(nav_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def extras_options(key: str) -> list[tuple[str, str]]:
    for k, options in EXTRAS:
        if k == key:
            return options
    return []


def kb_extra_options(lang: str, key: str, current: Any) -> InlineKeyboardMarkup:
    rows = [
        [btn(mark(str(current) == value, extra_option_label(lang, key, value, label_key)), f"xs:{key}:{value}")]
        for value, label_key in extras_options(key)
    ]
    rows.append([btn(mark(current in (None, ""), t(lang, "btn_any")), f"xs:{key}:any")])
    rows.append(nav_row(lang))
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_summary(lang: str, draft: dict[str, Any]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("🏠 " + t(lang, "s_type"), "sum:type"), btn("🏙 " + t(lang, "s_city"), "sum:city")],
        [btn("🛏 " + t(lang, "s_rooms"), "sum:rooms"), btn("📍 " + t(lang, "s_districts"), "sum:districts")],
        [btn("💰 " + t(lang, "s_budget"), "sum:budget"), btn("⚙️ " + t(lang, "s_extras"), "sum:extras")],
        [btn(t(lang, "btn_show"), "sum:show")],
        nav_row(lang),
    ])


def kb_menu(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "menu_search"), "menu:search")],
        [btn(t(lang, "menu_favs"), "menu:favs"), btn(t(lang, "menu_filters"), "menu:filters")],
        [btn(t(lang, "menu_lang"), "menu:lang")],
    ])


def kb_footer(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "btn_change_filters"), "res:filters"), btn(t(lang, "btn_sort"), "res:sort")],
        [btn(t(lang, "btn_save"), "res:save")],
    ])


def kb_sort(lang: str, current: str) -> InlineKeyboardMarkup:
    rows = [[btn(mark(mode == current, t(lang, label)), f"sort:{mode}")] for mode, label in SORT_MODES]
    rows.append([btn(t(lang, "btn_back"), "res:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_empty(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(t(lang, "btn_all_districts"), "res:alldist"), btn(t(lang, "btn_change_budget"), "res:budget")],
        [btn(t(lang, "btn_change_filters"), "res:filters")],
    ])


# ----------------------------------------------------------------- кроки -------
def step_view(state: dict[str, Any], step: str) -> tuple[str, InlineKeyboardMarkup]:
    """Текст і клавіатура кроку (чиста функція від стану — зручна для тестів)."""
    lang, draft = lang_of(state), state["draft"]
    if step == "lang":
        return t(lang, "choose_lang"), kb_lang()
    if step == "type":
        return t(lang, "step_type"), kb_type(lang)
    if step == "city":
        return t(lang, "step_city"), kb_city(lang)
    if step == "rooms":
        return t(lang, "step_rooms"), kb_rooms(lang, draft.get("rooms") or [])
    if step == "districts":
        city = draft.get("city") or "kyiv"
        return t(lang, "step_districts", city=dist.city_label(city, lang)), kb_districts(lang, city, draft.get("districts") or [])
    if step == "budget":
        return t(lang, "step_budget"), kb_budget(lang, draft.get("budget"))
    if step == "extras":
        return t(lang, "step_extras"), kb_extras(lang, draft)
    if step.startswith("extras:"):
        key = step.split(":", 1)[1]
        return t(lang, "x_pick", name=t(lang, f"x_{key}")), kb_extra_options(lang, key, draft.get(key))
    if step == "summary":
        return summary_text(lang, draft), kb_summary(lang, draft)
    if step == "menu":
        return t(lang, "menu_title"), kb_menu(lang)
    return t(lang, "menu_title"), kb_menu(lang)


def prev_step(state: dict[str, Any]) -> str:
    step, draft = state.get("step") or "type", state["draft"]
    if step.startswith("extras:"):
        return "extras"
    if step == "summary":
        return "extras"
    if draft.get("edit") and step in ORDER:
        return "summary"
    if step in ORDER:
        index = ORDER.index(step)
        return ORDER[index - 1] if index > 0 else step
    return "type"


def next_step(state: dict[str, Any], step: str) -> str:
    if state["draft"].get("edit"):
        return "summary"
    index = ORDER.index(step)
    return ORDER[min(index + 1, len(ORDER) - 1)]


async def edit_or_send(bot: Bot, chat_id: int, message: Message | None, text: str, keyboard: InlineKeyboardMarkup | None) -> None:
    if message is not None:
        try:
            await message.edit_text(text, reply_markup=keyboard)
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return
            logger.info("Не вдалося відредагувати повідомлення (%s) — надсилаю нове", exc)
    await call_with_flood_retry(lambda: bot.send_message(chat_id, text, reply_markup=keyboard))


async def goto(bot: Bot, chat_id: int, state: dict[str, Any], step: str, message: Message | None = None) -> None:
    state["step"] = step
    rentdb.save_state(chat_id, step=step, draft=state["draft"])
    text, keyboard = step_view(state, step)
    await edit_or_send(bot, chat_id, message, text, keyboard)


def callback_message(callback: CallbackQuery) -> Message | None:
    return callback.message if isinstance(callback.message, Message) else None


def callback_chat(callback: CallbackQuery) -> int:
    message = callback_message(callback)
    return message.chat.id if message else callback.from_user.id


# ---------------------------------------------------------------- команди ------
async def setup_commands(bot: Bot) -> None:
    """Меню команд Telegram (кнопка "Меню" біля поля вводу) для обох мов."""
    for lang in LANGS:
        commands = [
            BotCommand(command="start", description=t(lang, "cmd_start")),
            BotCommand(command="menu", description=t(lang, "cmd_menu")),
            BotCommand(command="filters", description=t(lang, "cmd_filters")),
            BotCommand(command="favorites", description=t(lang, "cmd_favorites")),
            BotCommand(command="language", description=t(lang, "cmd_language")),
        ]
        try:
            await bot.set_my_commands(commands, language_code=lang)
        except Exception:
            logger.exception("Не вдалося встановити команди для мови %s", lang)
    try:
        await bot.set_my_commands([
            BotCommand(command="start", description=t("uk", "cmd_start")),
            BotCommand(command="menu", description=t("uk", "cmd_menu")),
            BotCommand(command="filters", description=t("uk", "cmd_filters")),
            BotCommand(command="favorites", description=t("uk", "cmd_favorites")),
            BotCommand(command="language", description=t("uk", "cmd_language")),
        ])
    except Exception:
        logger.exception("Не вдалося встановити команди за замовчуванням")


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    chat_id = message.chat.id
    state = rentdb.ensure_user(chat_id)
    state["draft"] = new_draft()
    if state["lang"] is None:
        await goto(message.bot, chat_id, state, "lang")
        return
    await goto(message.bot, chat_id, state, "type")


@router.message(Command("menu"))
async def cmd_menu(message: Message) -> None:
    chat_id = message.chat.id
    state = rentdb.ensure_user(chat_id)
    if state["lang"] is None:
        await goto(message.bot, chat_id, state, "lang")
        return
    await goto(message.bot, chat_id, state, "menu")


@router.message(Command("language"))
async def cmd_language(message: Message) -> None:
    chat_id = message.chat.id
    state = rentdb.ensure_user(chat_id)
    text, keyboard = step_view(state, "lang")
    await edit_or_send(message.bot, chat_id, None, text, keyboard)


@router.message(Command("favorites"))
async def cmd_favorites(message: Message) -> None:
    state = rentdb.ensure_user(message.chat.id)
    if state["lang"] is None:
        await goto(message.bot, message.chat.id, state, "lang")
        return
    await show_favorites(message.bot, message.chat.id, state, offset=0)


@router.message(Command("filters"))
async def cmd_filters(message: Message) -> None:
    state = rentdb.ensure_user(message.chat.id)
    if state["lang"] is None:
        await goto(message.bot, message.chat.id, state, "lang")
        return
    await show_saved_filters(message.bot, message.chat.id, state)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if ADMIN_CHAT_ID is None or message.chat.id != ADMIN_CHAT_ID:
        return
    stats = rentdb.stats()
    lines = [f"📊 Оголошень у базі: {stats['total']}"] + [f"• {k}: {v}" for k, v in sorted(stats["by_city"].items())]
    lines.append(f"👥 Користувачів: {len(rentdb.all_user_ids())}")
    await message.answer("\n".join(lines))


# --------------------------------------------------------------- callbacks -----
@router.callback_query(F.data.startswith("lang:"))
async def cb_lang(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    lang = (callback.data or "").split(":", 1)[1]
    if lang not in LANGS:
        await callback.answer()
        return
    state = rentdb.ensure_user(chat_id)
    rentdb.save_state(chat_id, lang=lang)
    state["lang"] = lang
    await callback.answer()
    bot, message = callback.bot, callback_message(callback)
    if state["step"] in (None, "lang"):
        state["draft"] = new_draft()
        if message is not None:
            await edit_or_send(bot, chat_id, message, t(lang, "lang_set"), None)
        await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "welcome")))
        await goto(bot, chat_id, state, "type")
    else:
        await edit_or_send(bot, chat_id, message, t(lang, "lang_set"), None)
        text, keyboard = step_view(state, state["step"])
        await call_with_flood_retry(lambda: bot.send_message(chat_id, text, reply_markup=keyboard))


@router.callback_query(F.data.startswith("nav:"))
async def cb_nav(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    action = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    if action == "restart":
        state["draft"] = new_draft()
        await goto(callback.bot, chat_id, state, "type", callback_message(callback))
    elif action == "back":
        await goto(callback.bot, chat_id, state, prev_step(state), callback_message(callback))


@router.callback_query(F.data.startswith("menu:"))
async def cb_menu(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    action = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    bot, message = callback.bot, callback_message(callback)
    if action == "search":
        state["draft"] = new_draft()
        await goto(bot, chat_id, state, "type", message)
    elif action == "favs":
        await show_favorites(bot, chat_id, state, offset=0)
    elif action == "filters":
        await show_saved_filters(bot, chat_id, state)
    elif action == "lang":
        text, keyboard = step_view(state, "lang")
        await edit_or_send(bot, chat_id, message, text, keyboard)


async def _stale(callback: CallbackQuery, lang: str) -> None:
    await callback.answer(t(lang, "stale"), show_alert=True)


@router.callback_query(F.data.startswith("type:"))
async def cb_type(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    value = (callback.data or "").split(":", 1)[1]
    if state["step"] != "type" or value not in ("apartment", "house"):
        return await _stale(callback, lang)
    await callback.answer()
    state["draft"]["type"] = value
    if value == "house":
        state["draft"]["floor"] = None
    await goto(callback.bot, chat_id, state, next_step(state, "type"), callback_message(callback))


@router.callback_query(F.data.startswith("city:"))
async def cb_city(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    value = (callback.data or "").split(":", 1)[1]
    if state["step"] != "city" or value not in {c for c, _u, _e in dist.CITIES}:
        return await _stale(callback, lang)
    await callback.answer()
    draft = state["draft"]
    changed = draft.get("city") != value
    draft["city"] = value
    if changed:
        draft["districts"] = []  # райони залежать від міста
    target = "districts" if (changed and draft.get("edit")) else next_step(state, "city")
    await goto(callback.bot, chat_id, state, target, callback_message(callback))


@router.callback_query(F.data.startswith("room:"))
async def cb_rooms(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    value = (callback.data or "").split(":", 1)[1]
    if state["step"] != "rooms":
        return await _stale(callback, lang)
    draft = state["draft"]
    if value == "next":
        if not draft.get("rooms"):
            await callback.answer(t(lang, "need_one"), show_alert=True)
            return
        await callback.answer()
        await goto(callback.bot, chat_id, state, next_step(state, "rooms"), callback_message(callback))
        return
    if not value.isdigit() or int(value) not in ROOM_OPTIONS:
        return await _stale(callback, lang)
    rooms = set(draft.get("rooms") or [])
    rooms.symmetric_difference_update({int(value)})
    draft["rooms"] = sorted(rooms)
    await callback.answer()
    await goto(callback.bot, chat_id, state, "rooms", callback_message(callback))


@router.callback_query(F.data.startswith("dist:"))
async def cb_districts(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    parts = (callback.data or "").split(":")
    draft = state["draft"]
    city = draft.get("city")
    if state["step"] != "districts" or not city:
        return await _stale(callback, lang)
    items = dist.districts_for(city)
    selected = set(draft.get("districts") or [])

    action = parts[1]
    if action == "next":
        if not selected:
            await callback.answer(t(lang, "need_one"), show_alert=True)
            return
        await callback.answer()
        await goto(callback.bot, chat_id, state, next_step(state, "districts"), callback_message(callback))
        return
    if action == "all":
        everything = {d.slug for d in items}
        selected = set() if selected == everything else everything
    elif action == "bank" and len(parts) == 3 and parts[2] in ("right", "left"):
        group = set(dist.slugs_by_bank(city, parts[2]))
        selected = selected - group if group <= selected else selected | group
    elif action.isdigit() and int(action) < len(items):
        selected.symmetric_difference_update({items[int(action)].slug})
    else:
        return await _stale(callback, lang)
    draft["districts"] = [d.slug for d in items if d.slug in selected]  # стабільний порядок
    await callback.answer()
    await goto(callback.bot, chat_id, state, "districts", callback_message(callback))


@router.callback_query(F.data.startswith("bud:"))
async def cb_budget(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    value = (callback.data or "").split(":", 1)[1]
    if state["step"] != "budget" or not value.isdigit() or int(value) >= len(BUDGET_RANGES):
        return await _stale(callback, lang)
    await callback.answer()
    state["draft"]["budget"] = int(value)
    await goto(callback.bot, chat_id, state, next_step(state, "budget"), callback_message(callback))


@router.callback_query(F.data.startswith("x:"))
async def cb_extras(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    key = (callback.data or "").split(":", 1)[1]
    if state["step"] != "extras":
        return await _stale(callback, lang)
    await callback.answer()
    if key == "done":
        state["draft"]["edit"] = False
        await run_search(callback.bot, chat_id, state, offset=0)
        return
    if key not in {k for k, _o in extras_visible(state["draft"])}:
        return
    await goto(callback.bot, chat_id, state, f"extras:{key}", callback_message(callback))


@router.callback_query(F.data.startswith("xs:"))
async def cb_extra_value(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    parts = (callback.data or "").split(":", 2)
    if len(parts) != 3 or state["step"] != f"extras:{parts[1]}":
        return await _stale(callback, lang)
    key, value = parts[1], parts[2]
    valid = {v for v, _l in extras_options(key)}
    if value != "any" and value not in valid:
        return await _stale(callback, lang)
    await callback.answer()
    if value == "any":
        state["draft"][key] = None
    else:
        state["draft"][key] = int(value) if key == "area" else value
    await goto(callback.bot, chat_id, state, "extras", callback_message(callback))


@router.callback_query(F.data.startswith("sum:"))
async def cb_summary(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    action = (callback.data or "").split(":", 1)[1]
    await callback.answer()
    bot, message = callback.bot, callback_message(callback)
    if action == "show":
        state["draft"]["edit"] = False
        await run_search(bot, chat_id, state, offset=0)
        return
    if action in ("type", "city", "rooms", "districts", "budget", "extras"):
        state["draft"]["edit"] = True
        if action == "districts" and not state["draft"].get("city"):
            action = "city"
        await goto(bot, chat_id, state, action, message)


# ------------------------------------------------------------------ видача -----
async def run_search(bot: Bot, chat_id: int, state: dict[str, Any], offset: int) -> None:
    lang = lang_of(state)
    draft = state["draft"]
    if not draft.get("type") or not draft.get("city"):
        await goto(bot, chat_id, state, "type" if not draft.get("type") else "city")
        return
    state["step"] = "results"
    rentdb.save_state(chat_id, step="results", draft=draft)
    filters = filters_of(draft)
    rows, total = rentdb.search(filters, sort=state["sort"], offset=offset, limit=PAGE_SIZE)

    if total == 0:
        await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "empty"), reply_markup=kb_empty(lang)))
        return
    if not rows:
        await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "nothing_more"), reply_markup=kb_footer(lang)))
        return

    shown_to = offset + len(rows)
    header = t(lang, "results_header", total=total, a=offset + 1, b=shown_to)
    await call_with_flood_retry(lambda: bot.send_message(chat_id, header))
    more_offset = shown_to if shown_to < total else None
    await send_cards(bot, chat_id, lang, rows, last_more_offset=more_offset)
    footer = t(lang, "results_footer", total=total, b=shown_to)
    await call_with_flood_retry(lambda: bot.send_message(chat_id, footer, reply_markup=kb_footer(lang)))


@router.callback_query(F.data.startswith("more:"))
async def cb_more(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    raw = (callback.data or "").split(":", 1)[1]
    if not raw.isdigit():
        return await _stale(callback, lang_of(state))
    await callback.answer()
    await run_search(callback.bot, chat_id, state, offset=int(raw))


@router.callback_query(F.data.startswith("res:"))
async def cb_results(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    action = (callback.data or "").split(":", 1)[1]
    bot, message = callback.bot, callback_message(callback)
    draft = state["draft"]

    if action == "filters":
        await callback.answer()
        draft["edit"] = True
        await goto(bot, chat_id, state, "summary", message)
    elif action == "sort":
        await callback.answer()
        await edit_or_send(bot, chat_id, message, t(lang, "sort_pick"), kb_sort(lang, state["sort"]))
    elif action == "back":
        await callback.answer()
        await edit_or_send(bot, chat_id, message, t(lang, "footer_actions"), kb_footer(lang))
    elif action == "alldist":
        await callback.answer()
        city = draft.get("city")
        draft["districts"] = [d.slug for d in dist.districts_for(city or "")]
        await run_search(bot, chat_id, state, offset=0)
    elif action == "budget":
        await callback.answer()
        draft["edit"] = True
        await goto(bot, chat_id, state, "budget", message)
    elif action == "save":
        filters = filters_of(draft)
        if not filters.get("type") or not filters.get("city"):
            return await _stale(callback, lang)
        if rentdb.sf_count(chat_id) >= SAVED_FILTERS_LIMIT:
            await callback.answer(t(lang, "sf_limit", n=SAVED_FILTERS_LIMIT), show_alert=True)
            return
        rentdb.sf_create(chat_id, filter_name(lang, filters), filters, notify=True)
        await callback.answer(t(lang, "sf_saved"), show_alert=True)
    else:
        await _stale(callback, lang)


@router.callback_query(F.data.startswith("sort:"))
async def cb_sort(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    mode = (callback.data or "").split(":", 1)[1]
    if mode not in rentdb.SORTS:
        return await _stale(callback, lang_of(state))
    await callback.answer()
    rentdb.save_state(chat_id, sort=mode)
    state["sort"] = mode
    await run_search(callback.bot, chat_id, state, offset=0)


# ------------------------------------------------------------------- обране ----
def _markup_more_button(markup: InlineKeyboardMarkup | None) -> InlineKeyboardButton | None:
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            if (button.callback_data or "").startswith(("more:", "fmore:")):
                return button
    return None


@router.callback_query(F.data.startswith("fav:"))
async def cb_fav(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit() or parts[1] not in ("add", "del"):
        return await _stale(callback, lang)
    listing = rentdb.get_listing(int(parts[2]))
    if listing is None:
        await callback.answer(t(lang, "stale"), show_alert=True)
        return
    if parts[1] == "add":
        rentdb.fav_add(chat_id, listing["id"])
        await callback.answer(t(lang, "fav_added"))
    else:
        rentdb.fav_remove(chat_id, listing["id"])
        await callback.answer(t(lang, "fav_removed"))

    message = callback_message(callback)
    if message is None:
        return
    more = _markup_more_button(message.reply_markup)
    keyboard = card_keyboard(lang, listing, is_fav=parts[1] == "add")
    if more is not None:
        keyboard.inline_keyboard.append([more])
    try:
        await message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.info("Не вдалося оновити кнопки картки: %s", exc)


async def show_favorites(bot: Bot, chat_id: int, state: dict[str, Any], offset: int) -> None:
    lang = lang_of(state)
    rows, total = rentdb.fav_page(chat_id, offset, PAGE_SIZE)
    if total == 0:
        await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "favs_empty")))
        return
    if offset == 0:
        compare = InlineKeyboardMarkup(inline_keyboard=[[btn(t(lang, "btn_compare"), "favs:cmp")]])
        await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "favs_title", n=total), reply_markup=compare))
    shown_to = offset + len(rows)
    more_offset = shown_to if shown_to < total else None
    await send_cards(bot, chat_id, lang, rows, last_more_offset=more_offset, more_prefix="fmore")


@router.callback_query(F.data.startswith("fmore:"))
async def cb_fmore(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    raw = (callback.data or "").split(":", 1)[1]
    if not raw.isdigit():
        return await _stale(callback, lang_of(state))
    await callback.answer()
    await show_favorites(callback.bot, chat_id, state, offset=int(raw))


@router.callback_query(F.data == "favs:cmp")
async def cb_compare(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    listings = rentdb.get_listings(rentdb.fav_ids(chat_id)[:COMPARE_MAX])
    if len(listings) < 2:
        await callback.answer(t(lang, "compare_need_two"), show_alert=True)
        return
    await callback.answer()
    await call_with_flood_retry(lambda: callback.bot.send_message(chat_id, compare_text(lang, listings)))


# ------------------------------------------------------- збережені фільтри -----
async def show_saved_filters(bot: Bot, chat_id: int, state: dict[str, Any]) -> None:
    lang = lang_of(state)
    saved = rentdb.sf_list(chat_id)
    if not saved:
        await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "sf_empty")))
        return
    await call_with_flood_retry(lambda: bot.send_message(chat_id, t(lang, "sf_title")))
    for item in saved:
        text = f"📌 {item['name']}\n" + summary_text(lang, {**new_draft(), **item["filters"]}).split("\n", 1)[1]
        notify_label = t(lang, "sf_notify_on" if item["notify"] else "sf_notify_off")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [btn(t(lang, "sf_show"), f"sf:show:{item['id']}"), btn(notify_label, f"sf:ntf:{item['id']}")],
            [btn(t(lang, "sf_delete"), f"sf:del:{item['id']}")],
        ])
        await call_with_flood_retry(lambda text=text, keyboard=keyboard: bot.send_message(chat_id, text, reply_markup=keyboard))


@router.callback_query(F.data.startswith("sf:"))
async def cb_saved_filter(callback: CallbackQuery) -> None:
    chat_id = callback_chat(callback)
    state = rentdb.ensure_user(chat_id)
    lang = lang_of(state)
    parts = (callback.data or "").split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return await _stale(callback, lang)
    action, filter_id = parts[1], int(parts[2])
    saved = rentdb.sf_get(chat_id, filter_id)
    if saved is None:
        await callback.answer(t(lang, "stale"), show_alert=True)
        return
    message = callback_message(callback)
    if action == "show":
        await callback.answer()
        state["draft"] = {**new_draft(), **saved["filters"]}
        await run_search(callback.bot, chat_id, state, offset=0)
    elif action == "ntf":
        enabled = rentdb.sf_toggle_notify(chat_id, filter_id)
        await callback.answer()
        if message is not None and enabled is not None:
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [btn(t(lang, "sf_show"), f"sf:show:{filter_id}"),
                 btn(t(lang, "sf_notify_on" if enabled else "sf_notify_off"), f"sf:ntf:{filter_id}")],
                [btn(t(lang, "sf_delete"), f"sf:del:{filter_id}")],
            ])
            await edit_or_send(callback.bot, chat_id, message, message.text or "", keyboard)
    elif action == "del":
        rentdb.sf_delete(chat_id, filter_id)
        await callback.answer(t(lang, "sf_deleted"))
        if message is not None:
            try:
                await message.delete()
            except TelegramBadRequest:
                pass
    else:
        await _stale(callback, lang)


# --------------------------------------------------------------- запасний ------
@router.message(F.text)
async def fallback_text(message: Message) -> None:
    """Будь-який інший текст (зокрема натиснення старої клавіатури попередньої
    версії бота) -> головне меню."""
    chat_id = message.chat.id
    state = rentdb.ensure_user(chat_id)
    if state["lang"] is None:
        await goto(message.bot, chat_id, state, "lang")
        return
    if message.text == LEGACY_MENU_BUTTON:
        await message.answer(t(lang_of(state), "menu_title"), reply_markup=ReplyKeyboardRemove())
    await goto(message.bot, chat_id, state, "menu")
