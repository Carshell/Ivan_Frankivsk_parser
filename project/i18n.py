"""Тексти інтерфейсу бота: українська та англійська (ТЗ, п.1)."""

from __future__ import annotations

DEFAULT_LANG = "uk"
LANGS = ("uk", "en")

# Діапазони бюджету (грн/міс) і площі (м²) з ТЗ, п.3. (мін, макс) — макс None = без верхньої межі.
BUDGET_RANGES: list[tuple[int, int | None]] = [
    (0, 15000), (15000, 20000), (20000, 25000), (25000, 30000),
    (30000, 35000), (35000, 45000), (45000, 60000), (60000, None),
]
AREA_RANGES: list[tuple[int, int | None]] = [(0, 35), (35, 50), (50, 70), (70, 100), (100, None)]

STRINGS: dict[str, dict[str, str]] = {
    "uk": {
        # --- загальне ---
        "choose_lang": "🌐 Оберіть мову / Choose your language",
        "lang_uk": "🇺🇦 Українська",
        "lang_en": "🇬🇧 English",
        "lang_set": "Мову змінено на українську 🇺🇦",
        "welcome": (
            "Привіт! 👋 Я допоможу знайти житло в оренду в Києві, Івано-Франківську та Чернівцях.\n"
            "Усе робиться кнопками — просто обирайте варіанти. На кожному кроці є «Назад» і «Почати спочатку»."
        ),
        "btn_back": "⬅️ Назад",
        "btn_restart": "🔄 Почати спочатку",
        "btn_next": "Далі ➡️",
        "btn_done": "✅ Готово",
        "btn_skip": "Пропустити ⏭",
        "all_plain": "Обрати все",
        "btn_any": "Не важливо",
        "footer_actions": "Що далі?",
        "stale": "Ця кнопка вже неактуальна. Відкрийте /menu.",
        "need_one": "Оберіть хоча б один варіант.",
        "menu_title": "Головне меню:",
        "menu_search": "🔎 Новий пошук",
        "menu_favs": "⭐ Обране",
        "menu_filters": "💾 Мої фільтри",
        "menu_lang": "🌐 Мова",
        "menu_kb": "☰ Меню",
        "cmd_start": "Почати пошук",
        "cmd_menu": "Головне меню",
        "cmd_filters": "Збережені фільтри",
        "cmd_favorites": "Обране",
        "cmd_language": "Змінити мову",
        # --- кроки ---
        "step_type": "Що ви шукаєте?",
        "type_apartment": "Орендувати квартиру 🏠",
        "type_house": "Орендувати будинок 🏡",
        "type_name_apartment": "Квартира",
        "type_name_house": "Будинок",
        "step_city": "Оберіть місто:",
        "step_rooms": "Кількість кімнат (можна обрати кілька):",
        "step_districts": "Райони — {city} (можна обрати кілька):",
        "districts_bank": "За берегом Дніпра:",
        "bank_right": "Правий берег",
        "bank_left": "Лівий берег",
        "step_budget": "Бюджет, грн/міс (можна обрати кілька):",
        "step_extras": "Додаткові фільтри (цей крок можна пропустити):",
        "step_summary": "Ваші фільтри:",
        "btn_show": "▶️ Показати",
        # --- додаткові фільтри ---
        "x_pets": "🐾 Тварини",
        "x_kids": "👶 Діти",
        "x_floor": "🏢 Поверх",
        "x_area": "📐 Площа",
        "x_furn": "🛋 Меблі",
        "x_from": "👤 Від кого",
        "pets_yes": "Можна з тваринами",
        "pets_no": "Без тварин",
        "kids_yes": "Можна з дітьми",
        "kids_no": "Без дітей",
        "floor_any": "Будь-який",
        "floor_not_first": "Не перший",
        "floor_not_last": "Не останній",
        "floor_not_edge": "Не перший і не останній",
        "furn_yes": "З меблями і технікою",
        "furn_no": "Без меблів",
        "from_owner": "Від власника",
        "from_agency": "Від агенції",
        "x_pick": "{name}: оберіть значення",
        # --- сводка фільтрів ---
        "s_type": "Тип",
        "s_city": "Місто",
        "s_rooms": "Кімнати",
        "s_districts": "Райони",
        "s_budget": "Бюджет",
        "s_extras": "Додатково",
        "all_districts": "усі",
        "not_set": "—",
        "unit_uah": "грн/міс",
        "budget_up_to": "до {n} тис.",
        "budget_range": "{a}–{b} тис.",
        "budget_from": "{n} тис. та більше",
        "area_up_to": "до {n}",
        "area_range": "{a}–{b}",
        "area_from": "{n}+",
        # --- видача ---
        "searching": "🔎 Шукаю…",
        "results_header": "Знайдено: {total}. Показую {a}–{b}.",
        "results_footer": "Показано {b} з {total}.",
        "btn_more": "➕ Показати ще",
        "btn_change_filters": "⚙️ Змінити фільтри",
        "btn_sort": "↕️ Сортування",
        "btn_save": "🔔 Зберегти фільтр і сповіщати",
        "sort_pick": "Сортувати за:",
        "sort_price_asc": "Ціна ↑",
        "sort_price_desc": "Ціна ↓",
        "sort_date": "Дата (новіші)",
        "sort_area": "Площа ↓",
        "empty": "Нічого не знайшов за цими фільтрами 😕\nСпробуйте розширити райони або бюджет.",
        "btn_all_districts": "📍 Усі райони",
        "btn_change_budget": "💰 Змінити бюджет",
        "nothing_more": "Це всі оголошення за вашими фільтрами.",
        # --- картка ---
        "btn_contact": "📞 Зв'язатися",
        "btn_manager": "💬 Менеджер",
        "btn_fav_add": "⭐ В обране",
        "btn_fav_del": "✅ В обраному",
        "fav_added": "Додано в обране",
        "fav_removed": "Прибрано з обраного",
        "c_id": "ID",
        "c_rooms": "Кімнат",
        "c_area": "Площа",
        "c_floor": "Поверх",
        "c_bank": "Берег",
        "c_district": "Район",
        "c_price": "Ціна",
        "c_pets": "Тварини",
        "c_kids": "Діти",
        "c_extra": "Додатково",
        "c_commission": "Комісія",
        "c_deposit": "Застава",
        "c_heating": "Опалення",
        "c_autonomy": "Автономність",
        "c_metro": "Метро",
        "c_currency": "Валюта ціни",
        "allowed": "можна",
        "not_allowed": "не можна",
        "utilities_in": "комунальні включені",
        "utilities_out": "комунальні окремо",
        "was_price": "було {price}",
        "label_price_drop": "🔻 Вартість знижено!",
        "label_new": "🆕 Новий",
        "label_no_commission": "💚 Без комісії",
        "no_commission": "без комісії",
        "with_commission": "з комісією",
        "no_deposit": "без застави",
        "heating_central": "центральне",
        "heating_individual": "індивідуальне",
        "au_generator": "генератор",
        "au_inverter": "інвертор",
        "au_starlink": "Starlink",
        "au_shelter": "укриття поруч",
        "au_parking": "паркінг поруч",
        "am_elevator": "ліфт",
        "am_balcony": "балкон",
        "am_parking": "паркінг",
        "am_ac": "кондиціонер",
        "am_internet": "інтернет",
        "rooms_short": "{n}к",
        "area_unit": "м²",
        # --- обране ---
        "favs_title": "⭐ Обране ({n})",
        "favs_empty": "В обраному поки порожньо. Додавайте об'єкти кнопкою «⭐ В обране».",
        "btn_compare": "⚖️ Порівняти",
        "compare_title": "⚖️ Порівняння обраного:",
        "compare_need_two": "Для порівняння потрібно щонайменше 2 обрані об'єкти.",
        "compare_cheapest": "найдешевший",
        "compare_largest": "найбільший",
        # --- збережені фільтри ---
        "sf_title": "💾 Збережені фільтри:",
        "sf_empty": "Збережених фільтрів ще немає. Зробіть пошук і натисніть «🔔 Зберегти фільтр і сповіщати».",
        "sf_saved": "Фільтр збережено 🔔 Я повідомлю про нові об'єкти за ним.",
        "sf_limit": "Можна зберегти до {n} фільтрів. Видаліть зайвий у /filters.",
        "sf_notify_on": "🔔 Сповіщення: так",
        "sf_notify_off": "🔕 Сповіщення: ні",
        "sf_show": "▶️ Показати",
        "sf_delete": "🗑 Видалити",
        "sf_deleted": "Фільтр видалено",
        "notify_new": "🔔 Новий об'єкт за фільтром «{name}»:",
        "notify_more": "…і ще {n} нових за фільтром «{name}». Відкрийте /filters, щоб переглянути.",
    },
    "en": {
        "choose_lang": "🌐 Оберіть мову / Choose your language",
        "lang_uk": "🇺🇦 Українська",
        "lang_en": "🇬🇧 English",
        "lang_set": "Language set to English 🇬🇧",
        "welcome": (
            "Hi! 👋 I'll help you find a rental in Kyiv, Ivano-Frankivsk and Chernivtsi.\n"
            "Everything works with buttons — just pick options. Every step has «Back» and «Start over»."
        ),
        "btn_back": "⬅️ Back",
        "btn_restart": "🔄 Start over",
        "btn_next": "Next ➡️",
        "btn_done": "✅ Done",
        "btn_skip": "Skip ⏭",
        "all_plain": "Select all",
        "btn_any": "Doesn't matter",
        "footer_actions": "What next?",
        "stale": "This button is no longer valid. Open /menu.",
        "need_one": "Please pick at least one option.",
        "menu_title": "Main menu:",
        "menu_search": "🔎 New search",
        "menu_favs": "⭐ Favorites",
        "menu_filters": "💾 My filters",
        "menu_lang": "🌐 Language",
        "menu_kb": "☰ Menu",
        "cmd_start": "Start a search",
        "cmd_menu": "Main menu",
        "cmd_filters": "Saved filters",
        "cmd_favorites": "Favorites",
        "cmd_language": "Change language",
        "step_type": "What are you looking for?",
        "type_apartment": "Rent an apartment 🏠",
        "type_house": "Rent a house 🏡",
        "type_name_apartment": "Apartment",
        "type_name_house": "House",
        "step_city": "Choose a city:",
        "step_rooms": "Number of rooms (you can pick several):",
        "step_districts": "Districts — {city} (you can pick several):",
        "districts_bank": "By the Dnipro bank:",
        "bank_right": "Right bank",
        "bank_left": "Left bank",
        "step_budget": "Budget, UAH/month (you can pick several):",
        "step_extras": "Additional filters (you can skip this step):",
        "step_summary": "Your filters:",
        "btn_show": "▶️ Show",
        "x_pets": "🐾 Pets",
        "x_kids": "👶 Children",
        "x_floor": "🏢 Floor",
        "x_area": "📐 Area",
        "x_furn": "🛋 Furniture",
        "x_from": "👤 Listed by",
        "pets_yes": "Pets allowed",
        "pets_no": "No pets",
        "kids_yes": "Children allowed",
        "kids_no": "No children",
        "floor_any": "Any",
        "floor_not_first": "Not the first",
        "floor_not_last": "Not the last",
        "floor_not_edge": "Not first and not last",
        "furn_yes": "Furnished with appliances",
        "furn_no": "Unfurnished",
        "from_owner": "From owner",
        "from_agency": "From agency",
        "x_pick": "{name}: choose a value",
        "s_type": "Type",
        "s_city": "City",
        "s_rooms": "Rooms",
        "s_districts": "Districts",
        "s_budget": "Budget",
        "s_extras": "Extras",
        "all_districts": "all",
        "not_set": "—",
        "unit_uah": "UAH/month",
        "budget_up_to": "up to {n}k",
        "budget_range": "{a}–{b}k",
        "budget_from": "{n}k and more",
        "area_up_to": "up to {n}",
        "area_range": "{a}–{b}",
        "area_from": "{n}+",
        "searching": "🔎 Searching…",
        "results_header": "Found: {total}. Showing {a}–{b}.",
        "results_footer": "Shown {b} of {total}.",
        "btn_more": "➕ Show more",
        "btn_change_filters": "⚙️ Change filters",
        "btn_sort": "↕️ Sort",
        "btn_save": "🔔 Save filter & get alerts",
        "sort_pick": "Sort by:",
        "sort_price_asc": "Price ↑",
        "sort_price_desc": "Price ↓",
        "sort_date": "Date (newest)",
        "sort_area": "Area ↓",
        "empty": "Nothing found for these filters 😕\nTry widening the districts or the budget.",
        "btn_all_districts": "📍 All districts",
        "btn_change_budget": "💰 Change budget",
        "nothing_more": "That's everything for your filters.",
        "btn_contact": "📞 Contact",
        "btn_manager": "💬 Manager",
        "btn_fav_add": "⭐ Favorite",
        "btn_fav_del": "✅ In favorites",
        "fav_added": "Added to favorites",
        "fav_removed": "Removed from favorites",
        "c_id": "ID",
        "c_rooms": "Rooms",
        "c_area": "Area",
        "c_floor": "Floor",
        "c_bank": "Bank",
        "c_district": "District",
        "c_price": "Price",
        "c_pets": "Pets",
        "c_kids": "Children",
        "c_extra": "Extra",
        "c_commission": "Commission",
        "c_deposit": "Deposit",
        "c_heating": "Heating",
        "c_autonomy": "Backup power",
        "c_metro": "Metro",
        "c_currency": "Price currency",
        "allowed": "allowed",
        "not_allowed": "not allowed",
        "utilities_in": "utilities included",
        "utilities_out": "utilities not included",
        "was_price": "was {price}",
        "label_price_drop": "🔻 Price reduced!",
        "label_new": "🆕 New",
        "label_no_commission": "💚 No commission",
        "no_commission": "no commission",
        "with_commission": "with commission",
        "no_deposit": "no deposit",
        "heating_central": "central",
        "heating_individual": "individual",
        "au_generator": "generator",
        "au_inverter": "inverter",
        "au_starlink": "Starlink",
        "au_shelter": "shelter nearby",
        "au_parking": "parking nearby",
        "am_elevator": "elevator",
        "am_balcony": "balcony",
        "am_parking": "parking",
        "am_ac": "air conditioning",
        "am_internet": "internet",
        "rooms_short": "{n}r",
        "area_unit": "m²",
        "favs_title": "⭐ Favorites ({n})",
        "favs_empty": "No favorites yet. Add listings with the «⭐ Favorite» button.",
        "btn_compare": "⚖️ Compare",
        "compare_title": "⚖️ Favorites comparison:",
        "compare_need_two": "You need at least 2 favorites to compare.",
        "compare_cheapest": "cheapest",
        "compare_largest": "largest",
        "sf_title": "💾 Saved filters:",
        "sf_empty": "No saved filters yet. Run a search and tap «🔔 Save filter & get alerts».",
        "sf_saved": "Filter saved 🔔 I'll notify you about new listings matching it.",
        "sf_limit": "You can save up to {n} filters. Delete one in /filters.",
        "sf_notify_on": "🔔 Alerts: on",
        "sf_notify_off": "🔕 Alerts: off",
        "sf_show": "▶️ Show",
        "sf_delete": "🗑 Delete",
        "sf_deleted": "Filter deleted",
        "notify_new": "🔔 New listing for filter «{name}»:",
        "notify_more": "…and {n} more new ones for filter «{name}». Open /filters to view them.",
    },
}


