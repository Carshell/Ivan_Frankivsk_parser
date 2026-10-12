"""Наскрізна перевірка діалогу бота (ТЗ) на підробленому Telegram API:
python tests/test_bot_flow.py"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Harness, rentdb  # noqa: E402

import districts as dist  # noqa: E402
from telegam import notify  # noqa: E402


def listing(ext, **kw):
    base = {
        "external_id": ext, "source": "test", "url": f"https://example.com/{ext}", "origin_site": "test",
        "city": "kyiv", "property_type": "apartment", "title": f"Оренда {ext}", "description": "",
        "address": None, "price_uah": 21000.0, "price_usd": 500.0, "rooms": 2, "area": 55.0, "floor": 4,
        "floor_total": 9, "photos": ["https://example.com/p.jpg"],
    }
    base.update(kw)
    return base


def seed():
    rentdb.connect()
    L = rentdb.insert_listing
    L(listing("k1", price_uah=18000.0, area=55.0, floor=3), {"district": "kyiv.pechersk", "pets": True})
    L(listing("k2", price_uah=22000.0, area=62.0, floor=5, floor_total=16, photos=["https://e.com/a.jpg", "https://e.com/b.jpg"]),
      {"district": "kyiv.pozniaky", "pets": True, "furnished": True, "from_owner": True, "commission": "none",
       "deposit": "1 місяць", "heating": "individual", "autonomy": ["generator", "starlink"], "elevator": True,
       "balcony": True, "metro": "Осокорки 10 хв", "utilities_included": True})
    L(listing("k3", price_uah=24000.0, rooms=3, area=85.0, floor=9, floor_total=9), {"district": "kyiv.obolon"})
    L(listing("k4", price_uah=30000.0, rooms=1, area=33.0, floor=1, photos=[]), {"district": "kyiv.center"})
    L(listing("b27", price_uah=27000.0), {"district": "kyiv.pozniaky", "pets": True})
    for i in range(9):
        L(listing(f"f{i}", price_uah=20500.0 + i * 300, area=50.0 + i), {"district": "kyiv.pechersk", "pets": True})
    L(listing("h1", city="ivano-frankivsk", property_type="house", price_uah=52000.0, rooms=4, area=140.0, floor=None, floor_total=2),
      {"district": "ivano-frankivsk.krykhivtsi"})
    rentdb.mark_bootstrapped()


def card_ids(calls):
    ids = []
    for name, params in calls:
        if name in ("SendMessage", "SendPhoto"):
            text = params.get("text") or params.get("caption") or ""
            m = re.search(r"🆔 ID: (\d+)", text)
            if m:
                ids.append(int(m.group(1)))
    return ids


def prices(calls):
    out = []
    for name, params in calls:
        text = params.get("text") or params.get("caption") or ""
        m = re.search(r"💰 Ціна: ([\d ]+) грн", text)
        if name in ("SendMessage", "SendPhoto") and m:
            out.append(int(m.group(1).replace(" ", "")))
    return out


def button_texts(markup):
    return [b.text for row in markup.inline_keyboard for b in row]


async def main():
    seed()
    h = Harness()
    C = 1  # основний користувач

    # ---------------------------------------------------------------- мова ----
    await h.say(C, "/start")
    text = h.last_text(C)
    assert "Choose your language" in text
    assert button_texts(h.latest_with_markup(C)[1]["markup"]) == ["🇺🇦 Українська", "🇬🇧 English"]
    await h.press(C, "Українська")
    assert any("Привіт" in t for t in h.texts(C)), "вітання після вибору мови"
    assert rentdb.get_state(C)["lang"] == "uk"
    assert "Що ви шукаєте" in h.last_text(C)
    # на першому кроці (type) є "Назад" -> повернення до мови, а "Почати спочатку" є завжди
    type_buttons = button_texts(h.latest_with_markup(C)[1]["markup"])
    assert "Орендувати квартиру 🏠" in type_buttons and "Орендувати будинок 🏡" in type_buttons
    assert "⬅️ Назад" in type_buttons and "🔄 Почати спочатку" in type_buttons

    # ------------------------------------------------------ тип -> місто -----
    await h.press(C, "Орендувати квартиру")
    assert "Оберіть місто" in h.last_text(C)
    assert button_texts(h.latest_with_markup(C)[1]["markup"])[:3] == ["Київ", "Івано-Франківськ", "Чернівці"]
    await h.press(C, "Київ")
    assert "Кількість кімнат" in h.last_text(C)

    # -------------------------------------------------- кімнати (мінімум 1) ---
    await h.press(C, "Далі")
    assert h.alerts[-1] == ("Оберіть хоча б один варіант.", True), h.alerts[-1]
    await h.press(C, "2")
    await h.press(C, "3")
    assert [b for b in button_texts(h.latest_with_markup(C)[1]["markup"]) if "✅" in b] == ["✅ 2", "✅ 3"]
    await h.press(C, "Далі")

    # -------------------------------------------------------------- райони ----
    assert "Київ" in h.last_text(C) and "Райони" in h.last_text(C)
    labels = button_texts(h.latest_with_markup(C)[1]["markup"])
    for d in dist.districts_for("kyiv"):
        assert d.uk in labels, d.uk
    assert "Правий берег" in labels and "Лівий берег" in labels and "Обрати все" in labels
    await h.press(C, "Печерськ")
    await h.press(C, "Лівий берег")  # усі 6 лівобережних
    selected = rentdb.get_state(C)["draft"]["districts"]
    assert "kyiv.pechersk" in selected and "kyiv.pozniaky" in selected and "kyiv.troieshchyna" in selected
    assert len(selected) == 7
    await h.press(C, "Лівий берег")  # повторне натискання знімає групу
    assert rentdb.get_state(C)["draft"]["districts"] == ["kyiv.pechersk"]
    await h.press(C, "Позняки")
    # "Назад" -> до кімнат і знову вперед: вибір зберігається
    await h.press(C, "Назад")
    assert "Кількість кімнат" in h.last_text(C)
    assert [b for b in button_texts(h.latest_with_markup(C)[1]["markup"]) if "✅" in b] == ["✅ 2", "✅ 3"]
    await h.press(C, "Далі")
    assert "✅ Печерськ" in button_texts(h.latest_with_markup(C)[1]["markup"])
    await h.press(C, "Далі")

    # -------------------------------------------------------------- бюджет ----
    assert "Бюджет" in h.last_text(C)
    budget_labels = button_texts(h.latest_with_markup(C)[1]["markup"])
    for expected in ("до 15 тис.", "15–20 тис.", "20–25 тис.", "25–30 тис.", "30–35 тис.", "35–45 тис.", "45–60 тис.", "60 тис. та більше"):
        assert expected in budget_labels, expected
    await h.press(C, "20–25 тис.")
    await h.press(C, "25–30 тис.")
    await h.press(C, "15–20 тис.")
    await h.press(C, "15–20 тис.")  # повторне натискання знімає вибір
    assert [b for b in button_texts(h.latest_with_markup(C)[1]["markup"]) if "✅" in b] == ["✅ 20–25 тис.", "✅ 25–30 тис."]
    assert rentdb.get_state(C)["draft"]["budget"] == [2, 3]
    await h.press(C, "Далі")

    # ------------------------------------------------ додаткові фільтри --------
    assert "Додаткові фільтри" in h.last_text(C)
    extras_labels = button_texts(h.latest_with_markup(C)[1]["markup"])
    for name in ("🐾 Тварини", "👶 Діти", "🏢 Поверх", "📐 Площа", "🛋 Меблі", "👤 Від кого"):
        assert name in extras_labels, name
    assert "Пропустити ⏭" in extras_labels
    await h.press(C, "Тварини")
    option_labels = button_texts(h.latest_with_markup(C)[1]["markup"])
    assert "Можна з тваринами" in option_labels and "Без тварин" in option_labels and "✅ Не важливо" in option_labels
    await h.press(C, "Можна з тваринами")
    extras_labels = button_texts(h.latest_with_markup(C)[1]["markup"])
    assert "🐾 Тварини: Можна з тваринами" in extras_labels and "▶️ Показати" in extras_labels

    # --------------------------------------------------------------- видача ---
    mark = h.mark()
    await h.press(C, "Показати")
    draft = rentdb.get_state(C)["draft"]
    expected, total = rentdb.search(
        {"type": "apartment", "city": "kyiv", "rooms": [2, 3], "districts": ["kyiv.pechersk", "kyiv.pozniaky"],
         "budget": [2, 3], "pets": "yes"}, sort="date", offset=0, limit=5)
    assert total > 5, f"потрібна пагінація, а знайдено {total}"
    sent = card_ids(h.since(mark))
    assert sent == [r["id"] for r in expected], (sent, [r["id"] for r in expected])
    first_page_texts = [p.get("text") or p.get("caption") or "" for n, p in h.since(mark) if n in ("SendMessage", "SendPhoto")]
    assert any(f"Знайдено: {total}" in t for t in first_page_texts)
    assert any(f"Показано 5 з {total}" in t for t in first_page_texts)
    more_button = [b for b in h.buttons(h.latest_with_markup(C, "Показати ще")[1]["markup"]) if "Показати ще" in b.text]
    assert more_button and more_button[0].callback_data == "more:5"
    # картка k2 (усі поля) — перевіряємо формат
    k2 = rentdb.connect().execute("SELECT id FROM listings WHERE external_id='k2'").fetchone()["id"]
    card_calls = [p for n, p in h.since(mark) if (p.get("text") or p.get("caption") or "").find(f"🆔 ID: {k2}\n") != -1]
    if card_calls:
        card = card_calls[0].get("text") or card_calls[0].get("caption")
        for needle in ("🛏 Кімнат: 2к", "📐 Площа: 62 м²", "🏢 Поверх: 5/16", "🌉 Берег: Лівий",
                       "Позняки/Осокорки", "💰 Ціна: 22 000 грн/міс · комунальні включені", "🐾 Тварини: можна",
                       "💼 Комісія: без комісії", "🔒 Застава: 1 міс", "🔥 Опалення: індивідуальне",
                       "🔋 Автономність: генератор, Starlink", "ліфт", "балкон", "🚇 Метро: Осокорки 10 хв", "💚 Без комісії"):
            assert needle in card, (needle, card)

    # другий сторінка
    mark = h.mark()
    await h.press(C, "Показати ще")
    expected2, _ = rentdb.search(
        {"type": "apartment", "city": "kyiv", "rooms": [2, 3], "districts": ["kyiv.pechersk", "kyiv.pozniaky"],
         "budget": [2, 3], "pets": "yes"}, sort="date", offset=5, limit=5)
    page2 = card_ids(h.since(mark))
    assert page2 == [r["id"] for r in expected2] and not set(page2) & set(sent)

    # --------------------------------------------------------- сортування -----
    await h.press(C, "Сортування")
    assert "Сортувати за" in h.last_text(C)
    mark = h.mark()
    await h.press(C, "Ціна ↑")
    p = prices(h.since(mark))
    assert p == sorted(p) and rentdb.get_state(C)["sort"] == "price_asc"

    # ------------------------------------------------------------- обране -----
    mid, msg = h.latest_with_markup(C, "В обране")
    fav_button = next(b for b in h.buttons(msg["markup"]) if "В обране" in b.text)
    await h.press(C, "В обране")
    assert h.alerts[-1][0] == "Додано в обране"
    assert any("В обраному" in b.text for b in h.buttons(h.messages[mid]["markup"])), "кнопка має змінитись"
    fav_id = int(fav_button.callback_data.split(":")[2])
    assert fav_id in rentdb.fav_ids(C)
    other = next(r for r in expected if r["id"] != fav_id)
    await h.tap_data(C, mid, f"fav:add:{other['id']}")
    mark = h.mark()
    await h.say(C, "/favorites")
    assert "⭐ Обране (2)" in h.last_text(C) or any("⭐ Обране (2)" in (p.get("text") or "") for n, p in h.since(mark))
    assert set(card_ids(h.since(mark))) == {fav_id, other["id"]}
    mark = h.mark()
    await h.press(C, "Порівняти")
    cmp_text = [p["text"] for n, p in h.since(mark) if n == "SendMessage"][-1]
    assert "Порівняння обраного" in cmp_text and "найдешевший" in cmp_text and "найбільший" in cmp_text
    await h.tap_data(C, mid, f"fav:del:{fav_id}")
    assert fav_id not in rentdb.fav_ids(C)

    # ------------------------------------------------- збережені фільтри -------
    await h.press(C, "Зберегти фільтр")
    assert h.alerts[-1][0].startswith("Фільтр збережено") and h.alerts[-1][1] is True
    saved = rentdb.sf_list(C)
    assert len(saved) == 1 and saved[0]["name"] == "Квартира · Київ · 2/3к · 20–30 тис." and saved[0]["notify"]
    mark = h.mark()
    await h.say(C, "/filters")
    assert any("Збережені фільтри" in (p.get("text") or "") for n, p in h.since(mark))
    assert any("📌 Квартира · Київ" in (p.get("text") or "") for n, p in h.since(mark))
    await h.press(C, "Сповіщення: так")
    assert rentdb.sf_list(C)[0]["notify"] is False
    await h.press(C, "Сповіщення: ні")
    assert rentdb.sf_list(C)[0]["notify"] is True

    # сповіщення про НОВЕ оголошення, що підходить під фільтр
    new_id = rentdb.insert_listing(listing("n1", price_uah=22500.0, area=70.0), {"district": "kyiv.pechersk", "pets": True})
    miss_id = rentdb.insert_listing(listing("n2", price_uah=90000.0), {"district": "kyiv.pechersk"})  # поза бюджетом
    mark = h.mark()
    sent_count = await notify.notify_new_listings(h.bot, [new_id, miss_id])
    assert sent_count == 1
    note = [p.get("text") or p.get("caption") or "" for n, p in h.since(mark) if n in ("SendMessage", "SendPhoto")]
    assert any("🔔 Новий об'єкт за фільтром «Квартира · Київ · 2/3к · 20–30 тис.»" in t and f"🆔 ID: {new_id}" in t for t in note)
    assert not any(f"🆔 ID: {miss_id}" in t for t in note)
    assert "🆕 Новий" in note[0], "мітка Новий після базової лінії"

    # "Вартість знижено!"
    rentdb.touch_existing([{"external_id": "n1", "price_uah": 19000.0, "price_usd": 450.0}])
    mark = h.mark()
    await h.tap_data(C, mid, f"more:0")  # перезапуск видачі, у фільтрі n1 ще підходить
    drop_cards = [p.get("text") or p.get("caption") or "" for n, p in h.since(mark) if n in ("SendMessage", "SendPhoto")]
    drops = [t for t in drop_cards if f"🆔 ID: {new_id}" in t]
    # n1 тепер 19 000 і випадає з бюджету 20–25 — перевіряємо мітку окремо через картку
    from telegam.cards import card_text
    assert "🔻 Вартість знижено!" in card_text("uk", rentdb.get_listing(new_id))
    assert "(було 22 500)" in card_text("uk", rentdb.get_listing(new_id))
    _ = drops

    # ----------------------------------------------------- порожня видача -------
    C2 = 2
    await h.say(C2, "/start")
    await h.press(C2, "Українська")
    await h.press(C2, "Орендувати будинок")
    await h.press(C2, "Чернівці")
    await h.press(C2, "1")
    await h.press(C2, "Далі")
    await h.press(C2, "Обрати все")
    await h.press(C2, "Далі")
    await h.press(C2, "60 тис. та більше")
    await h.press(C2, "Далі")
    await h.press(C2, "Пропустити")
    assert "Нічого не знайшов" in h.last_text(C2)
    assert {"📍 Усі райони", "💰 Змінити бюджет", "⚙️ Змінити фільтри"} <= set(button_texts(h.latest_with_markup(C2)[1]["markup"]))
    # будинок: у додаткових фільтрах немає "Поверх"
    await h.press(C2, "Змінити фільтри")
    assert "Ваші фільтри:" in h.last_text(C2)
    summary_labels = button_texts(h.latest_with_markup(C2)[1]["markup"])
    assert "▶️ Показати" in summary_labels and "🔄 Почати спочатку" in summary_labels
    await h.press(C2, "Додатково")
    assert "🏢 Поверх" not in button_texts(h.latest_with_markup(C2)[1]["markup"])
    # редагування міста з підсумку скидає райони і веде на вибір районів
    await h.press(C2, "Назад")  # з extras -> до бюджету? (режим редагування: повертає до підсумку)
    assert "Ваші фільтри:" in h.last_text(C2)
    await h.press(C2, "Місто")
    await h.press(C2, "Київ")
    assert "Райони" in h.last_text(C2) and rentdb.get_state(C2)["draft"]["districts"] == []
    await h.press(C2, "Обрати все")
    await h.press(C2, "Далі")
    assert "Ваші фільтри:" in h.last_text(C2), "після редагування — назад у підсумок"
    mark = h.mark()
    await h.press(C2, "Показати")
    assert card_ids(h.since(mark)) == [], "будинків у Києві нема"
    # "Почати спочатку"
    await h.press(C2, "Почати спочатку")
    assert "Що ви шукаєте" in " ".join(h.texts(C2)[-3:])
    assert rentdb.get_state(C2)["draft"]["type"] is None and rentdb.get_state(C2)["lang"] == "uk"

    # ------------------------------------------------------- англійська --------
    C3 = 3
    await h.say(C3, "/start")
    await h.press(C3, "English")
    assert any("Hi!" in t for t in h.texts(C3)) and "What are you looking for?" in h.last_text(C3)
    assert "Rent an apartment 🏠" in button_texts(h.latest_with_markup(C3)[1]["markup"])
    await h.press(C3, "Rent an apartment")
    await h.press(C3, "Kyiv")
    await h.press(C3, "1")
    await h.press(C3, "Next")
    assert "Districts — Kyiv" in h.last_text(C3)
    assert "Pechersk" in button_texts(h.latest_with_markup(C3)[1]["markup"])
    await h.say(C3, "/menu")
    assert "Main menu:" == h.last_text(C3)
    await h.say(C3, "/language")
    await h.press(C3, "Українська")
    assert "Мову змінено" in " ".join(h.texts(C3)[-3:])

    # ---------------------------------------- застарілі кнопки / запасні шляхи --
    await h.say(C, "/menu")
    alerts_before = len(h.alerts)
    districts_mid = next(m for m, v in h.messages.items() if v["chat_id"] == C and v["text"] and "Райони" in v["text"])
    await h.tap_data(C, districts_mid, "dist:1")
    assert len(h.alerts) == alerts_before + 1 and h.alerts[-1][0].startswith("Ця кнопка вже неактуальна")
    await h.say(C, "⚙️ Міста і тип нерухомості")
    assert "Головне меню" in h.last_text(C)
    await h.say(C, "щось незрозуміле")
    assert "Головне меню" in h.last_text(C)

    # адмін-статистика
    await h.say(999, "/stats")
    assert "Оголошень у базі" in h.last_text(999)
    await h.say(C, "/stats")  # звичайному користувачу — тиша
    assert "Оголошень у базі" not in h.last_text(C)

    # ------------------------------------------------------- перестраховка ----
    for markup in h.all_markups():
        for b in h.buttons(markup):
            if b.callback_data:
                assert len(b.callback_data.encode()) <= 64, b.callback_data
    state = rentdb.get_state(C)
    assert state["lang"] == "uk" and state["step"] in ("menu", "results")
    print("bot flow: усі перевірки пройшли")


if __name__ == "__main__":
    asyncio.run(main())
