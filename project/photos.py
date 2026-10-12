"""Підміна URL фото на версію найвищої якості, яку віддає CDN джерела.

Парсери (JSON-LD, картки пошуку) часто віддають мініатюри: Flatfy — 360×360,
OLX — без параметра розміру (≈466×700), dom.ria — стиснутий webp. Функція
ідемпотентна й чіпає лише відомі CDN, тому безпечна для будь-яких URL
(локальні файли Telegram/Instagram проходять без змін)."""

from __future__ import annotations

import re

HIRES = 1200  # lunstatic віддає до 1200×1200; Telegram все одно зменшує до 1280 px

_LUN_RE = re.compile(r"(market-images\.lunstatic\.net/[^/]+/)(\d+)/(\d+)(/images/)")
_OLX_SIZE_RE = re.compile(r";s=\d+x\d+")
_OLX_MAX = "1600x1600"
_RIA_RE = re.compile(r"(cdn\.riastatic\.com/photos/.*xl)\.webp$")


def _lun(url: str) -> str:
    def repl(m: re.Match[str]) -> str:
        w, h = int(m.group(2)), int(m.group(3))
        return f"{m.group(1)}{max(w, HIRES)}/{max(h, HIRES)}{m.group(4)}"
    return _LUN_RE.sub(repl, url)


def hires(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return url
    if "lunstatic.net" in url:
        return _lun(url)
    if "olxcdn.com" in url and "/files/" in url:
        base = _OLX_SIZE_RE.sub("", url)
        return f"{base};s={_OLX_MAX}"
    if "riastatic.com" in url:
        return _RIA_RE.sub(r"\1.jpg", url)
    return url


def hires_all(urls: list[str] | None) -> list[str]:
    return [hires(u) for u in (urls or [])]
