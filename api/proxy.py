"""
proxy.py — Proxy HLS interno per stremio-livetv.

Permette di:
  - Aggirare problemi CORS e header sui flussi HLS live
  - Riscrivere manifest M3U8 (master e media playlist)
  - Proxiare segmenti .ts, chiavi AES, sub-playlist
  - Passare header custom via ?headers=<base64-JSON>
  - Seguire redirect del relinker RAI (risposta video/mp4 → URL HLS reale)

Fix live timer:
  - Rimuove #EXT-X-PROGRAM-DATE-TIME (causa calcolo durata assoluta in Stremio/VLC)
  - Rimuove #EXT-X-PLAYLIST-TYPE:VOD (forza modalità live)
  - Rimuove #EXT-X-ENDLIST (non inviare mai fine stream)
"""

import base64
import json
import logging
import re
from urllib.parse import quote, urljoin, urlparse

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from .config import USER_AGENT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxy", tags=["proxy"])

_DEFAULT_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8",
    "Connection": "keep-alive",
}

_TIMEOUT = httpx.Timeout(30.0)
_client: httpx.AsyncClient | None = None

_M3U8_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "audio/mpegurl",
    "audio/x-mpegurl",
}

# Regex relinker RAI — l'URL viene risolto in un MP4 o HLS a seconda dell'UA
_RAI_RELINKER_RE = re.compile(r'relinker\.rai\.it|mediapolis\.rai\.it|relinkerServlet', re.IGNORECASE)

# Tag da rimuovere completamente perché causano il calcolo di una durata assoluta
# in Stremio/VLC che porta al blocco a ~3 ore sul live
_STRIP_TAGS_RE = re.compile(
    r'^#EXT-X-PROGRAM-DATE-TIME'     # timeline assoluta → VLC calcola durata totale
    r'|^#EXT-X-PLAYLIST-TYPE:VOD'    # forza modalità VOD
    r'|^#EXT-X-ENDLIST',             # segnala fine stream → non deve mai arrivare al client
    re.IGNORECASE
)


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True)
    return _client


async def close_proxy_client() -> None:
    global _client
    if _client and not _client.is_closed:
        await _client.aclose()
        _client = None


def _decode_headers_param(h: str | None) -> dict:
    if not h:
        return {}
    try:
        padded = h + "==" if len(h) % 4 else h
        return json.loads(base64.b64decode(padded).decode("utf-8"))
    except Exception:
        return {}


def _build_headers(custom: dict) -> dict:
    merged = {**_DEFAULT_HEADERS, **custom}
    return merged


def _is_m3u8(content_type: str, body: str) -> bool:
    ct = content_type.split(";")[0].strip().lower()
    return ct in _M3U8_CONTENT_TYPES or body.lstrip().startswith("#EXTM3U")


def _make_absolute(url: str, base: str) -> str:
    return url if url.startswith("http") else urljoin(base, url)


def _proxy_base(request: Request) -> str:
    return str(request.base_url).rstrip("/") + "/proxy"


def _is_master_playlist(content: str) -> bool:
    """Restituisce True se il manifest è un master playlist (contiene stream variant)."""
    return bool(re.search(r'^#EXT-X-STREAM-INF', content, re.MULTILINE))


def _rewrite_manifest(content: str, original_url: str, proxy_base: str, headers_b64: str | None = None) -> str:
    """
    Riscrive tutti gli URL in un manifest M3U8 sostituendoli con URL proxy.
    Gestisce: righe URL, URI=\"...\" in #EXT-X-KEY, #EXT-X-MAP, #EXT-X-MEDIA.

    Per le media playlist (non master) rimuove i tag che causano il blocco
    a ~3 ore in Stremio/VLC:
      - #EXT-X-PROGRAM-DATE-TIME  (VLC somma i timestamp assoluti come durata)
      - #EXT-X-PLAYLIST-TYPE:VOD  (forza modalità VOD bloccando gli aggiornamenti)
      - #EXT-X-ENDLIST            (segnala fine stream, mai da inviare su live)
    """
    h_param = f"&headers={quote(headers_b64, safe='')}" if headers_b64 else ""
    is_master = _is_master_playlist(content)

    def proxify(raw: str) -> str:
        abs_url = _make_absolute(raw, original_url)
        enc_url = quote(abs_url, safe="")
        if ".m3u8" in urlparse(abs_url).path or abs_url.endswith(".m3u"):
            return f"{proxy_base}/manifest.m3u8?url={enc_url}{h_param}"
        return f"{proxy_base}/segment?url={enc_url}{h_param}"

    out = []
    for line in content.splitlines(keepends=True):
        s = line.strip()
        if not s:
            out.append(line)
            continue

        if s.startswith("#"):
            # Sui media playlist rimuovi i tag che causano il blocco a 3h
            if not is_master and _STRIP_TAGS_RE.match(s):
                logger.debug(f"[m3u8] rimosso tag live-blocker: {s[:80]}")
                continue
            # Riscrivi URI=\"...\" dentro tag come #EXT-X-KEY, #EXT-X-MAP, #EXT-X-MEDIA
            if 'URI="' in s:
                line = re.sub(r'URI="([^"]+)"', lambda m: f'URI="{proxify(m.group(1))}"', line)
            out.append(line)
        else:
            out.append(proxify(s) + "\n")

    return "".join(out)


