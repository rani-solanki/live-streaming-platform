"use strict";

const $ = (id) => document.getElementById(id);
const tiles = new Map(); // stream_id -> {el, hls, playing}

function showError(message) {
  $("errors").textContent = message || "";
}

function formatDetail(detail) {
  if (Array.isArray(detail)) {
    return detail.map((e) => `${(e.loc || []).filter((p) => p !== "body").join(".")}: ${e.msg}`).join("\n");
  }
  return String(detail);
}

async function api(path, options) {
  const res = await fetch(path, options);
  let body = null;
  try { body = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error(formatDetail(body?.detail ?? `${res.status} ${res.statusText}`));
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---- form ---------------------------------------------------------------
const savedText = { webcam: "0", rtsp: "" }; // separate memory per source type
let currentType = "file";

function updateSourceInput() {
  const type = $("source-type").value;
  if (currentType !== "file") savedText[currentType] = $("source-text").value;
  currentType = type;

  const isFile = type === "file";
  $("source-file").hidden = !isFile;
  $("source-text").hidden = isFile;
  $("loop-wrap").hidden = !isFile;
  if (!isFile) {
    $("source-text").type = type === "rtsp" ? "password" : "text"; // hide camera credentials on screen
    $("source-text").placeholder = type === "rtsp" ? "rtsp://user:pass@host:554/stream" : "Device index (0)";
    $("source-text").value = savedText[type];
  } else {
    loadMedia();
  }
  updateStartButton();
}

let mediaFiles = [];
async function loadMedia() {
  const select = $("source-file");
  try {
    const { files } = await api("/api/media");
    mediaFiles = files;
    const previous = select.value;
    if (files.length) {
      select.replaceChildren(...files.map((f) => new Option(f, f)));
      if (files.includes(previous)) select.value = previous;
    } else {
      select.replaceChildren(new Option("(no videos found in media/)", ""));
    }
    updateStartButton();
  } catch (e) { showError(e.message); }
}

function updateStartButton() {
  const noFiles = $("source-type").value === "file" && mediaFiles.length === 0;
  $("start-btn").disabled = noFiles;
  $("start-btn").title = noFiles ? "Put a video file in the media/ folder first" : "";
}

$("start-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  showError("");
  const type = $("source-type").value;
  const source = type === "file" ? $("source-file").value : $("source-text").value.trim();
  if (!source) { showError(type === "file" ? "Select a video file." : "Enter a source."); return; }
  const num = (id) => ($(id).value ? Number($(id).value) : undefined);
  const body = {
    source_type: type, source,
    protocol: $("protocol").value || undefined,
    fps: num("fps"), width: num("width"), height: num("height"),
    loop: $("loop").checked,
  };
  const button = $("start-btn");
  button.disabled = true;
  try {
    await api("/api/streams/start", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (type === "rtsp") { $("source-text").value = ""; savedText.rtsp = ""; } // don't keep credentials around
    await refresh();
  } catch (e) { showError(e.message); }
  finally { updateStartButton(); }
});
$("source-type").addEventListener("change", updateSourceInput);
// pick up files added to media/ while the page is open
$("source-file").addEventListener("mousedown", loadMedia);
$("source-file").addEventListener("focus", loadMedia);
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadMedia(); });

// ---- tiles --------------------------------------------------------------
function createTile(s) {
  const el = document.createElement("section");
  el.className = "tile";
  el.dataset.id = s.stream_id;
  el.innerHTML = `
    <div class="screen"></div>
    <div class="meta">
      <div><div class="title"></div><div class="sub"></div></div>
      <div><span class="badge"></span> <button class="stop" type="button">Stop</button></div>
    </div>
    <div class="err"></div>`;
  el.querySelector(".title").textContent = s.source;
  const stop = el.querySelector(".stop");
  stop.addEventListener("click", async () => {
    stop.disabled = true;
    try {
      await api(`/api/streams/stop/${s.stream_id}`, { method: "POST" });
    } catch (e) {
      if (e.status !== 404) { showError(e.message); stop.disabled = false; return; } // 404 = already stopped
    }
    removeTile(s.stream_id);
  });
  $("grid").append(el);
  return { el, hls: null, playing: false };
}

function startPlayer(tile, s) {
  const screen = tile.el.querySelector(".screen");
  if (s.protocol === "mjpeg") {
    const img = document.createElement("img");
    img.alt = "";
    img.src = s.video_url;
    screen.replaceChildren(img);
  } else {
    const video = document.createElement("video");
    video.muted = true; video.autoplay = true; video.playsInline = true; video.controls = true;
    screen.replaceChildren(video);
    if (window.Hls && Hls.isSupported()) {
      tile.hls = new Hls({ liveSyncDurationCount: 2, lowLatencyMode: true });
      tile.hls.loadSource(s.playback_url);
      tile.hls.attachMedia(video);
      tile.hls.on(Hls.Events.ERROR, (_, data) => {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) tile.hls.startLoad();
        else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) tile.hls.recoverMediaError();
      });
    } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = s.playback_url; // Safari: native HLS
    } else {
      showError("HLS playback needs the hls.js library, which failed to load (offline?). Use the MJPEG protocol instead.");
    }
  }
  tile.playing = true;
}

function destroyPlayer(tile) {
  tile.hls?.destroy();
  tile.el.querySelector(".screen").replaceChildren(); // removing the <img> closes the MJPEG connection
}

function removeTile(id) {
  const tile = tiles.get(id);
  if (!tile) return;
  destroyPlayer(tile);
  tile.el.remove();
  tiles.delete(id);
  render();
}

function updateTile(tile, s) {
  const badge = tile.el.querySelector(".badge");
  badge.textContent = s.state;
  badge.className = `badge ${s.state}`;
  const bits = [s.protocol.toUpperCase(), `${s.fps} fps`];
  if (s.width) bits.push(`${s.width}×${s.height}`);
  if (s.protocol === "mjpeg") bits.push(`${s.viewers} viewer${s.viewers === 1 ? "" : "s"}`);
  if (s.reconnect_count) bits.push(`${s.reconnect_count} reconnect${s.reconnect_count === 1 ? "" : "s"}`);
  tile.el.querySelector(".sub").textContent = bits.join(" · ");
  tile.el.querySelector(".err").textContent = s.error || "";
  if (!tile.playing && s.state === "running") startPlayer(tile, s);
}

function render() {
  $("empty").hidden = tiles.size > 0;
  $("summary").textContent = tiles.size ? `${tiles.size} active` : "";
}

let refreshing = false, rerun = false;
async function refresh() {
  if (refreshing) { rerun = true; return; } // don't drop a refresh requested mid-poll
  refreshing = true;
  try {
    do {
      rerun = false;
      const { streams } = await api("/api/streams");
      const seen = new Set();
      for (const s of streams) {
        seen.add(s.stream_id);
        if (!tiles.has(s.stream_id)) tiles.set(s.stream_id, createTile(s));
        updateTile(tiles.get(s.stream_id), s);
      }
      for (const id of [...tiles.keys()]) if (!seen.has(id)) removeTile(id);
      render();
      if ($("errors").textContent.startsWith("Lost connection")) showError("");
    } while (rerun);
  } catch (e) { showError(`Lost connection to server: ${e.message}`); }
  finally { refreshing = false; }
}

updateSourceInput();
refresh();
setInterval(refresh, 2000);
