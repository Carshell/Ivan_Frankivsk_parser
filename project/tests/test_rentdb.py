"""Перевірка rentdb на тимчасовій базі: python tests/test_rentdb.py"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp(prefix="rentdb_test_"))
os.environ["RENT_DB_PATH"] = str(TMP / "rent.db")

import rentdb  # noqa: E402


def listing(ext, city="kyiv", ptype="apartment", **kw):
    base = {
        "external_id": ext, "source": "test", "url": f"https://x/{ext}", "origin_site": "test",
        "city": city, "property_type": ptype, "title": f"Оренда {ext}", "description": "", "address": None,
        "price_uah": 20000.0, "price_usd": 480.0, "rooms": 2, "area": 55.0, "floor": 3, "floor_total": 9,
        "photos": ["https://x/p.jpg"],
    }
    base.update(kw)
    return base


def ids(rows):
    return {r["external_id"] for r in rows}


def main():
    rentdb.connect()
    assert not rentdb.is_bootstrapped()

    # --- вставка + район з витягу Claude / з тексту -------------------------
    a = rentdb.insert_listing(listing("a"), {"district": "kyiv.pechersk", "pets": True, "furnished": True,
                                             "from_owner": True, "autonomy": ["generator"], "heating": "central"})
    b = rentdb.insert_listing(listing("b", address="Київ, Позняки, вул. Драгоманова 5", price_uah=32000.0, rooms=3, area=80.0), {})
    c = rentdb.insert_listing(listing("c", price_uah=None, rooms=None, area=None, floor=None, floor_total=None), {})
    d = rentdb.insert_listing(listing("d", city="ivano-frankivsk", ptype="house", price_uah=60000.0, rooms=5, area=160.0), {"pets": False})
    assert all([a, b, c, d])
    assert rentdb.insert_listing(listing("a")) is None, "дубль external_id має ігноруватись"
    assert rentdb.insert_listing(listing("zz", city=None)) is None
    la = rentdb.get_listing(a)
    assert la["district"] == "kyiv.pechersk" and la["bank"] == "right" and la["pets"] == 1 and la["autonomy"] == ["generator"]
    lb = rentdb.get_listing(b)
    assert lb["district"] == "kyiv.pozniaky" and lb["bank"] == "left", lb["district"]
    assert rentdb.get_listing(d)["pets"] == 0 and rentdb.get_listing(d)["bank"] is None

    # --- пошук -------------------------------------------------------------
    base = {"type": "apartment", "city": "kyiv"}
    rows, total = rentdb.search(base)
    assert ids(rows) == {"a", "b", "c"} and total == 3
    rows, _ = rentdb.search({**base, "rooms": [2]})
    assert ids(rows) == {"a", "c"}, "rooms=2 + оголошення без кімнат (лояльність)"
    rows, _ = rentdb.search({**base, "rooms": [4]})
    assert ids(rows) == {"c"}
    rows, _ = rentdb.search({**base, "rooms": [3, 4]})
    assert ids(rows) == {"b", "c"}
    rows, _ = rentdb.search({**base, "districts": ["kyiv.pechersk"]})
    assert ids(rows) == {"a", "c"}, "c без району проходить"
    all_slugs = [x.slug for x in __import__("districts").districts_for("kyiv")]
    rows, _ = rentdb.search({**base, "districts": all_slugs})
    assert ids(rows) == {"a", "b", "c"}, "обрати все = без фільтра району"
    rows, _ = rentdb.search({**base, "budget": 2})  # 20-25 тис.
    assert ids(rows) == {"a", "c"}
    rows, _ = rentdb.search({**base, "budget": 7})  # 60+
    assert ids(rows) == {"c"}
    rows, _ = rentdb.search({**base, "pets": "no"})
    assert ids(rows) == {"b", "c"}, "pets=no: a явно дозволяє тварин -> відсіюється"
    rows, _ = rentdb.search({**base, "pets": "yes", "furn": "yes", "from": "owner"})
    assert ids(rows) == {"a", "b", "c"}
    rows, _ = rentdb.search({**base, "floor": "not_edge"})
    assert ids(rows) == {"a", "b", "c"}
    rentdb.insert_listing(listing("top", floor=9, floor_total=9, rooms=2), {})
    rentdb.insert_listing(listing("first", floor=1, floor_total=9, rooms=2), {})
    rows, _ = rentdb.search({**base, "floor": "not_edge"})
    assert "top" not in ids(rows) and "first" not in ids(rows)
    rows, _ = rentdb.search({**base, "area": 3})  # 70-100
    assert ids(rows) == {"b", "c"}
    # будинок: фільтр поверху ігнорується
    rows, _ = rentdb.search({"type": "house", "city": "ivano-frankivsk", "floor": "not_first"})
    assert ids(rows) == {"d"}
    # інше місто/тип не змішуються
    rows, _ = rentdb.search({"type": "house", "city": "kyiv"})
    assert not rows

    # --- сортування і пагінація -------------------------------------------
    rows, _ = rentdb.search({**base, "rooms": [2, 3]}, sort="price_asc")
    prices = [r["price_uah"] for r in rows]
    assert prices[-1] is None or prices == sorted(p for p in prices if p is not None) + [p for p in prices if p is None]
    rows, total = rentdb.search(base, sort="price_desc", limit=2, offset=0)
    assert len(rows) == 2 and total == 5 and rows[0]["external_id"] == "b"
    rows2, _ = rentdb.search(base, sort="price_desc", limit=2, offset=2)
    assert not (ids(rows) & ids(rows2))
    rows, _ = rentdb.search(base, sort="area", limit=1)
    assert rows[0]["external_id"] == "b"

    # --- зміна ціни: знижено / підвищено -----------------------------------
    r = rentdb.touch_existing([{"external_id": "a", "price_uah": 18000.0, "price_usd": 430.0}])
    assert r == {"touched": 1, "dropped": 1}
    la = rentdb.get_listing(a)
    assert la["price_uah"] == 18000.0 and la["prev_price_uah"] == 20000.0 and rentdb.is_price_dropped(la)
    rentdb.touch_existing([{"external_id": "a", "price_uah": 18050.0}])  # <1% — шум
    assert rentdb.get_listing(a)["prev_price_uah"] == 20000.0
    rentdb.touch_existing([{"external_id": "a", "price_uah": 25000.0, "price_usd": 600.0}])
    la = rentdb.get_listing(a)
    assert la["prev_price_uah"] is None and not rentdb.is_price_dropped(la)

    # --- "Новий": лише після базової лінії ---------------------------------
    assert not rentdb.is_new(rentdb.get_listing(b))
    rentdb.mark_bootstrapped()
    assert rentdb.is_bootstrapped() and not rentdb.is_new(rentdb.get_listing(b))
    time.sleep(0.01)
    n = rentdb.insert_listing(listing("fresh"), {})
    assert rentdb.is_new(rentdb.get_listing(n))

    # --- only_ids (сповіщення) ---------------------------------------------
    rows, total = rentdb.search(base, only_ids=[n, b])
    assert ids(rows) == {"fresh", "b"} and total == 2
    assert rentdb.search(base, only_ids=[])[1] == 0

    # --- обране --------------------------------------------------------------
    rentdb.fav_add(1, a); rentdb.fav_add(1, b); rentdb.fav_add(1, a)
    assert set(rentdb.fav_ids(1)) == {a, b}
    page, total = rentdb.fav_page(1, 0, 1)
    assert total == 2 and len(page) == 1
    rentdb.fav_remove(1, a)
    assert rentdb.fav_ids(1) == [b]

    # --- збережені фільтри ---------------------------------------------------
    fid = rentdb.sf_create(1, "Київ", {"type": "apartment", "city": "kyiv"})
    assert rentdb.sf_count(1) == 1 and rentdb.sf_get(1, fid)["notify"] is True
    assert rentdb.sf_get(2, fid) is None, "чужий фільтр недоступний"
    assert rentdb.sf_toggle_notify(1, fid) is False
    assert rentdb.sf_all_notifying() == []
    assert rentdb.sf_toggle_notify(1, fid) is True and len(rentdb.sf_all_notifying()) == 1
    rentdb.sf_delete(1, fid)
    assert rentdb.sf_list(1) == []

    # --- користувач / стан ---------------------------------------------------
    st = rentdb.ensure_user(7)
    assert st["lang"] is None and st["draft"] == {} and st["sort"] == "date"
    rentdb.save_state(7, lang="en", step="city", draft={"type": "house", "rooms": [1, 4]}, sort="price_asc")
    st = rentdb.get_state(7)
    assert st == {"chat_id": 7, "lang": "en", "step": "city", "draft": {"type": "house", "rooms": [1, 4]}, "sort": "price_asc"}
    try:
        rentdb.save_state(7, hack="x")
        raise SystemExit("очікувалась помилка для невідомого поля")
    except ValueError:
        pass

    # --- актуальність/очищення ---------------------------------------------
    conn = rentdb.connect()
    conn.execute("UPDATE listings SET last_seen_at = ? WHERE external_id = 'c'", (time.time() - 20 * 86400,))
    conn.commit()
    rows, _ = rentdb.search(base)
    assert "c" not in ids(rows), "старіше ACTIVE_LISTING_DAYS не показується"
    conn.execute("UPDATE listings SET last_seen_at = ?, photos = ? WHERE external_id = 'c'",
                 (time.time() - 90 * 86400, json.dumps(["media_cache/x.jpg", "https://x/y.jpg"])))
    conn.commit()
    files = rentdb.prune_old()
    assert files == ["media_cache/x.jpg"] and rentdb.existing_external_ids(["c", "a"]) == {"a"}

    # --- міграція зі старого subscribers.json ------------------------------
    legacy = TMP / "subscribers.json"
    legacy.write_text(json.dumps({
        "100": {"paused": False, "cities": ["ivano-frankivsk"], "property_types": ["house"]},
        "200": {"paused": False},
        "300": {"paused": True, "cities": ["kyiv"], "property_types": ["apartment"]},
    }), encoding="utf-8")
    rentdb.LEGACY_SUBSCRIBERS = legacy
    assert rentdb.migrate_legacy_subscribers() == 2
    assert [f["filters"] for f in rentdb.sf_list(100)] == [{"type": "house", "city": "ivano-frankivsk"}]
    assert rentdb.sf_list(200) == [] and rentdb.sf_list(300) == []
    assert rentdb.get_state(100)["lang"] == "uk"
    assert rentdb.migrate_legacy_subscribers() == 0, "міграція одноразова"

    print("rentdb: усі перевірки пройшли")
    rentdb.close()


if __name__ == "__main__":
    main()