def encode_headers_b64(headers: dict) -> str:
    return base64.b64encode(json.dumps(headers).encode()).decode().rstrip("=")


async def _resolve_rai_relinker(url: str, headers: dict, client: httpx.AsyncClient) -> str:
    """
    Il relinker RAI risponde con redirect o con payload video/mp4.
    Segue i redirect finché non trova un URL HLS (.m3u8) o restituisce
    l'URL finale (anche se MP4, Stremio lo gestisce).
    """
    try:
        resp = await client.get(url, headers=headers, follow_redirects=False)
        hops = 0
        while resp.status_code in (301, 302, 303, 307, 308) and hops < 10:
            location = resp.headers.get("location", "")
            if not location:
                break
            logger.debug(f"[relinker] redirect → {location[:100]}")
            resp = await client.get(location, headers=headers, follow_redirects=False)
            hops += 1

        final_url = str(resp.url)
        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()

        if _is_m3u8(ct, "") or ".m3u8" in final_url:
            logger.info(f"[relinker] risolto HLS → {final_url[:100]}")
            return final_url

        logger.info(f"[relinker] risolto {ct} → {final_url[:100]}")
        return final_url

    except Exception as e:
        logger.error(f"[relinker] errore risoluzione {url[:80]}: {e}")
        return url


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.head("/manifest.m3u8")
async def proxy_manifest_head():
    return Response(b"", media_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*"})


@router.get("/manifest.m3u8")
async def proxy_manifest(url: str, request: Request, headers: str | None = None):
    if not url:
        raise HTTPException(400, "Parametro 'url' mancante")
    client = get_client()
    custom = _decode_headers_param(headers)
    eff = _build_headers(custom)

    # ── RAI relinker: risolvi prima l'URL reale ──────────────────────────────
    if _RAI_RELINKER_RE.search(url):
        url = await _resolve_rai_relinker(url, eff, client)
        if not headers:
            headers = encode_headers_b64(custom) if custom else None

    try:
        resp = await client.get(url, headers=eff)
        if resp.status_code != 200:
            logger.warning(f"[proxy manifest] upstream {resp.status_code} per {url[:80]}")
            raise HTTPException(resp.status_code, f"Upstream {resp.status_code}")
        ct = resp.headers.get("content-type", "")
        body = resp.text
        if _is_m3u8(ct, body):
            rewritten = _rewrite_manifest(body, url, _proxy_base(request), headers)
            return Response(rewritten, media_type="application/vnd.apple.mpegurl",
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
        return Response(resp.content, media_type=ct,
                        headers={"Access-Control-Allow-Origin": "*"})
    except httpx.RequestError as e:
        logger.error(f"[proxy] manifest error: {e}")
        raise HTTPException(502, "Upstream non raggiungibile")


@router.get("/segment")
async def proxy_segment(url: str, request: Request, headers: str | None = None):
    if not url:
        raise HTTPException(400, "Parametro 'url' mancante")
    client = get_client()
    eff = _build_headers(_decode_headers_param(headers))
    try:
        resp = await client.get(url, headers=eff)
        if resp.status_code not in (200, 206):
            raise HTTPException(resp.status_code, "Upstream error")
        ct = resp.headers.get("content-type", "video/MP2T")
        body = resp.text
        if _is_m3u8(ct, body):
            rewritten = _rewrite_manifest(body, url, _proxy_base(request), headers)
            return Response(rewritten, media_type="application/vnd.apple.mpegurl",
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
        async def _stream():
            yield resp.content
        return StreamingResponse(_stream(), status_code=resp.status_code, media_type=ct,
                                  headers={"Access-Control-Allow-Origin": "*"})
    except httpx.RequestError as e:
        logger.error(f"[proxy] segment error: {e}")
        raise HTTPException(502, "Upstream non raggiungibile")
