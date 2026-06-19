# 📺 stremio-livetv

Addon Stremio per **Live TV italiana** via playlist IPTV M3U/M3U8.

Scritto in **Python / FastAPI**, include un proxy HLS interno per aggirare problemi CORS e header sui flussi live.

## Funzionalità

- Catalogo canali italiani da sorgenti M3U/M3U8 pubbliche
- Filtro per gruppo/categoria (RAI, Mediaset, News, Sport…)
- Paginazione del catalogo (`skip`)
- Proxy HLS interno: riscrittura manifest M3U8, segmenti `.ts`, chiavi AES
- Cache in memoria con TTL configurabile (default 1h)
- Pre-caricamento canali all'avvio
- Aggiunta sorgenti extra via variabile d'ambiente `IPTV_URLS`

## Sorgenti IPTV predefinite

| Sorgente | URL |
|---|---|
| TVITALIA | `https://raw.githubusercontent.com/maginetweb-arch/TVITALIA/refs/heads/main/iptvit.m3u` |
| Free-TV Italy | `https://raw.githubusercontent.com/Free-TV/IPTV/refs/heads/master/playlists/playlist_italy.m3u8` |

## Avvio rapido

### Docker Compose

```yaml
services:
  stremio-livetv:
    build: .
    ports:
      - "7878:7878"
    environment:
      - IPTV_URLS=   # opzionale: URL aggiuntivi separati da virgola
      - IPTV_PAGE_SIZE=100
      - CACHE_TTL=3600
```

### Locale

```bash
pip install -r requirements.txt
uvicorn api.index:app --host 0.0.0.0 --port 7878 --reload
```

## Installazione in Stremio

Aggiungi l'addon tramite URL:

```
http://<tuo-ip>:7878/manifest.json
```

## Variabili d'ambiente

| Variabile | Default | Descrizione |
|---|---|---|
| `IPTV_URLS` | *(liste default)* | URL M3U/M3U8 extra, separati da virgola |
| `IPTV_PAGE_SIZE` | `100` | Canali per pagina nel catalogo |
| `CACHE_TTL` | `3600` | Secondi di validità della cache canali |
| `PORT` | `7878` | Porta di ascolto |

## Struttura

```
api/
  index.py      # FastAPI app, manifest, routes catalog/stream/meta
  iptv.py       # Parser M3U, cache, helpers canali
  proxy.py      # Proxy HLS interno (manifest + segmenti)
  config.py     # Configurazione e variabili d'ambiente
Dockerfile
requirements.txt
```
