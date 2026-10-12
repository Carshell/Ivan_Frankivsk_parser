"""Тестовий стенд Telegram-бота без мережі: підроблена сесія aiogram
записує всі виклики Bot API, а "користувач" натискає кнопки за їхнім текстом."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = Path(tempfile.mkdtemp(prefix="bot_test_"))
os.environ["RENT_DB_PATH"] = str(_TMP / "rent.db")
os.environ["ADMIN_CHAT_ID"] = "999"
os.environ.setdefault("PAGE_SIZE", "5")

from aiogram import Bot, Dispatcher  # noqa: E402
from aiogram.client.session.base import BaseSession  # noqa: E402
from aiogram.types import Message  # noqa: E402

import rentdb  # noqa: E402
import rentdb as db  # noqa: E402
from telegam import bot as bot_module  # noqa: E402

DATE = 1_700_000_000  # ненульова дата, інакше aiogram вважає повідомлення "недоступним"


class FakeSession(BaseSession):
    def __init__(self, harness: "Harness") -> None:
        super().__init__()
        self.h = harness

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):  # не використовується
        yield b""

    async def make_request(self, bot, method, timeout=None):
        return self.h.handle(bot, method)


class Harness:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.messages: dict[int, dict[str, Any]] = {}  # message_id -> {chat_id, text, markup}
        self.alerts: list[tuple[str, bool]] = []
        self._mid = 100
        self._uid = 0
        self.bot = Bot(token="123456:TEST", session=FakeSession(self))
        self.dp = Dispatcher()
        self.dp.include_router(bot_module.router)

    # ---------------------------------------------------------- Bot API ----
    def _message(self, bot, chat_id: int, text: str | None, markup, mid: int | None = None) -> Message:
        if mid is None:
            self._mid += 1
            mid = self._mid
        self.messages[mid] = {"chat_id": chat_id, "text": text, "markup": markup}
        return Message.model_validate(
            {"message_id": mid, "date": DATE, "chat": {"id": chat_id, "type": "private"}, "text": text},
            context={"bot": bot},
        )

    def handle(self, bot, method):
        name = type(method).__name__
        params = method.model_dump(exclude_none=True)
        self.calls.append((name, params))
        if name == "SendMessage":
            return self._message(bot, method.chat_id, method.text, method.reply_markup)
        if name == "SendPhoto":
            return self._message(bot, method.chat_id, method.caption, method.reply_markup)
        if name == "SendMediaGroup":
            return [self._message(bot, method.chat_id, None, None) for _ in method.media]
        if name == "EditMessageText":
            self.messages[method.message_id].update(text=method.text, markup=method.reply_markup)
            return self._message(bot, method.chat_id, method.text, method.reply_markup, method.message_id)
        if name == "EditMessageReplyMarkup":
            self.messages[method.message_id]["markup"] = method.reply_markup
            return self._message(bot, method.chat_id, self.messages[method.message_id]["text"], method.reply_markup, method.message_id)
        if name == "AnswerCallbackQuery":
            self.alerts.append((method.text or "", bool(method.show_alert)))
            return True
        if name in ("DeleteMessage", "SetMyCommands"):
            return True
        raise NotImplementedError(f"тестовий стенд не підтримує {name}")

    # ----------------------------------------------------------- користувач ---
    def _uid_next(self) -> int:
        self._uid += 1
        return self._uid

    def _user(self, chat_id: int) -> dict[str, Any]:
        return {"id": chat_id, "is_bot": False, "first_name": "Tester"}

    async def say(self, chat_id: int, text: str) -> None:
        message: dict[str, Any] = {
            "message_id": self._uid_next() + 5000, "date": DATE,
            "chat": {"id": chat_id, "type": "private"}, "from": self._user(chat_id), "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.dp.feed_raw_update(self.bot, {"update_id": self._uid_next(), "message": message})

    async def tap_data(self, chat_id: int, message_id: int, data: str) -> None:
        stored = self.messages.get(message_id, {"text": ""})
        update = {
            "update_id": self._uid_next(),
            "callback_query": {
                "id": f"cb{self._uid_next()}", "from": self._user(chat_id), "chat_instance": "x", "data": data,
                "message": {"message_id": message_id, "date": DATE,
                            "chat": {"id": chat_id, "type": "private"}, "text": stored.get("text") or "x"},
            },
        }
        await self.dp.feed_raw_update(self.bot, update)

    # ------------------------------------------------------------ огляд -----
    @staticmethod
    def buttons(markup) -> list[Any]:
        if markup is None or not hasattr(markup, "inline_keyboard"):
            return []
        return [b for row in markup.inline_keyboard for b in row]

    def latest_with_markup(self, chat_id: int, contains: str | None = None) -> tuple[int, dict[str, Any]]:
        """Найновіше повідомлення з inline-клавіатурою (за потреби — з кнопкою,
        текст якої містить `contains`)."""
        for mid in sorted(self.messages, reverse=True):
            msg = self.messages[mid]
            if msg["chat_id"] != chat_id or not hasattr(msg["markup"], "inline_keyboard"):
                continue
            if contains is None or any(contains in (b.text or "") for b in self.buttons(msg["markup"])):
                return mid, msg
        raise AssertionError(f"немає повідомлення з кнопкою {contains!r} у чаті {chat_id}")

    async def press(self, chat_id: int, label: str) -> None:
        """Натискає найновішу кнопку, текст якої містить label."""
        mid, msg = self.latest_with_markup(chat_id, label)
        button = next(b for b in self.buttons(msg["markup"]) if label in (b.text or ""))
        assert button.callback_data, f"кнопка {label!r} не має callback_data (url-кнопка?)"
        await self.tap_data(chat_id, mid, button.callback_data)

    def texts(self, chat_id: int) -> list[str]:
        return [m["text"] for _mid, m in sorted(self.messages.items()) if m["chat_id"] == chat_id and m["text"]]

    def last_text(self, chat_id: int) -> str:
        return self.texts(chat_id)[-1]

    def all_markups(self):
        for msg in self.messages.values():
            if hasattr(msg["markup"], "inline_keyboard"):
                yield msg["markup"]

    def mark(self) -> int:
        """Точка відліку: наступні виклики шукати після неї."""
        return len(self.calls)

    def since(self, mark: int) -> list[tuple[str, dict[str, Any]]]:
        return self.calls[mark:]


__all__ = ["Harness", "db", "rentdb", "bot_module"]
