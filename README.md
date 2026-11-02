# Video Streaming

FastAPI + OpenCV + FFmpeg service that streams video files, webcams and RTSP cameras to the browser.

## Architecture

| Source | Default protocol | How |
|---|---|---|
| Webcam / USB | **MJPEG** | OpenCV capture thread → one shared JPEG encode → `multipart/x-mixed-replace` |
| Video file (mp4/avi/mov/mkv/webm) | **HLS** (MJPEG if FFmpeg is missing) | Supervised FFmpeg subprocess writes rolling `.ts` segments |
| RTSP camera | **HLS** (MJPEG if FFmpeg is missing) | Same, with `-rtsp_transport tcp` and automatic restart with exponential backoff |

Any file/RTSP stream can be forced to `mjpeg` or `hls` with the `protocol` field. WebRTC is not included
(it needs STUN/TURN and `aiortc`); MJPEG covers the low-latency case (~100–300 ms) and HLS the efficient one (2–6 s).

```
Browser ──HTTP──> FastAPI (async) ──> StreamManager ──> MjpegStream  (thread: OpenCV → latest frame → JPEG → N viewers)
                                                   └──> HlsStream    (thread: supervises FFmpeg → /hls dir → static serve)
```

Key decisions
- **Threads, not the event loop, run OpenCV/FFmpeg.** Blocking stop/join calls go through `asyncio.to_thread`.
- **Only the latest frame is kept** (no queues): bounded memory, slow viewers drop frames instead of lagging.
- **Encode once, serve many**: JPEG encoding happens only while at least one viewer is connected.
- **Files are streamed incrementally** (`grab`/`retrieve`, frame skipping to hit the target fps), never loaded whole.
- **Security**: file sources are confined to `VS_MEDIA_DIR`; RTSP URLs must be `rtsp(s)://`; credentials are masked in
  API responses, validation errors, logs, and FFmpeg stderr. Credentials live only in process memory.
  (Note: while FFmpeg runs, the URL is visible in the OS process list to local users; use a dedicated low-privilege camera account.)
- **One process owns the registry.** Run a single uvicorn worker (see Scaling).

## Requirements
Python 3.10+, FFmpeg (for HLS only; MJPEG works without it).

## Local development

```bash
python -m venv .venv
.venv/Scripts/activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # optional
# put videos in ./media
uvicorn app.main:app --reload     # http://localhost:8000  (API docs: /docs)
```

FFmpeg 5.0+ is required (older versions ignore the RTSP timeout option). `VS_HLS_DIR` must be dedicated to this app:
on startup, leftover stream directories (12-hex-character names) in it are deleted.

Install FFmpeg: Windows `winget install Gyan.FFmpeg` (or download from ffmpeg.org and set `VS_FFMPEG_PATH`),
Debian/Ubuntu `sudo apt install ffmpeg`, macOS `brew install ffmpeg`.

The frontend loads hls.js from jsDelivr (pinned version). For air-gapped installs, download it into `frontend/` and edit `index.html`.

## API

| Method | Path | Description |
|---|---|---|
| POST | `/api/streams/start` | Start a stream → `201` + `StreamInfo` |
| POST | `/api/streams/stop/{id}` | Stop and clean up → `200` + final `StreamInfo` |
| GET | `/api/streams` | `{count, streams[]}` |
| GET | `/api/streams/{id}/status` | State, error, fps, viewers, reconnect count, uptime |
| GET | `/api/streams/{id}/video` | MJPEG stream, or `307` redirect to the HLS playlist |
| GET | `/api/streams/{id}/snapshot` | Single JPEG (MJPEG streams) |
| GET | `/api/media` | Files available in the media dir + `ffmpeg_available` |
| GET | `/api/health` | Liveness |

Interactive docs: `/docs`. Example:

```bash
curl -X POST localhost:8000/api/streams/start -H 'content-type: application/json' \
  -d '{"source_type":"rtsp","source":"rtsp://user:pass@192.168.1.10:554/stream1","fps":15,"width":1280,"height":720}'
```

Request fields: `source_type` (`file|webcam|rtsp`), `source` (file path under media dir / device index / RTSP URL),
`protocol` (`mjpeg|hls`, optional), `fps` (1–60), `width`+`height` (even, together), `loop` (files).

States: `starting → running ⇄ reconnecting → error`, or `ended` (non-looping file), `stopped`.
Errors: `404` unknown stream, `422` invalid input/source, `429` stream limit, `503` HLS requested without FFmpeg.
RTSP connection problems do not fail the start call; the stream enters `reconnecting` and the reason appears in `error`.
A webcam that has never opened (wrong index, camera busy) fails fast with `error`; an unplugged webcam that worked before is retried.
A file never plays faster than its native fps (requesting 60 fps for a 30 fps video yields 30).
`viewers` counts open MJPEG connections (each browser tab showing the stream is one viewer). HLS viewers are not counted.
Every page shows every stream on the server, so a second browser tab on the same server adds a viewer to each running MJPEG stream.

## Live voice

The page has a **Voice** panel. One person talks; everyone else listens.

- **Speak:** click *Start talking*, allow the microphone, use *Mute/Unmute* and watch the level meter. *Stop talking* releases the mic.
- **Listen:** click *Listen* (browsers only allow audio after a click). Volume slider and level meter included. Viewers who
  haven't clicked *Listen* receive no audio data; the speaker sees how many people are listening.
- Only **one speaker at a time**; a second person gets "Someone else is already speaking". The slot frees if the speaker
  closes the tab or loses connection.
