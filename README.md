# YT Caption API

Small `FastAPI` wrapper around `yt-dlp` for pulling YouTube subtitles or auto captions.

## Why Python

- `yt-dlp` is a Python project, so integration is direct.
- `curl_cffi` is also Python-native and works well for browser-like HTTP requests.
- `FastAPI` gives you a tiny API surface with almost no boilerplate.
- `yt-dlp` currently works best here with Node available for its JS challenge solver.

## Setup

```powershell
uv sync
```

## Run

```powershell
uv run uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

## Endpoints

### `GET /health`

Returns a basic health check.

### `GET /transcript/{video_id}?language=en`

Example curl:

```powershell
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ?language=en"
```

Smaller responses:

```powershell
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ?language=en&include_meta=false"
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ?language=en&max_chars=500"
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ/text?language=en"
```

## Notes

- `cookies.txt` is expected in the project root.
- Node should be installed and available on `PATH`.
- The API prefers normal subtitles and falls back to auto captions by default.
- You can disable auto captions with `?include_auto=false`.
- Captions are cached on disk in `cache/` by video ID + language + auto-caption mode.
- Cache files older than 7 days are deleted automatically on startup and before new cache writes.
- A background cleanup job also prunes expired cache files every 6 hours while the API is running.
- You can bypass cache with `?use_cache=false`.
- Responses include `X-Cache: HIT` or `X-Cache: MISS`.
- `json3` caption tracks are converted into plain text. Other track types are returned as raw text.
