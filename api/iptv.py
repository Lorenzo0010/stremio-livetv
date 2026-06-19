"""
iptv.py — Parser M3U/M3U8 e gestore canali live.

Fix applicati analizzando le playlist reali:
  1. Attributi M3U senza spazio tra di loro (es. tvg-id="X"tvg-logo="Y")
  2. Righe #KODIPROP (DRM Widevine) — non devono consumare current{}
  3. Stream .mpd (MPEG-DASH) — non supportati da Stremio nativo, scartati
  4. http-user-agent in EXTINF — estratto e usato per header custom
  5. Rilevamento automatico provider (RAI / Mediaset / La7 / Sky / NOW /
     DAZN / Discovery / Rai Radio / Tv8 / Nove) per header injection
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

# ── Rilevamento provider ───────────────────────────────────────────────────────

_RAI_RELINKER = re.compile(r'relinker\.rai\.it|mediapolis\.rai\.it|relinkerServlet', re.IGNORECASE)
_RAI_CDN      = re.compile(r'rai-simulcast\.akamaized\.net|akamaized\.net.*[Rr]ai|raiplay', re.IGNORECASE)
_RAI_RADIO    = re.compile(r'radio\.rai\.it|icestreaming\.rai\.it|radio[0-9]?\.rai', re.IGNORECASE)
_MEDIASET_CDN = re.compile(r'mediaset|msf\.cdn|live.*mediaset|mediasetplay', re.IGNORECASE)
# La7: CDN Akamai con path /la7/, CloudFront dedicato, dominio diretto
_LA7_CDN      = re.compile(
    r'la7\.it|d1chghleocc9sm\.cloudfront|la7stream|'
    r'akamaized\.net.*/la7|la7d\.akamaized',
    re.IGNORECASE,
)
# Sky: skycdn, SkyGO, TG24, NOW TV (now.sky.it, nowtv.it, skyshowtime)
_SKY_CDN      = re.compile(
    r'skycdn|skytg24|skygo\.sky\.it|skytv|'
    r'now\.sky\.it|nowtv\.it|skyshowtime|'
    r'akamaicdn\.sky\.it|sky-h\.akamaized',
    re.IGNORECASE,
)
# DAZN — CDN Akamai/AWS dedicati
_DAZN_CDN     = re.compile(r'dazn\.com|daznservices|dazn-.*\.akamaized', re.IGNORECASE)
# Discovery / Warner / Eurosport / Real Time / NOVE / DMAX / Focus
_DISCOVERY_CDN = re.compile(
    r'discoveryplus|dplay\.com|eurosportplayer|'
    r'nove\.tv|novetv|dmax\.it|realtime\.it|'
    r'warnermedia|discoverychannelgo|hbomax',
    re.IGNORECASE,
)
# Tv8 / Nove (Gruppo Sky Italia)
_TV8_CDN      = re.compile(r'tv8\.it|tv8stream|akamaized\.net.*/tv8', re.IGNORECASE)

# User-Agent HbbTV usato da RAI per i relinker
_UA_HBBTV   = "HbbTV/1.6.1 (+DL+DH;Samsung;SmartTV2018;T-HKMFDEUC-1252.2302.1;1/;v3.3"
_UA_MOBILE  = "Mozilla/5.0 (Linux; Android 12; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
_UA_DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_UA_SMART_TV = "Mozilla/5.0 (SMART-TV; Linux; Tizen 6.0) AppleWebKit/538.1 (KHTML, like Gecko) Version/6.0 TV Safari/538.1"


def detect_provider(url: str, user_agent_hint: str = "") -> str:
    """Rileva il provider dallo stream URL o dallo user-agent del tag EXTINF."""
    if _RAI_RELINKER.search(url):
        return "rai_relinker"
    if _RAI_RADIO.search(url):
        return "rai_radio"
    if _RAI_CDN.search(url):
        return "rai_cdn"
    if _MEDIASET_CDN.search(url):
        return "mediaset"
    if _LA7_CDN.search(url):
        return "la7"
    if _DAZN_CDN.search(url):
        return "dazn"
    if _DISCOVERY_CDN.search(url):
        return "discovery"
    if _TV8_CDN.search(url):
        return "tv8"
    if _SKY_CDN.search(url):
        return "sky"
    # Fallback: controlla UA dal tag EXTINF
    if user_agent_hint and "hbbtv" in user_agent_hint.lower():
        return "rai_relinker"
    return "generic"


def get_provider_headers(provider: str) -> dict:
    """
    Restituisce gli header HTTP necessari per il provider rilevato.
    Questi vengono passati al proxy come headers=<base64-JSON>.
    """
    if provider == "rai_relinker":
        return {
            "User-Agent": _UA_HBBTV,
            "Referer": "https://www.raiplay.it/",
            "Origin": "https://www.raiplay.it",
            "Accept": "application/x-mpegurl, application/vnd.apple.mpegurl, */*",
        }
    if provider == "rai_cdn":
        return {
            "User-Agent": _UA_HBBTV,
            "Referer": "https://www.rai.it/",
            "Origin": "https://www.rai.it",
        }
    if provider == "rai_radio":
        return {
            "User-Agent": _UA_DESKTOP,
            "Referer": "https://www.raiplayradio.it/",
            "Origin": "https://www.raiplayradio.it",
            "Accept": "audio/mpeg, audio/aac, */*",
        }
    if provider == "mediaset":
        return {
            "User-Agent": _UA_DESKTOP,
            "Referer": "https://mediasetplay.mediaset.it/",
            "Origin": "https://mediasetplay.mediaset.it",
            "Accept": "application/x-mpegurl, */*",
        }
    if provider == "la7":
        return {
            "User-Agent": _UA_DESKTOP,
            "Referer": "https://www.la7.it/",
            "Origin": "https://www.la7.it",
            "Accept": "application/x-mpegurl, application/vnd.apple.mpegurl, */*",
        }
    if provider == "sky":
        return {
            "User-Agent": _UA_SMART_TV,
            "Referer": "https://www.sky.it/",
            "Origin": "https://www.sky.it",
            "Accept": "application/x-mpegurl, */*",
        }
    if provider == "dazn":
        return {
            "User-Agent": _UA_DESKTOP,
            "Referer": "https://www.dazn.com/",
            "Origin": "https://www.dazn.com",
            "Accept": "application/x-mpegurl, */*",
        }
    if provider == "discovery":
        return {
            "User-Agent": _UA_DESKTOP,
            "Referer": "https://www.discoveryplus.com/it/",
            "Origin": "https://www.discoveryplus.com",
            "Accept": "application/x-mpegurl, */*",
        }
    if provider == "tv8":
        return {
            "User-Agent": _UA_DESKTOP,
            "Referer": "https://www.tv8.it/",
            "Origin": "https://www.tv8.it",
            "Accept": "application/x-mpegurl, */*",
        }
    # generic: usa User-Agent desktop standard
    return {"User-Agent": _UA_DESKTOP}


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
    m = re.search(
        rf'(?:^|\s){re.escape(name)}=["\']?([^"\'\ >]+)["\']?',
        line,
        re.IGNORECASE,
    )
    if not m:
        m = re.search(
            rf'{re.escape(name)}="([^"]+)"',
            line,
            re.IGNORECASE,
        )
    if not m:
        return ""
    val = m.group(1)
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

        if any(line.startswith(p) for p in _SKIP_PREFIXES):
            continue

        if line.startswith("#EXTINF:"):
            tvg_id   = _attr(line, "tvg-id")
            tvg_name = _attr(line, "tvg-name")
            logo     = _attr(line, "tvg-logo") or _attr(line, "logo")
            group    = _attr(line, "group-title") or "Generale"
            ua       = _attr(line, "http-user-agent")

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
            continue

        elif current:
            if line.lower().endswith(".mpd") or ".mpd?" in line.lower():
                skipped_dash += 1
                current = {}
                continue

            provider = detect_provider(line, current.get("user_agent", ""))

            ch_id = current["tvg_id"] if current["tvg_id"] else _slugify(current["name"])
            channels.append({
                "id":         f"iptv:{ch_id}",
                "name":       current["name"],
                "logo":       current["logo"],
                "group":      current["group"],
                "stream_url": line,
                "source":     current["source"],
                "user_agent": current.get("user_agent", ""),
                "provider":   provider,
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

    provider_stats: dict[str, int] = {}
    for url, content in zip(iptv_urls, results):
        if not content:
            continue
        label = url.split("/")[-1]
        for ch in _parse_m3u(content, label):
            if ch["id"] not in seen:
                seen.add(ch["id"])
                all_channels.append(ch)
                p = ch.get("provider", "generic")
                provider_stats[p] = provider_stats.get(p, 0) + 1

    logger.info(f"📺 Canali totali caricati: {len(all_channels)}")
    for p, n in sorted(provider_stats.items()):
        logger.info(f"   ├─ {p}: {n} canali")
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
