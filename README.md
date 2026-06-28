# 📺 stremio-livetv

Addon Stremio per **Live TV italiana** via playlist IPTV M3U/M3U8.

Scritto in **Python / FastAPI**, include un proxy HLS interno per aggirare problemi CORS e header sui flussi live, oltre a funzionalità avanzate di ricerca e rilevamento automatico dei provider.

## Funzionalità

- **Catalogo canali italiani** aggregati da multiple sorgenti M3U/M3U8 pubbliche e legali (inclusi Pluto TV e Samsung TV Plus).
- **Filtro per gruppo/categoria** (RAI, Mediaset, News, Sport, ecc.) e paginazione del catalogo (`skip`).
- **Ricerca canali avanzata** integrata nativamente in Stremio con fuzzy matching (es. `rai 1` vs `rai1`).
- **Proxy HLS interno**: riscrittura manifest M3U8, segmenti `.ts`, e iniezione automatica degli header HTTP corretti identificando il provider di origine (Rai, Mediaset, La7, Sky, DAZN, Discovery, Tv8/Nove, ecc.).
- **Cache in memoria e background refresh**: TTL configurabile (default 1h) con aggiornamento automatico in background per evitare rallentamenti all'utente.
- **Pre-caricamento canali** all'avvio dell'applicazione.
- **Filtro canali automatico**: esclusione di stream DASH (`.mpd` non supportati nativamente) e filtro parole chiave per contenuti adulti.
- Aggiunta sorgenti extra via variabile d'ambiente `IPTV_URLS`.

## Sorgenti IPTV predefinite

| Sorgente | URL |
|---|---|
| TVITALIA | `https://raw.githubusercontent.com/maginetweb-arch/TVITALIA/refs/heads/main/iptvit.m3u` |
| Free-TV Italy | `https://raw.githubusercontent.com/Free-TV/IPTV/refs/heads/master/playlists/playlist_italy.m3u8` |
| Tundrak IPTV-Italia | `https://github.com/Tundrak/IPTV-Italia/raw/main/iptvitaplus.m3u` |
| iptv-org (Italia) | `https://raw.githubusercontent.com/iptv-org/iptv/master/streams/it.m3u` |
| Pluto TV | `https://i.mjh.nz/PlutoTV/it.m3u8` |
| Samsung TV Plus | `https://i.mjh.nz/SamsungTVPlus/it.m3u8` |

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

O visita la landing page dell'addon dal tuo browser all'indirizzo `http://<tuo-ip>:7878/` per copiare il link al manifest.

## Variabili d'ambiente

| Variabile | Default | Descrizione |
|---|---|---|
| `IPTV_URLS` | *(liste default)* | URL M3U/M3U8 extra, separati da virgola |
| `IPTV_PAGE_SIZE` | `100` | Canali per pagina nel catalogo |
| `CACHE_TTL` | `3600` | Secondi di validità della cache canali |
| `PORT` | `7878` | Porta di ascolto (se configurata diversamente nel container) |
| `USER_AGENT` | *(desktop)* | User-Agent di base utilizzato nelle richieste HTTP |

## Struttura

```
api/
  index.py      # FastAPI app, manifest, routes catalog/stream/meta, ricerca
  iptv.py       # Parser M3U, cache, filter provider, regex canali
  proxy.py      # Proxy HLS interno (manifest + segmenti)
  config.py     # Configurazione e variabili d'ambiente
Dockerfile
requirements.txt
```
