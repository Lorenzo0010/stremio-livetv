"""
proxy.py — Proxy HLS interno per stremio-livetv.

Permette di:
  - Aggirare problemi CORS e header sui flussi HLS live
  - Riscrivere manifest M3U8 (master e media playlist)
  - Proxiare segmenti .ts, chiavi AES, sub-playlist
  - Passare header custom via ?headers=<base64-JSON>
  - Seguire redirect del relinker RAI (risposta video/mp4 → URL HLS reale)
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


def _rewrite_manifest(content: str, original_url: str, proxy_base: str, headers_b64: str | None = None) -> str:
    """
    Riscrive tutti gli URL in un manifest M3U8 sostituendoli con URL proxy.
    Gestisce: righe URL, URI=\"...\" in #EXT-X-KEY, #EXT-X-MAP, #EXT-X-MEDIA.
    """
    h_param = f"&headers={quote(headers_b64, safe='')}" if headers_b64 else ""

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
        elif s.startswith("#"):
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
        # Prima richiesta con follow_redirects=False per catturare la Location
        resp = await client.get(url, headers=headers, follow_redirects=False)
        hops = 0
        while resp.status_code in (301, 302, 303, 307, 308) and hops < 10:
            location = resp.headers.get("location", "")
            if not location:
                break
            logger.debug(f"[relinker] redirect → {location[:100]}")
            resp = await client.get(location, headers=headers, follow_redirects=False)
            hops += 1

        # URL finale: se è un redirect con Location, usalo
        final_url = str(resp.url)
        ct = resp.headers.get("content-type", "").split(";")[0].strip().lower()

        # Se la risposta è un M3U8, è già il manifest — restituiamo l'URL finale
        if _is_m3u8(ct, "") or ".m3u8" in final_url:
            logger.info(f"[relinker] risolto HLS → {final_url[:100]}")
            return final_url

        # video/mp4 o altro — restituiamo l'URL finale (Stremio può gestire MP4)
        logger.info(f"[relinker] risolto {ct} → {final_url[:100]}")
        return final_url

    except Exception as e:
        logger.error(f"[relinker] errore risoluzione {url[:80]}: {e}")
        return url  # fallback: URL originale


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

    if _RAI_RELINKER_RE.search(url):
        url = await _resolve_rai_relinker(url, eff, client)
        if not headers:
            headers = encode_headers_b64(custom) if custom else None

    try:
        req = client.build_request("GET", url, headers=eff)
        resp = await client.send(req, stream=True)
        if resp.status_code != 200:
            await resp.aclose()
            logger.warning(f"[proxy manifest] upstream {resp.status_code} per {url[:80]}")
            raise HTTPException(resp.status_code, f"Upstream {resp.status_code}")
            
        ct = resp.headers.get("content-type", "")
        ct_lower = ct.split(";")[0].strip().lower()
        is_m3u8 = ct_lower in _M3U8_CONTENT_TYPES or ".m3u8" in url.lower()

        if is_m3u8:
            await resp.aread()
            body = resp.text
            if body.lstrip().startswith("#EXTM3U") or ct_lower in _M3U8_CONTENT_TYPES:
                rewritten = _rewrite_manifest(body, url, _proxy_base(request), headers)
                await resp.aclose()
                return Response(rewritten, media_type="application/vnd.apple.mpegurl",
                                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})

        # Stream direct MP4 or other non-manifest
        async def _stream():
            try:
                if hasattr(resp, '_content'):
                    yield resp.content
                else:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk
            finally:
                await resp.aclose()

        resp_headers = {"Access-Control-Allow-Origin": "*"}
        if "content-length" in resp.headers:
            resp_headers["Content-Length"] = resp.headers["content-length"]
            
        return StreamingResponse(_stream(), status_code=resp.status_code, media_type=ct,
                                 headers=resp_headers)
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
        req = client.build_request("GET", url, headers=eff)
        resp = await client.send(req, stream=True)
        if resp.status_code not in (200, 206):
            await resp.aclose()
            raise HTTPException(resp.status_code, "Upstream error")
            
        ct = resp.headers.get("content-type", "video/MP2T")
        ct_lower = ct.split(";")[0].strip().lower()
        
        if ct_lower in _M3U8_CONTENT_TYPES or ".m3u8" in url.lower():
            await resp.aread()
            body = resp.text
            if body.lstrip().startswith("#EXTM3U") or ct_lower in _M3U8_CONTENT_TYPES:
                rewritten = _rewrite_manifest(body, url, _proxy_base(request), headers)
                await resp.aclose()
                return Response(rewritten, media_type="application/vnd.apple.mpegurl",
                                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})

        async def _stream():
            try:
                if hasattr(resp, '_content'):
                    yield resp.content
                else:
                    async for chunk in resp.aiter_bytes(chunk_size=65536):
                        yield chunk
            finally:
                await resp.aclose()
                
        resp_headers = {"Access-Control-Allow-Origin": "*"}
        if "content-length" in resp.headers:
            resp_headers["Content-Length"] = resp.headers["content-length"]
            
        return StreamingResponse(_stream(), status_code=resp.status_code, media_type=ct,
                                  headers=resp_headers)
    except httpx.RequestError as e:
        logger.error(f"[proxy] segment error: {e}")
        raise HTTPException(502, "Upstream non raggiungibile")
