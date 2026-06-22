"""
index.py — FastAPI app principale per stremio-livetv.

Endpoint Stremio:
  GET /manifest.json
  GET /catalog/tv/livetv.json?genre=...&skip=...&search=...
  GET /catalog/tv/livetv/genre={genre}.json
  GET /stream/tv/{id}.json
  GET /meta/tv/{id}.json

Endpoint utilità:
  GET /              — stato addon
  GET /cache/reload  — invalida cache canali e ricarica
  GET /groups        — lista gruppi disponibili
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import (
    ADDON_ID, ADDON_NAME, ADDON_VERSION, ADDON_LOGO,
    IPTV_URLS, IPTV_PAGE_SIZE, CACHE_TTL,
    validate_config,
)
from .iptv import (
    get_all_channels, get_channel_by_id, get_channels_page,
    get_groups, invalidate_cache,
    get_provider_headers, detect_provider,
)
from .proxy import router as proxy_router, close_proxy_client, encode_headers_b64

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


async def _background_cache_refresh():
    """
    Task in background: ogni CACHE_TTL secondi invalida la cache
    e pre-carica i canali da tutte le sorgenti, senza aspettare richieste utente.
    """
    while True:
        await asyncio.sleep(CACHE_TTL)
        logger.info("🔄 Background refresh cache avviato...")
        try:
            invalidate_cache()
            ch = await get_all_channels(IPTV_URLS)
            logger.info(f"✅ Background refresh completato: {len(ch)} canali")
        except Exception as e:
            logger.warning(f"⚠️  Background refresh fallito: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_config()
    try:
        ch = await get_all_channels(IPTV_URLS)
        logger.info(f"📺 Pre-caricati {len(ch)} canali")
    except Exception as e:
        logger.warning(f"⚠️  Pre-caricamento fallito: {e}")
    refresh_task = asyncio.create_task(_background_cache_refresh())
    logger.info(f"⏱  Background cache refresh avviato (ogni {CACHE_TTL}s)")
    yield
    refresh_task.cancel()
    try:
        await refresh_task
    except asyncio.CancelledError:
        pass
    await close_proxy_client()
    logger.info("🔌 Sessioni HTTP chiuse")


app = FastAPI(title=ADDON_NAME, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)
app.include_router(proxy_router)


def _json(data: Any) -> JSONResponse:
    r = JSONResponse(content=data)
    r.headers["Access-Control-Allow-Origin"] = "*"
    return r


# ── Stato ─────────────────────────────────────────────────────────────────────

@app.get("/")
async def root(request: Request):
    base = str(request.base_url).rstrip("/")
    return _json({"status": "online", "addon": ADDON_NAME,
                  "version": ADDON_VERSION, "manifest": f"{base}/manifest.json"})


@app.get("/groups")
async def groups_list():
    return _json({"groups": await get_groups(IPTV_URLS)})


@app.get("/cache/reload")
async def cache_reload():
    invalidate_cache()
    ch = await get_all_channels(IPTV_URLS)
    return _json({"status": "ok", "channels": len(ch)})


# ── Manifest ──────────────────────────────────────────────────────────────────

@app.get("/manifest.json")
async def manifest():
    return _json({
        "id":          ADDON_ID,
        "version":     ADDON_VERSION,
        "name":        ADDON_NAME,
        "description": "Live TV italiana da sorgenti IPTV M3U/M3U8 con proxy HLS interno",
        "logo":        ADDON_LOGO,
        "resources":   ["stream", "meta", "catalog"],
        "types":       ["tv"],
        "catalogs": [
            {
                "type":  "tv",
                "id":    "livetv",
                "name":  "📺 Live TV Italia",
                "extra": [
                    {"name": "search", "isRequired": False},
                    {"name": "genre",  "isRequired": False},
                    {"name": "skip",   "isRequired": False},
                ],
            }
        ],
        "behaviorHints": {"configurable": False},
    })


# ── Catalog ───────────────────────────────────────────────────────────────────

def _ch_to_meta(ch: dict) -> dict:
    return {
        "id":          ch["id"],
        "type":        "tv",
        "name":        ch["name"],
        "poster":      ch["logo"] or ADDON_LOGO,
        "background":  ch["logo"] or ADDON_LOGO,
        "logo":        ch["logo"] or ADDON_LOGO,
        "genres":      [ch["group"]],
        "description": f"{ch['group']} · {ch['source']} · {ch.get('provider', 'generic')}",
    }


def _search_channels(channels: list[dict], query: str) -> list[dict]:
    """Filtra i canali per nome con ricerca case-insensitive e supporto parziale."""
    q = query.lower().strip()
    if not q:
        return channels
    # Ordina: prima i match che iniziano con la query, poi i match che la contengono
    starts = [c for c in channels if c["name"].lower().startswith(q)]
    contains = [c for c in channels if q in c["name"].lower() and not c["name"].lower().startswith(q)]
    return starts + contains


@app.get("/catalog/tv/livetv.json")
async def catalog_tv(
    genre:  Optional[str] = Query(None),
    skip:   int           = Query(0, ge=0),
    search: Optional[str] = Query(None),
):
    if search:
        # In modalità ricerca: nessun filtro genre, nessuna paginazione, max 100 risultati
        all_ch = await get_all_channels(IPTV_URLS)
        results = _search_channels(all_ch, search)
        logger.info(f"🔍 Ricerca '{search}': {len(results)} risultati")
        return _json({"metas": [_ch_to_meta(c) for c in results[:100]]})
    channels = await get_channels_page(IPTV_URLS, group=genre, skip=skip, limit=IPTV_PAGE_SIZE)
    return _json({"metas": [_ch_to_meta(c) for c in channels]})


@app.get("/catalog/tv/livetv/genre={genre}.json")
async def catalog_tv_genre(genre: str, skip: int = Query(0, ge=0)):
    channels = await get_channels_page(IPTV_URLS, group=genre, skip=skip, limit=IPTV_PAGE_SIZE)
    return _json({"metas": [_ch_to_meta(c) for c in channels]})


# ── Stream ────────────────────────────────────────────────────────────────────

def _build_proxy_url(base: str, stream_url: str, provider_headers: dict) -> str:
    enc_url = quote(stream_url, safe="")
    if provider_headers:
        h_b64 = encode_headers_b64(provider_headers)
        h_param = quote(h_b64, safe="")
        return f"{base}/proxy/manifest.m3u8?url={enc_url}&headers={h_param}"
    return f"{base}/proxy/manifest.m3u8?url={enc_url}"


@app.get("/stream/tv/{id}.json")
async def stream_tv(id: str, request: Request):
    ch = await get_channel_by_id(IPTV_URLS, id)
    if ch is None:
        return _json({"streams": []})

    stream_url = ch["stream_url"]
    base = str(request.base_url).rstrip("/")
    provider = ch.get("provider") or detect_provider(stream_url, ch.get("user_agent", ""))
    p_headers = get_provider_headers(provider)

    logger.debug(f"[stream] {ch['name']} | provider={provider} | url={stream_url[:80]}")

    if ".m3u8" in stream_url or stream_url.endswith(".m3u") or "relinker" in stream_url.lower():
        proxied_url = _build_proxy_url(base, stream_url, p_headers)
    else:
        proxied_url = stream_url

    title_provider = {
        "rai_relinker": "RAI",
        "rai_cdn": "RAI CDN",
        "mediaset": "Mediaset",
        "la7": "LA7",
        "sky": "Sky",
        "generic": "",
    }.get(provider, provider)

    title_suffix = f" [{title_provider}]" if title_provider else ""

    return _json({
        "streams": [{
            "url":   proxied_url,
            "name":  ch["name"],
            "title": f"📺 {ch['name']}{title_suffix}\n{ch['group']} · {ch['source']}",
            "behaviorHints": {
                "notWebReady": False,
                "bingeGroup":  f"iptv-{ch['group']}",
            },
        }]
    })


# ── Meta ──────────────────────────────────────────────────────────────────────

@app.get("/meta/tv/{id}.json")
async def meta_tv(id: str):
    ch = await get_channel_by_id(IPTV_URLS, id)
    if ch is None:
        return _json({"meta": {}})
    return _json({"meta": {
        **_ch_to_meta(ch),
        "links":   [],
        "trailers": [],
    }})
