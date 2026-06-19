"""
iptv.py — Parser M3U/M3U8 e gestore canali live.

Logica:
  - Scarica playlist in parallelo con aiohttp
  - Parsa #EXTINF estraendo tvg-id, tvg-name, tvg-logo, group-title
  - Deduplica per ID (priorità alla prima sorgente)
  - Cache in memoria con TTL configurabile
"""

import asyncio
import logging
import re
import time
from typing import Optional

import aiohttp

from .config import CACHE_TTL, USER_AGENT

logger = logging.getLogger(__name__)

# cache: key → (channels, timestamp)
_cache: dict[str, tuple[list[dict], float]] = {}


# ── Download ──────────────────────────────────────────────────────────────────

async def _fetch_m3u(url: str, session: aiohttp.ClientSession) -> str:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as resp:
            resp.raise_for_status()
            return await resp.text(encoding="utf-8", errors="replace")
    except Exception as e:
        logger.warning(f"⚠️  Impossibile scaricare {url}: {e}")
        return ""


# ── Parser ────────────────────────────────────────────────────────────────────

def _attr(line: str, name: str) -> str:
    """Estrae il valore di un attributo M3U dalla riga #EXTINF."""
    m = re.search(rf'{re.escape(name)}=["\']?([^"\' ]+)["\']?', line, re.IGNORECASE)
    return m.group(1) if m else ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s-]+", "-", slug).strip("-") or "ch"


def _parse_m3u(content: str, source_label: str) -> list[dict]:
    channels: list[dict] = []
    current: dict = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("#EXTINF:"):
            tvg_id   = _attr(line, "tvg-id")
            tvg_name = _attr(line, "tvg-name")
            logo     = _attr(line, "tvg-logo") or _attr(line, "logo")
            group    = _attr(line, "group-title") or "Generale"
            comma    = line.rfind(",")
            name     = line[comma + 1:].strip() if comma != -1 else (tvg_name or "Canale")
            current  = {
                "tvg_id": tvg_id,
                "name":   name or tvg_name or "Canale",
                "logo":   logo,
                "group":  group,
                "source": source_label,
            }

        elif line.startswith("#"):
            continue  # altri tag, ignora

        elif current:
            ch_id = current["tvg_id"] if current["tvg_id"] else _slugify(current["name"])
            channels.append({
                "id":         f"iptv:{ch_id}",
                "name":       current["name"],
                "logo":       current["logo"],
                "group":      current["group"],
                "stream_url": line,
                "source":     current["source"],
            })
            current = {}

    return channels


# ── Cache + caricamento ───────────────────────────────────────────────────────

async def get_all_channels(iptv_urls: list[str]) -> list[dict]:
    """Scarica, parsa e deduplica i canali da tutte le sorgenti. Usa la cache."""
    cache_key = "|".join(sorted(iptv_urls))
    if cache_key in _cache:
        cached, ts = _cache[cache_key]
        if time.time() - ts < CACHE_TTL:
            logger.debug(f"Cache hit — {len(cached)} canali")
            return cached

    headers = {"User-Agent": USER_AGENT}
    async with aiohttp.ClientSession(headers=headers) as session:
        results = await asyncio.gather(*[_fetch_m3u(u, session) for u in iptv_urls])

    all_channels: list[dict] = []
    seen: set[str] = set()

    for url, content in zip(iptv_urls, results):
        if not content:
            continue
        label = url.split("/")[-1]  # es. "iptvit.m3u"
        for ch in _parse_m3u(content, label):
            if ch["id"] not in seen:
                seen.add(ch["id"])
                all_channels.append(ch)

    logger.info(f"📺 Canali totali caricati: {len(all_channels)}")
    _cache[cache_key] = (all_channels, time.time())
    return all_channels


# ── Helpers ───────────────────────────────────────────────────────────────────

async def get_channels_page(
    iptv_urls: list[str],
    group: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> list[dict]:
    """Restituisce una pagina di canali, opzionalmente filtrata per gruppo."""
    channels = await get_all_channels(iptv_urls)
    if group and group.lower() not in ("tutti", "all", ""):
        channels = [c for c in channels if c["group"].lower() == group.lower()]
    return channels[skip: skip + limit]


async def get_channel_by_id(iptv_urls: list[str], channel_id: str) -> Optional[dict]:
    for ch in await get_all_channels(iptv_urls):
        if ch["id"] == channel_id:
            return ch
    return None


async def get_groups(iptv_urls: list[str]) -> list[str]:
    """Lista gruppi/categorie unici preservando l'ordine di apparizione."""
    seen: dict[str, None] = {}
    for ch in await get_all_channels(iptv_urls):
        seen.setdefault(ch["group"], None)
    return list(seen.keys())


def invalidate_cache() -> None:
    """Svuota la cache — utile per forzare il reload dei canali."""
    _cache.clear()
    logger.info("🗑️  Cache IPTV invalidata")