def t(lang: str | None, key: str, **kwargs) -> str:
    table = STRINGS.get(lang or DEFAULT_LANG) or STRINGS[DEFAULT_LANG]
    text = table.get(key)
    if text is None:
        text = STRINGS[DEFAULT_LANG].get(key, key)
    return text.format(**kwargs) if kwargs else text


def budget_label(lang: str, index: int) -> str:
    lo, hi = BUDGET_RANGES[index]
    if lo == 0 and hi is not None:
        return t(lang, "budget_up_to", n=hi // 1000)
    if hi is None:
        return t(lang, "budget_from", n=lo // 1000)
    return t(lang, "budget_range", a=lo // 1000, b=hi // 1000)


def budget_indices(value) -> list[int]:
    """Обраний бюджет як відсортований список індексів діапазонів. Приймає і
    старий формат (одне число) зі збережених фільтрів."""
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return sorted({int(i) for i in items if 0 <= int(i) < len(BUDGET_RANGES)})


def budget_labels(lang: str, value) -> str:
    """Підпис бюджету; сусідні діапазони зливаються: 15–20 + 20–25 → 15–25 тис."""
    indices = budget_indices(value)
    groups: list[list[int]] = []
    for index in indices:
        if groups and index == groups[-1][-1] + 1:
            groups[-1].append(index)
        else:
            groups.append([index])
    labels = []
    for group in groups:
        lo, hi = BUDGET_RANGES[group[0]][0], BUDGET_RANGES[group[-1]][1]
        if lo == 0 and hi is not None:
            labels.append(t(lang, "budget_up_to", n=hi // 1000))
        elif hi is None:
            labels.append(t(lang, "budget_from", n=lo // 1000))
        else:
            labels.append(t(lang, "budget_range", a=lo // 1000, b=hi // 1000))
    return ", ".join(labels)


def area_label(lang: str, index: int) -> str:
    lo, hi = AREA_RANGES[index]
    if lo == 0 and hi is not None:
        return t(lang, "area_up_to", n=hi)
    if hi is None:
        return t(lang, "area_from", n=lo)
    return t(lang, "area_range", a=lo, b=hi)
