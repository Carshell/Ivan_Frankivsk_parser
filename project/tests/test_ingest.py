"""Перевірка main.ingest із підміненим Claude: python tests/test_ingest.py"""
import asyncio, os, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["RENT_DB_PATH"] = str(Path(tempfile.mkdtemp()) / "rent.db")
import rentdb, main, claude_extract  # noqa: E402

calls = []
async def fake_extract(listing):
    calls.append(listing["external_id"])
    if listing["external_id"] == "bad":
        return None
    return {"is_rental": listing["external_id"] != "notrent", "district": "kyiv.obolon", "pets": True}
claude_extract.extract = fake_extract
main.claude_extract.extract = fake_extract

def L(ext, **kw):
    d = {"external_id": ext, "source": "t", "url": "https://x/" + ext, "origin_site": "t", "city": "kyiv",
         "property_type": "apartment", "title": "Оренда квартири", "description": "", "address": "вул. Тестова 1",
         "price_uah": 20000.0, "price_usd": 480.0, "rooms": 2, "area": 50.0, "floor": 2, "floor_total": 9, "photos": []}
    d.update(kw); return d

async def run():
    rentdb.connect()
    batch = [L("a"), L("b", address="вул. Інша 5", price_uah=31000.0), L("sale", title="Продаж квартири"),
             L("notrent", address="пр. Мира 7", price_uah=15000.0), L("bad", address="пр. Бандери 9", price_uah=26000.0)]
    stats, new_ids = await main.ingest(batch)
    assert len(new_ids) == stats['inserted'] == 3, (stats, new_ids)
    print(stats)
    assert stats["sale_rejected"] == 1 and stats["not_rental"] == 1
    assert rentdb.get_listing(1) is not None
    ids = rentdb.existing_external_ids(["a", "b", "sale", "notrent", "bad"])
    assert {"a", "b", "sale", "notrent", "bad"} <= set(ids), ids
    n = len(calls)
    stats2, new2 = await main.ingest([L("a", price_uah=18000.0), L("sale", title="Продаж квартири")])
    assert len(calls) == n + 1 or len(calls) == n, calls  # лише ретрай "bad"
    assert stats2["new"] == 0 and stats2["price_drops"] == 1, stats2
    print("ingest: усі перевірки пройшли")
asyncio.run(run())
