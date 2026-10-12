"""Сповіщення про нові об'єкти за збереженими фільтрами (ТЗ, п.6)."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot

import rentdb
from i18n import t
from telegam.cards import call_with_flood_retry, send_cards

logger = logging.getLogger(__name__)

MAX_CARDS_PER_FILTER = 5  # решта — одним рядком "…і ще N", щоб не засипати чат


async def notify_new_listings(bot: Bot, new_ids: list[int]) -> int:
    """Для кожного збереженого фільтра зі сповіщеннями шукає серед щойно
    доданих оголошень підходящі й надсилає картки. Одне оголошення одному
    користувачу — лише раз, навіть якщо підходить під кілька його фільтрів.
    Повертає кількість надісланих карток."""
    delivered: set[tuple[int, int]] = set()
    sent_total = 0

    for saved in rentdb.sf_all_notifying():
        chat_id = saved["chat_id"]
        rows, total = rentdb.search(
            saved["filters"], sort="date", offset=0, limit=MAX_CARDS_PER_FILTER + 20, only_ids=new_ids
        )
        fresh = [row for row in rows if (chat_id, row["id"]) not in delivered]
        if not fresh:
            continue
        shown = fresh[:MAX_CARDS_PER_FILTER]
        lang = rentdb.get_state(chat_id)["lang"] or "uk"
        try:
            sent = await send_cards(
                bot, chat_id, lang, shown, header=t(lang, "notify_new", name=saved["name"])
            )
            for row in shown:
                delivered.add((chat_id, row["id"]))
            sent_total += sent
            hidden = total - len(shown)
            if hidden > 0:
                await call_with_flood_retry(
                    lambda: bot.send_message(chat_id, t(lang, "notify_more", n=hidden, name=saved["name"]))
                )
        except Exception:
            logger.exception("Не вдалося надіслати сповіщення (chat_id=%s, фільтр %s)", chat_id, saved["id"])
        await asyncio.sleep(0.5)

    return sent_total
