"""
index.py — FastAPI app principale per stremio-livetv.

Endpoint Stremio:
  GET /manifest.json
  GET /catalog/tv/livetv.json?genre=...&skip=...&search=...
  GET /catalog/tv/livetv/search={query}.json   <- ricerca Stremio (path param)
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
import re
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import quote

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, HTMLResponse

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
    manifest_url = f"{base}/manifest.json"

    html = f"""
<!DOCTYPE html>
<html lang="it">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{ADDON_NAME} - Stremio Addon</title>
    <style>
        body {{
            margin: 0;
            padding: 0;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%);
            color: #fff;
            display: flex;
            flex-direction: column;
            align-items: center;
            min-height: 100vh;
        }}
        .container {{
            margin-top: 10vh;
            text-align: center;
            background: rgba(0, 0, 0, 0.5);
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.3);
            backdrop-filter: blur(10px);
            width: 90%;
            max-width: 600px;
            border: 1px solid rgba(255, 255, 255, 0.1);
        }}
        .logo {{
            width: 120px;
            height: 120px;
            margin-bottom: 20px;
            border-radius: 20%;
            object-fit: cover;
            box-shadow: 0 4px 15px rgba(0,0,0,0.5);
        }}
        h1 {{
            margin: 0 0 10px 0;
            font-size: 2.5em;
        }}
        p.subtitle {{
            font-size: 1.1em;
            color: #ccc;
            margin-bottom: 30px;
        }}
        .copy-box {{
            display: flex;
            align-items: center;
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.2);
            border-radius: 8px;
            padding: 10px;
        }}
        .copy-box input {{
            flex: 1;
            background: transparent;
            border: none;
            color: #fff;
            font-size: 1em;
            outline: none;
            padding: 5px;
        }}
        .copy-box button {{
            background: #4caf50;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            transition: background 0.3s;
        }}
        .copy-box button:hover {{
            background: #45a049;
        }}
    </style>
</head>
<body>
    <div class="container">
        <img src="{ADDON_LOGO}" alt="Logo" class="logo">
        <h1>{ADDON_NAME}</h1>
        <p class="subtitle">v{ADDON_VERSION} - Live TV italiana per Stremio</p>
        
        <div class="copy-box">
            <input type="text" id="manifestUrl" value="{manifest_url}" readonly>
            <button onclick="copyManifest()">Copia Link</button>
        </div>
    </div>

    <script>
        function copyManifest() {{
            var copyText = document.getElementById("manifestUrl");
            copyText.select();
            copyText.setSelectionRange(0, 99999);
            navigator.clipboard.writeText(copyText.value).then(() => {{
                alert("Link copiato negli appunti: " + copyText.value);
            }}).catch(err => {{
                console.error("Errore durante la copia", err);
            }});
        }}
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html)


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


_SEP = re.compile(r"[\s\-_\.]+")

def _normalize(text: str) -> str:
    """Rimuove spazi, trattini, underscore e punti per il confronto fuzzy.
    Es: 'Rai 1' -> 'rai1', 'TG 24' -> 'tg24', 'Italia-1' -> 'italia1'
    """
    return _SEP.sub("", text.lower())


def _search_channels(channels: list[dict], query: str) -> list[dict]:
    """
    Ricerca canali con doppio confronto:
    1. Testo originale (case-insensitive) — per match precisi con spazi
    2. Testo normalizzato (senza separatori) — per match tipo rai1 -> Rai 1
    Priorita': match che iniziano con la query > match che la contengono.
    """
    q_raw  = query.lower().strip()
    q_norm = _normalize(query)
    if not q_raw:
        return channels

    def _matches(name: str) -> bool:
        n_raw  = name.lower()
        n_norm = _normalize(name)
        return q_raw in n_raw or q_norm in n_norm

    def _starts(name: str) -> bool:
        n_raw  = name.lower()
        n_norm = _normalize(name)
        return n_raw.startswith(q_raw) or n_norm.startswith(q_norm)

    starts   = [c for c in channels if _starts(c["name"])]
    contains = [c for c in channels if _matches(c["name"]) and not _starts(c["name"])]
    return starts + contains


@app.get("/catalog/tv/livetv.json")
async def catalog_tv(
    genre:  Optional[str] = Query(None),
    skip:   int           = Query(0, ge=0),
    search: Optional[str] = Query(None),
):
    if search:
        all_ch = await get_all_channels(IPTV_URLS)
        results = _search_channels(all_ch, search)
        logger.info(f"🔍 Ricerca '{search}': {len(results)} risultati")
        return _json({"metas": [_ch_to_meta(c) for c in results[:100]]})
    channels = await get_channels_page(IPTV_URLS, group=genre, skip=skip, limit=IPTV_PAGE_SIZE)
    return _json({"metas": [_ch_to_meta(c) for c in channels]})


@app.get("/catalog/tv/livetv/search={query}.json")
async def catalog_tv_search(query: str):
    """
    Route per la ricerca Stremio: il client invia la query come path parameter
    nel formato /catalog/tv/{catalogId}/search={query}.json
    """
    all_ch = await get_all_channels(IPTV_URLS)
    results = _search_channels(all_ch, query)
    logger.info(f"🔍 Ricerca (path) '{query}': {len(results)} risultati")
    return _json({"metas": [_ch_to_meta(c) for c in results[:100]]})


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
                "isLive": True,
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
