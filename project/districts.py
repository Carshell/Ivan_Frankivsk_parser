"""Міста та райони з ТЗ (розділ 4), назви українською/англійською, аліаси для
розпізнавання району в тексті оголошення та "берег Дніпра" для Києва.

slug району має вигляд "<місто>.<район>" — стабільний ключ, який зберігається
в базі (listings.district) і в збережених фільтрах, незалежно від мови інтерфейсу.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CITIES: list[tuple[str, str, str]] = [
    ("kyiv", "Київ", "Kyiv"),
    ("ivano-frankivsk", "Івано-Франківськ", "Ivano-Frankivsk"),
    ("chernivtsi", "Чернівці", "Chernivtsi"),
]


@dataclass(frozen=True)
class District:
    slug: str
    uk: str
    en: str
    aliases: tuple[str, ...] = field(default_factory=tuple)
    bank: str | None = None  # "right" | "left" — лише для Києва

    def label(self, lang: str) -> str:
        return self.en if lang == "en" else self.uk


def _d(city: str, key: str, uk: str, en: str, aliases: tuple[str, ...], bank: str | None = None) -> District:
    return District(f"{city}.{key}", uk, en, aliases, bank)


_KYIV = (
    _d("kyiv", "lukianivka", "Лук'янівка", "Lukianivka", ("лук'янівк", "лукьяновк", "lukianivka"), "right"),
    _d("kyiv", "podil", "Поділ/Воздвиженка", "Podil / Vozdvyzhenka", ("поділ", "подол", "воздвиженк", "podil"), "right"),
    _d("kyiv", "center", "Центр", "City Center", ("центр", "хрещатик", "крещатик", "майдан незалежності"), "right"),
    _d("kyiv", "pechersk", "Печерськ", "Pechersk", ("печерськ", "печерск", "липки", "pechersk"), "right"),
    _d("kyiv", "syrets", "Сирець/Дорогожичі", "Syrets / Dorohozhychi", ("сирець", "сырец", "дорогожич", "syrets"), "right"),
    _d("kyiv", "shuliavka", "Шулявка/КПІ", "Shuliavka / KPI", ("шулявк", "кпі", "кпи", "shuliavka"), "right"),
    _d("kyiv", "nyvky", "Нивки", "Nyvky", ("нивки", "nyvky"), "right"),
    _d("kyiv", "vidradnyi", "Відрадний/Караваєві дачі", "Vidradnyi / Karavaievi Dachi", ("відрадн", "вудрадн", "караваєв", "караваев", "vidradnyi"), "right"),
    _d("kyiv", "solomianka", "Солом'янка/Чоколівка", "Solomianka / Chokolivka", ("солом'янк", "соломенк", "чоколівк", "чоколовк", "solomianka"), "right"),
    _d("kyiv", "holosiivo", "Голосіїво/Деміївка", "Holosiivo / Demiivka", ("голосіїв", "голосеев", "деміївк", "демиевк", "holosiivo"), "right"),
    _d("kyiv", "stolychne", "Столичне шосе", "Stolychne Shose", ("столичне шосе", "столичное шоссе", "феофані", "феофан"), "right"),
    _d("kyiv", "teremky", "Теремки/ВДНГ", "Teremky / VDNH", ("теремки", "вднг", "вднх", "teremky"), "right"),
    _d("kyiv", "borshchahivka", "Борщагівка", "Borshchahivka", ("борщагівк", "борщаговк", "borshchahivka"), "right"),
    _d("kyiv", "kurenivka", "Куренівка/Пріорка", "Kurenivka / Priorka", ("куренівк", "куреневк", "пріорк", "приорк", "kurenivka"), "right"),
    _d("kyiv", "akademmistechko", "Академмістечко", "Akademmistechko", ("академмістечк", "академгородок", "akademmistechko"), "right"),
    _d("kyiv", "vynohradar", "Виноградар/Мінський масив", "Vynohradar / Minskyi Masyv", ("виноградар", "мінськ", "минск", "vynohradar"), "right"),
    _d("kyiv", "obolon", "Оболонь", "Obolon", ("оболон", "obolon"), "right"),
    _d("kyiv", "troieshchyna", "Троєщина", "Troieshchyna", ("троєщин", "троещин", "troieshchyna"), "left"),
    _d("kyiv", "lisovyi", "Лісовий масив/Воскресенка", "Lisovyi Masyv / Voskresenka", ("лісовий масив", "лесной массив", "воскресенк", "voskresenka"), "left"),
    _d("kyiv", "darnytsia", "Дарниця/Русанівка", "Darnytsia / Rusanivka", ("дарниц", "русанівк", "русановк", "darnytsia"), "left"),
    _d("kyiv", "sotsmisto", "СоцМісто/Березняки", "SotsMisto / Berezniaky", ("соцміст", "соцгород", "березняки", "березняк", "berezniaky"), "left"),
    _d("kyiv", "pozniaky", "Позняки/Осокорки", "Pozniaky / Osokorky", ("позняки", "осокорки", "pozniaky", "osokorky"), "left"),
    _d("kyiv", "kharkivske", "Харківське шосе", "Kharkivske Shose", ("харківське шосе", "харьковское шоссе", "kharkivske"), "left"),
)

_IVANO = (
    _d("ivano-frankivsk", "center", "Центр", "Center", ("центр",)),
    _d("ivano-frankivsk", "kaskad", "Каскад", "Kaskad", ("каскад", "позитрон", "kaskad")),
    _d("ivano-frankivsk", "bam", "БАМ", "BAM", ("бам",)),
    _d("ivano-frankivsk", "pasichna", "Пасічна", "Pasichna", ("пасічн", "пасечн", "pasichna")),
    _d("ivano-frankivsk", "kniahynyn", "Княгинин", "Kniahynyn", ("княгинин", "kniahynyn")),
    _d("ivano-frankivsk", "braty", "Брати", "Braty", ("брати",)),
    _d("ivano-frankivsk", "vokzal", "Вокзал", "Railway Station", ("вокзал",)),
    _d("ivano-frankivsk", "sofiivka", "Софіївка", "Sofiivka", ("софіївк", "софиевк", "sofiivka")),
    _d("ivano-frankivsk", "khryplyn", "Хриплин", "Khryplyn", ("хриплин", "khryplyn")),
    _d("ivano-frankivsk", "oprysh", "Опришівці", "Opryshivtsi", ("опришівц", "опришевц", "opryshivtsi")),
    _d("ivano-frankivsk", "krykhivtsi", "Крихівці", "Krykhivtsi", ("крихівц", "крыховц", "krykhivtsi")),
    _d("ivano-frankivsk", "pidzamche", "Підзамче", "Pidzamche", ("підзамч", "подзамч", "pidzamche")),
    _d("ivano-frankivsk", "vovchynets", "Вовчинець", "Vovchynets", ("вовчинец", "вовчинц", "vovchynets")),
    _d("ivano-frankivsk", "uhryniv", "Угринів", "Uhryniv", ("угринів", "угринов", "uhryniv")),
)

_CHERNIVTSI = (
    _d("chernivtsi", "center", "Центр", "Center", ("центр",)),
    _d("chernivtsi", "prospekt", "Проспект", "Prospekt", ("проспект", "prospekt")),
    _d("chernivtsi", "sadhora", "Садгора", "Sadhora", ("садгор", "sadhora")),
    _d("chernivtsi", "rohizna", "Рогізна", "Rohizna", ("рогізн", "рогизн", "rohizna")),
    _d("chernivtsi", "rosha", "Роша", "Rosha", ("роша", "rosha")),
    _d("chernivtsi", "kalichanka", "Калічанка", "Kalichanka", ("калічанк", "каличанк", "kalichanka")),
    _d("chernivtsi", "klokuchka", "Клокучка", "Klokuchka", ("клокучк", "klokuchka")),
    _d("chernivtsi", "hraviton", "Гравітон", "Hraviton", ("гравітон", "гравитон", "hraviton")),
    _d("chernivtsi", "horecha", "Горече", "Horecha", ("горече", "горечи", "horecha")),
    _d("chernivtsi", "tsetsyno", "Цецино", "Tsetsyno", ("цецин", "tsetsyno")),
    _d("chernivtsi", "pershotravnevyi", "Першотравневий", "Pershotravnevyi", ("першотравнев", "первомайск", "pershotravnevyi")),
)

DISTRICTS: dict[str, tuple[District, ...]] = {
    "kyiv": _KYIV,
    "ivano-frankivsk": _IVANO,
    "chernivtsi": _CHERNIVTSI,
}

_BY_SLUG: dict[str, District] = {d.slug: d for ds in DISTRICTS.values() for d in ds}

_APOSTROPHES = re.compile(r"[’ʼ`´‘]")


def normalize_text(text: str) -> str:
    return _APOSTROPHES.sub("'", text.lower().replace("ё", "е"))


def city_label(code: str, lang: str) -> str:
    for c, uk, en in CITIES:
        if c == code:
            return en if lang == "en" else uk
    return code


def districts_for(city: str) -> tuple[District, ...]:
    return DISTRICTS.get(city, ())


def get_district(slug: str | None) -> District | None:
    return _BY_SLUG.get(slug) if slug else None


def district_label(slug: str | None, lang: str) -> str | None:
    d = get_district(slug)
    return d.label(lang) if d else None


def bank_of(slug: str | None) -> str | None:
    d = get_district(slug)
    return d.bank if d else None


def slugs_by_bank(city: str, bank: str) -> list[str]:
    return [d.slug for d in districts_for(city) if d.bank == bank]


def guess_district(city: str | None, text: str | None) -> str | None:
    """Район за ключовими словами в адресі/тексті — запасний варіант, коли
    Claude не визначив район. Перемагає найдовший збіг (щоб "харківське шосе"
    не з'їв короткий аліас на кшталт "центр")."""
    if not city or not text:
        return None
    haystack = normalize_text(text)
    best: tuple[int, str] | None = None
    for d in districts_for(city):
        for alias in d.aliases:
            if len(alias) <= 3:
                # короткі аліаси ("бам", "кпі") — лише як окреме слово
                pattern = rf"(?<![\w']){re.escape(alias)}(?![\w'])"
            else:
                pattern = rf"(?<![\w']){re.escape(alias)}"
            if re.search(pattern, haystack):
                if best is None or len(alias) > best[0]:
                    best = (len(alias), d.slug)
    return best[1] if best else None
