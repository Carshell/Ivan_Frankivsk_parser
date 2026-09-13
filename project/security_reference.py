"""Довідник потенційно небезпечних об'єктів і розрахунок відстаней (п.5 ТЗ).

security_objects.json ведеться вручну (див. security_objects.README.md) —
тут лише читання файлу й геометрія, жодних вигаданих координат.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
OBJECTS_FILE = BASE_DIR / "security_objects.json"

MAX_DISTANCE_KM = 30
MAX_OBJECTS = 8


def _load_objects() -> list[dict[str, Any]]:
    if not OBJECTS_FILE.exists():
        return []
    try:
        return json.loads(OBJECTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearby_objects(lat: float | None, lon: float | None) -> list[dict[str, Any]]:
    """Об'єкти в радіусі MAX_DISTANCE_KM від точки, найближчі перші.

    Порожній список — або немає координат оголошення, або довідник ще
    порожній (нормальний стан, поки security_objects.json не заповнений).
    """
    if lat is None or lon is None:
        return []

    result = []
    for obj in _load_objects():
        try:
            dist = _haversine_km(lat, lon, obj["lat"], obj["lon"])
        except (KeyError, TypeError):
            continue
        if dist <= MAX_DISTANCE_KM:
            result.append({"name": obj.get("name"), "type": obj.get("type"), "distance_km": round(dist, 1)})

    result.sort(key=lambda o: o["distance_km"])
    return result[:MAX_OBJECTS]