- **How it works:** microphone → 16 kHz mono 16-bit PCM in 20 ms frames → WebSocket `/ws/audio/talk` → server fan-out →
  `/ws/audio/listen` → Web Audio with a ~100 ms jitter buffer. Latency ≈ 20 ms capture + network + ~100 ms playout.
  A slow listener skips audio (it never lags further behind or slows anyone else). Nothing is recorded or stored.
  WebSockets were chosen over WebRTC because they work through HTTP tunnels/proxies (Cloudflare etc.).
- **Requirements:** the page must be opened over **https://** or `http://localhost` (browser rule for microphone access).
  Plain `http://<LAN-IP>` will not allow the microphone.
- **Security:** voice sits behind the same login as the rest of the app. WebSockets also reject foreign `Origin`s (stops other
  websites from using your login to listen/talk). Behind a proxy the public host is accepted via `X-Forwarded-Host`;
  add others with `VS_ALLOWED_ORIGINS='["https://my.site"]'`. Limits: `VS_MAX_LISTENERS` (default 50), frame size/rate caps.
- API: `GET /api/audio/status` → `{talking, muted, listeners, listening, sample_rate}`.
- **Anyone with the login can become the speaker** (there is one shared password, no separate host role).

Tested in Edge (Chromium) and Firefox with a fake microphone: speaker + several listeners, mute/unmute, late joiners,
second-speaker refusal, reconnect after connection drops, permission-denied/no-mic/busy-mic/insecure-page messages.
Not tested: real Safari/iOS (no Web Audio in the Playwright WebKit build used here), real microphones/hardware, and mobile
browsers. Safari needs 14.1+ (AudioWorklet).

## Configuration
All via `VS_*` environment variables / `.env` — see [.env.example](.env.example).

## Tests

```bash
pytest -q
```
Tests need no camera, network or FFmpeg (a fake capture and a stub FFmpeg are used). They cover start/stop,
invalid sources, RTSP failures and give-up, disconnect + recovery, concurrent streams and the limit,
resource cleanup (threads, captures, HLS dirs, processes), validation, and credential masking.
One extra test runs real FFmpeg when it is installed.

Manual check: start the server, open http://localhost:8000, pick a file from `media/`, press **Start**.
Open several streams to see the grid; try an unreachable RTSP URL to watch the `reconnecting` badge.

## Deployment

### Docker
```bash
mkdir -p media && cp some.mp4 media/
docker compose up --build -d
```
Image includes FFmpeg, runs as non-root, mounts `./media` read-only and keeps HLS segments in a tmpfs.
Webcams: on Linux uncomment `devices: /dev/video0` in the compose file (not possible on Docker Desktop for Windows/macOS).

### Linux (systemd)
```bash
sudo apt install ffmpeg python3-venv
sudo useradd -r -m -G video vs && sudo -u vs git clone <repo> /opt/video-streaming
cd /opt/video-streaming && sudo -u vs python3 -m venv .venv && sudo -u vs .venv/bin/pip install -r requirements.txt
```
`/etc/systemd/system/video-streaming.service`:
```ini
[Unit]
Description=Video streaming
After=network.target
[Service]
User=vs
WorkingDirectory=/opt/video-streaming
EnvironmentFile=/opt/video-streaming/.env
ExecStart=/opt/video-streaming/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
Restart=always
[Install]
WantedBy=multi-user.target
```
Put nginx/Caddy in front for TLS and auth. For streaming, disable proxy buffering:
`proxy_buffering off; proxy_read_timeout 1h;` on `/api/streams/` and allow caching of `/hls/*.ts`.

### Logging & monitoring
Logs go to stdout (journald / `docker logs`) with credentials scrubbed. `VS_LOG_LEVEL=DEBUG` adds FFmpeg stderr.
Use `/api/health` for liveness probes and poll `/api/streams` (state, `reconnect_count`, `frames_published`) for alerting
on streams stuck in `reconnecting`/`error`.

**This app has no authentication.** Anyone who can reach it can start streams (and use your RTSP credentials
if they are supplied). Put it behind a reverse proxy with auth before exposing it beyond a trusted network.

## Performance & scaling
- Prefer **HLS for many viewers** (static files, CDN/nginx-cacheable); **MJPEG for few viewers needing low latency**
  (~10× the bandwidth of H.264, but a single encode per stream regardless of viewer count).
- Cap resolution/fps per camera (`width`/`height`/`fps`); decode and encode cost scale with pixel rate.
  `libx264 veryfast` costs roughly 0.5–1 core per 1080p30 stream. For many cameras, use hardware encoders
  (`h264_nvenc`, `h264_vaapi`) by editing `_build_cmd`, or `-c:v copy` for H.264 RTSP sources (zero transcode, but keyframe-dependent latency).
- Set `VS_HLS_DIR` to tmpfs. Use `VS_HLS_SEGMENT_SECONDS=1` for lower latency (at higher overhead).
- Python threads share a GIL; OpenCV releases it during decode, so ~dozens of MJPEG streams per core-set are fine.
  Beyond that, shard cameras across several instances (the registry is per-process, so use sticky routing by stream id)
  or move capture into worker processes.
- Long-running stability: workers reconnect with exponential backoff (`VS_RECONNECT_*`), FFmpeg is restarted on exit,
  and HLS segments are deleted by FFmpeg as the window rolls.
- Not included (future): WebRTC (`aiortc` + TURN) for sub-second latency, authentication, persistence of streams across restarts.
