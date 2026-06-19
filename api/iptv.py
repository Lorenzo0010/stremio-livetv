"""
iptv.py — Parser M3U/M3U8 e gestore canali live.

Fix applicati analizzando le playlist reali:
  1. Attributi M3U senza spazio tra di loro (es. tvg-id="X"tvg-logo="Y")
     — il regex precedente li gestiva correttamente, ma il formato
       group-title="Rai":http-user-agent=... rompe il parse del group.
       Fix: tronca il valore all'eventuale \":\"
  2. Righe #KODIPROP (DRM Widevine) — non devono consumare current{}
  3. Stream .mpd (MPEG-DASH) — non supportati da Stremio nativo, scartati
  4. http-user-agent in EXTINF — estratto e salvato per uso futuro
"""

import asyncio
import logging
import re
import time
from typing import Optional

import aiohttp

from .config import CACHE_TTL, USER_AGENT

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[list[dict], float]] = {}

# Tag da ignorare senza resettare current (non sono URL)
_SKIP_PREFIXES = (
    "#KODIPROP",
    "#EXTVLCOPT",
    "#EXTHTTP",
)


# ── Download ────────────────────────────────────────────────────────────────

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
    """
    Estrae il valore di un attributo dalla riga #EXTINF.
    Gestisce attributi con e senza spazio tra di loro:
      tvg-id="Rai1.it"tvg-logo="..."
      tvg-id="Rai1.it" tvg-logo="..."
    Tronca il valore al primo \":\" non-URL per gestire:
      group-title="Rai":http-user-agent="HbbTV"
    """
    m = re.search(
        rf'(?:^|\s){re.escape(name)}=["\']?([^"\' >]+)["\']?',
        line,
        re.IGNORECASE,
    )
    if not m:
        # fallback: senza spazio prima (es. tvg-id="X"tvg-logo=)
        m = re.search(
            rf'{re.escape(name)}="([^"]+)"',
            line,
            re.IGNORECASE,
        )
    if not m:
        return ""
    val = m.group(1)
    # Tronca group-title="Rai":http-user-agent=... → "Rai"
    if name.lower() == "group-title" and ":" in val and not val.startswith("http"):
        val = val.split(":")[0]
    return val.strip('"\'')


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    return re.sub(r"[\s-]+", "-", slug).strip("-") or "ch"


def _parse_m3u(content: str, source_label: str) -> list[dict]:
    channels: list[dict] = []
    current: dict = {}
    skipped_drm = 0
    skipped_dash = 0

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # Tag da ignorare senza toccare current
        if any(line.startswith(p) for p in _SKIP_PREFIXES):
            continue

        if line.startswith("#EXTINF:"):
            tvg_id   = _attr(line, "tvg-id")
            tvg_name = _attr(line, "tvg-name")
            logo     = _attr(line, "tvg-logo") or _attr(line, "logo")
            group    = _attr(line, "group-title") or "Generale"
            ua       = _attr(line, "http-user-agent")  # salvato ma non usato ora

            # Nome canale = testo dopo l'ultima virgola
            comma = line.rfind(",")
            name  = line[comma + 1:].strip() if comma != -1 else (tvg_name or "Canale")

            current = {
                "tvg_id":     tvg_id,
                "name":       name or tvg_name or "Canale",
                "logo":       logo,
                "group":      group,
                "source":     source_label,
                "user_agent": ua,
            }

        elif line.startswith("#"):
            continue  # altri tag M3U, ignora

        elif current:
            # Scarta stream MPEG-DASH (.mpd) — non supportati da Stremio nativo
            if line.lower().endswith(".mpd") or ".mpd?" in line.lower():
                skipped_dash += 1
                current = {}
                continue

            ch_id = current["tvg_id"] if current["tvg_id"] else _slugify(current["name"])
            channels.append({
                "id":         f"iptv:{ch_id}",
                "name":       current["name"],
                "logo":       current["logo"],
                "group":      current["group"],
                "stream_url": line,
                "source":     current["source"],
                "user_agent": current.get("user_agent", ""),
            })
            current = {}

    if skipped_drm:
        logger.info(f"🔒 [{source_label}] Canali DRM scartati: {skipped_drm}")
    if skipped_dash:
        logger.info(f"⚠️  [{source_label}] Canali DASH (.mpd) scartati: {skipped_dash}")

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
        label = url.split("/")[-1]
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
    seen: dict[str, None] = {}
    for ch in await get_all_channels(iptv_urls):
        seen.setdefault(ch["group"], None)
    return list(seen.keys())


def invalidate_cache() -> None:
    _cache.clear()
    logger.info("🗑️  Cache IPTV invalidata")
