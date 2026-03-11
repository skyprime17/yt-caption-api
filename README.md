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

## Runtime Requirements

- Node.js 20+ is strongly recommended so `yt-dlp` can use its JavaScript runtime support reliably.
- `ffmpeg` is optional for this API, but recommended for general `yt-dlp` compatibility and troubleshooting.

## Run

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Deployment

- Example Linux systemd unit: `deploy/systemd/yt-caption-api.service.example`
- Update the username, home directory, project folder, and binary paths before using that example.

### Deploy With systemd

1. Copy the project to your server, for example into `/home/user/yt-caption-api`.
2. Make sure `.env`, `cookies.txt`, Node.js 20+, and your Python/uv environment are set up on the server.
3. Copy `deploy/systemd/yt-caption-api.service.example` to `/etc/systemd/system/yt-caption-api.service`.
4. Edit the service file and adjust:
   - `User`
   - `WorkingDirectory`
   - `Environment=PATH=...`
   - `ExecStart`
5. Reload systemd and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now yt-caption-api
sudo systemctl status yt-caption-api
```

6. Check logs if needed:

```bash
sudo journalctl -u yt-caption-api -f
```

7. Test the API locally on the server:

```bash
curl "http://127.0.0.1:8000/transcript/Ol18JoeXlVI?language=en" \
  -H "X-AccessToken: 23dbce5530c211ee939900ff79794a8f"
```

## Endpoints

### `GET /health`

Returns a basic health check.

### `GET /transcript/{video_id}?language=en`

Example curl:

```powershell
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ?language=en" `
  -H "X-AccessToken: 23dbce5530c211ee939900ff79794a8f"
```

Smaller responses:

```powershell
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ?language=en&include_meta=false" `
  -H "X-AccessToken: 23dbce5530c211ee939900ff79794a8f"
curl.exe "http://127.0.0.1:8000/transcript/dQw4w9WgXcQ?language=en&max_chars=500" `
  -H "X-AccessToken: 23dbce5530c211ee939900ff79794a8f"
```

## Notes

- `cookies.txt` is expected in the project root.
- Requests to `/transcript/...` must include `X-AccessToken`.
- Node should be installed and available on `PATH`.
- `yt-dlp` extraction retries can be tuned with `YT_DLP_RETRY_ATTEMPTS` and `YT_DLP_RETRY_DELAY_SECONDS`.
- The API prefers normal subtitles and falls back to auto captions by default.
- You can disable auto captions with `?include_auto=false`.
- Captions are cached on disk in `cache/` by video ID + language + auto-caption mode.
- Cache files older than 7 days are deleted automatically on startup and before new cache writes.
- A background cleanup job also prunes expired cache files every 6 hours while the API is running.
- You can bypass cache with `?use_cache=false`.
- Responses include `X-Cache: HIT` or `X-Cache: MISS`.
- `json3` caption tracks are converted into plain text. Other track types are returned as raw text.
