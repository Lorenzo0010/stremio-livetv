import os
import logging

logger = logging.getLogger(__name__)

ADDON_NAME = "Live TV Italia"
ADDON_ID   = "org.stremio.livetv.italia"
ADDON_VERSION = "1.0.0"
ADDON_LOGO = "https://static.vecteezy.com/system/resources/thumbnails/037/297/656/small/tv-icon-3d-render-free-png.png"

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
)

# ── IPTV ──────────────────────────────────────────────────────────────────────
DEFAULT_IPTV_URLS: list[str] = [
    # Core — aggiornate quotidianamente
    "https://raw.githubusercontent.com/iptv-org/iptv/master/streams/it.m3u",
    "https://raw.githubusercontent.com/Free-TV/IPTV/refs/heads/master/playlists/playlist_italy.m3u8",
    # Supplementare — canali esclusivi (SportItalia, RAI 4K, 7Gold, DiscoveryTV…)
    "https://raw.githubusercontent.com/maginetweb-arch/TVITALIA/refs/heads/main/iptvit.m3u",
]

_env_iptv = os.getenv("IPTV_URLS", "")
IPTV_URLS: list[str] = (
    [u.strip() for u in _env_iptv.split(",") if u.strip()]
    if _env_iptv
    else DEFAULT_IPTV_URLS
)

IPTV_PAGE_SIZE = int(os.getenv("IPTV_PAGE_SIZE", "100"))
CACHE_TTL      = int(os.getenv("CACHE_TTL", "3600"))


def validate_config() -> None:
    logger.info(f"✅ {ADDON_NAME} v{ADDON_VERSION} avviato")
    logger.info(f"📺 Sorgenti IPTV configurate: {len(IPTV_URLS)}")
    for u in IPTV_URLS:
        logger.info(f"   → {u}")
    logger.info(f"⏱  Cache TTL: {CACHE_TTL}s  |  Page size: {IPTV_PAGE_SIZE}")
