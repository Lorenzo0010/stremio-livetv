"""
proxy.py — Proxy HLS interno per stremio-livetv.

Permette di:
  - Aggirare problemi CORS e header sui flussi HLS live
  - Riscrivere manifest M3U8 (master e media playlist)
  - Proxiare segmenti .ts, chiavi AES, sub-playlist
  - Passare header custom via ?headers=<base64-JSON>
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
        return json.loads(base64.b64decode(h + "==").decode("utf-8"))
    except Exception:
        return {}


def _build_headers(custom: dict) -> dict:
    return {**_DEFAULT_HEADERS, **custom}


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
    eff = _build_headers(_decode_headers_param(headers))
    try:
        resp = await client.get(url, headers=eff)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code, "Upstream error")
        rewritten = _rewrite_manifest(resp.text, url, _proxy_base(request), headers)
        return Response(rewritten, media_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"})
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
