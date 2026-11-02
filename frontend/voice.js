"use strict";
/*
 * Live voice. One person talks (microphone -> server), everyone else listens.
 * Audio is 16 kHz mono 16-bit PCM in 20 ms frames over WebSockets, played back with Web Audio.
 */

const VOICE = { rate: 16000, frameSamples: 320, jitterSeconds: 0.1, maxLatencySeconds: 0.6 };

function voiceWsUrl(path) {
  return `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${path}`;
}

function micErrorMessage(err) {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    return "Microphone access needs a secure connection. Open this page with https:// (or on localhost).";
  }
  switch (err?.name) {
    case "NotAllowedError": case "SecurityError": case "PermissionDeniedError":
      return "Microphone permission was denied. Click the lock icon in the address bar, allow the microphone, then try again.";
    case "NotFoundError": case "DevicesNotFoundError": case "OverconstrainedError":
      return "No microphone was found. Connect one and try again.";
    case "NotReadableError": case "TrackStartError": case "AbortError":
      return "The microphone is busy or can't be opened. Close other apps that use it and try again.";
    default:
      return `Could not start the microphone: ${err?.message || err}`;
  }
}

// ---- speaker ---------------------------------------------------------------------------------------
class VoiceBroadcaster {
  // callbacks: onStatus(state, text), onLevel(0..1), onError(text)
  constructor(callbacks) {
    this.cb = callbacks;
    this.active = false;
    this.muted = false;
    this.stats = { framesSent: 0, framesDropped: 0 };
    this._resetResampler();
  }

  async start() {
    if (this.active) return;
    this.active = true;
    this.muted = false;
    this.cb.onStatus("connecting", "Starting microphone…");
    try {
      if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) throw new Error("insecure");
      if (!window.AudioContext && !window.webkitAudioContext) throw new Error("This browser has no Web Audio support.");
      this.stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
      });
      await this._setupAudioGraph();
      this.track = this.stream.getAudioTracks()[0];
      this.track.addEventListener("ended", () => this._fail("The microphone was disconnected or its permission was revoked."));
      this.track.addEventListener("mute", () => this.cb.onStatus("live", "Microphone is muted by the system or another app."));
      this.track.addEventListener("unmute", () => this.cb.onStatus("live", "Live"));
      await this._connect();
    } catch (err) {
      // Browser (DOMException) errors get a friendly microphone explanation; our own errors already read well.
      const message = err?.message === "insecure" || err instanceof DOMException ? micErrorMessage(err) : String(err?.message || err);
      this._teardown();
      this.cb.onError(message);
      this.cb.onStatus("idle", "");
      throw err;
    }
  }

  async _setupAudioGraph() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (typeof AudioWorkletNode === "undefined") throw new Error("This browser is too old for live audio.");
    // Prefer a 16 kHz context (browser does high-quality resampling); fall back to the device rate.
    for (const options of [{ sampleRate: VOICE.rate, latencyHint: "interactive" }, { latencyHint: "interactive" }]) {
      try {
        this.ctx = new Ctx(options);
        await this.ctx.audioWorklet.addModule("pcm-worklet.js");
        this.source = this.ctx.createMediaStreamSource(this.stream);
        break;
      } catch (err) {
        await this.ctx?.close().catch(() => {});
        this.ctx = null;
        if (options.sampleRate === undefined) throw err;
      }
    }
    await this.ctx.resume();
    this.inRate = this.ctx.sampleRate;
    this._resetResampler();
    this.node = new AudioWorkletNode(this.ctx, "pcm-capture");
    this.node.port.onmessage = (e) => this._onSamples(e.data);
    const silent = this.ctx.createGain(); // keeps the graph pulled without playing the mic back (no echo)
    silent.gain.value = 0;
    this.source.connect(this.node).connect(silent).connect(this.ctx.destination);
  }

  // -- websocket (with automatic reconnect while the microphone stays open) --
  _connect() {
    return new Promise((resolve, reject) => {
      let settled = false;
      const ws = new WebSocket(voiceWsUrl("/ws/audio/talk"));
      ws.binaryType = "arraybuffer";
      this.ws = ws;
      ws.onopen = () => { /* wait for the server's "ready" */ };
      ws.onmessage = (ev) => {
        if (typeof ev.data !== "string") return;
        const msg = JSON.parse(ev.data);
        if (msg.type === "ready") {
          this.reconnectAttempts = 0;
          ws.send(JSON.stringify({ type: "mute", muted: this.muted }));
          this.cb.onStatus("live", this.muted ? "Muted" : "Live");
          if (!settled) { settled = true; resolve(); }
        } else if (msg.type === "error") {
          const error = new Error(msg.message || "Server refused the connection.");
          error.name = msg.code === "busy" ? "Busy" : "ServerError";
          this._busy = true;
          if (!settled) { settled = true; reject(error); } else this._fail(error.message);
        }
      };
      ws.onclose = () => {
        if (!settled) { settled = true; reject(new Error("Could not connect to the voice server (are you logged in?).")); return; }
        if (this.active && !this._busy) this._reconnect();
      };
    });
  }

  async _reconnect() {
    if (this._reconnecting) return;
    this._reconnecting = true;
    this.reconnectAttempts = (this.reconnectAttempts || 0) + 1;
    this.cb.onStatus("reconnecting", "Connection lost, reconnecting…");
    while (this.active && this.reconnectAttempts <= 10) {
      await new Promise((r) => setTimeout(r, Math.min(500 * 2 ** (this.reconnectAttempts - 1), 8000)));
      if (!this.active) break;
      try { await this._connect(); this._reconnecting = false; return; } catch (e) {
        if (this._busy) break;
        this.reconnectAttempts++;
      }
    }
    this._reconnecting = false;
    if (this.active) this._fail("Lost the connection to the server.");
  }

  // -- capture pipeline --
  _resetResampler() {
    this.pos = 0; this.idx = 0; this.prev = 0;
    this.frame = new Int16Array(VOICE.frameSamples); this.fill = 0;
  }

  _onSamples(input) {
    if (!this.active) return;
    const ratio = (this.inRate || VOICE.rate) / VOICE.rate;
    let energy = 0;
    for (let n = 0; n < input.length; n++) {
      const x = input[n];
      if (ratio === 1) {
        this._pushSample(x);
      } else {
        // linear-interpolating resampler, used only when the browser couldn't give a 16 kHz context
        while (this.pos <= this.idx) {
          this._pushSample(this.prev + (x - this.prev) * (this.pos - (this.idx - 1)));
          this.pos += ratio;
        }
        this.prev = x;
        this.idx++;
      }
      energy += x * x;
    }
    this.cb.onLevel(this.muted ? 0 : Math.min(1, Math.sqrt(energy / input.length) * 4));
  }

  _pushSample(x) {
    this.frame[this.fill++] = Math.max(-1, Math.min(1, x)) * 32767;
    if (this.fill < VOICE.frameSamples) return;
    this.fill = 0;
    const ws = this.ws;
    if (this.muted || !ws || ws.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > 32768) { this.stats.framesDropped++; return; } // network can't keep up: stay real-time
    ws.send(this.frame.slice().buffer);
    this.stats.framesSent++;
  }

  // -- controls --
  setMuted(muted) {
    this.muted = muted;
    if (this.track) this.track.enabled = !muted;
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "mute", muted }));
    if (muted) this.cb.onLevel(0);
    this.cb.onStatus("live", muted ? "Muted" : "Live");
  }

  stop() {
    this._teardown();
    this.cb.onStatus("idle", "");
  }

  _fail(message) {
    if (!this.active) return;
    this._teardown();
    this.cb.onError(message);
    this.cb.onStatus("idle", "");
  }

  _teardown() {
    this.active = false;
    this._busy = false;
    this._reconnecting = false;
    try { this.ws?.close(); } catch { /* already closed */ }
    this.ws = null;
    this.stream?.getTracks().forEach((t) => t.stop()); // releases the mic: the browser's recording indicator goes off
    this.stream = null;
    try { this.node?.disconnect(); this.source?.disconnect(); } catch { /* ignore */ }
    this.ctx?.close().catch(() => {});
    this.ctx = this.node = this.source = this.track = null;
    this.cb.onLevel(0);
  }
}

// ---- listener --------------------------------------------------------------------------------------
class VoiceListener {
  // callbacks: onState({talking, muted, listeners}), onConnection(connected), onLevel(0..1)
  constructor(callbacks) {
    this.cb = callbacks;
    this.enabled = false;
    this.volume = 1;
    this.rate = VOICE.rate;
    this.nextTime = 0;
    this.stats = { framesPlayed: 0, framesDropped: 0, underruns: 0, framesReceived: 0 };
    this.delay = 1000;
    this._connect();
    setInterval(() => this.ws?.readyState === WebSocket.OPEN && this.ws.send("ping"), 20000); // keep proxies from idling us out
    setInterval(() => this._measureLevel(), 100);
  }

  _connect() {
    const ws = new WebSocket(voiceWsUrl("/ws/audio/listen"));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onopen = () => { this.delay = 1000; this.cb.onConnection(true); this._announce(); };
    ws.onmessage = (ev) => {
      if (typeof ev.data === "string") {
        const msg = JSON.parse(ev.data);
        if (msg.type === "state") { this.rate = msg.sample_rate || VOICE.rate; this.cb.onState(msg); }
        else if (msg.type === "error") this.cb.onState({ error: msg.message });
        // {"type":"listening"} is just the server's acknowledgement
      } else {
        this.stats.framesReceived++;
        this._play(ev.data);
      }
    };
    ws.onclose = () => {
      this.cb.onConnection(false);
      setTimeout(() => this._connect(), this.delay); // keep trying forever, backing off up to 10 s
      this.delay = Math.min(this.delay * 2, 10000);
    };
  }

  // Tell the server whether we want audio frames (it sends none to viewers who haven't pressed Listen).
  _announce() {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify({ type: "listening", on: this.enabled }));
  }

  // Must be called from a click: browsers only allow audio after a user gesture.
  async enable() {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) throw new Error("This browser has no Web Audio support.");
    if (!this.ctx) {
      this.ctx = new Ctx({ latencyHint: "interactive" });
      this.gain = this.ctx.createGain();
      this.gain.gain.value = this.volume;
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 512;
      this.gain.connect(this.analyser).connect(this.ctx.destination);
    }
    await this.ctx.resume();
    this.nextTime = 0;
    this.enabled = true;
    this._announce();
  }

  disable() {
    this.enabled = false;
    this._announce();
    this.cb.onLevel(0);
  }

  setVolume(v) {
    this.volume = v;
    if (this.gain) this.gain.gain.value = v;
  }

  _play(buffer) {
    if (!this.enabled || !this.ctx || this.ctx.state !== "running") return;
    const n = buffer.byteLength >> 1;
    if (!n) return;
    const pcm = new Int16Array(buffer, 0, n);
    const audio = this.ctx.createBuffer(1, n, this.rate);
    const channel = audio.getChannelData(0);
    for (let i = 0; i < n; i++) channel[i] = pcm[i] / 32768;

    const now = this.ctx.currentTime;
    if (this.nextTime < now + 0.005) {
      // First frame, or we ran dry: restart with a small cushion that absorbs network jitter.
      if (this.nextTime > 0 && now - this.nextTime < 1) this.stats.underruns++;
      this.nextTime = now + VOICE.jitterSeconds;
    } else if (this.nextTime - now > VOICE.maxLatencySeconds) {
      this.stats.framesDropped++; // buffer grew (burst after a stall): skip to stay in real time
      return;
    }
    const src = this.ctx.createBufferSource();
    src.buffer = audio;
    src.connect(this.gain);
    src.start(this.nextTime);
    this.nextTime += audio.duration;
    this.stats.framesPlayed++;
  }

  _measureLevel() {
    if (!this.enabled || !this.analyser) return;
    const data = new Float32Array(this.analyser.fftSize);
    this.analyser.getFloatTimeDomainData(data);
    let sum = 0;
    for (const x of data) sum += x * x;
    this.level = Math.sqrt(sum / data.length);
    this.cb.onLevel(Math.min(1, this.level * 4));
  }

  get bufferedSeconds() {
    return this.ctx && this.enabled ? Math.max(0, this.nextTime - this.ctx.currentTime) : 0;
  }
}

// ---- page wiring ----------------------------------------------------------------------------------
(function initVoiceUi() {
  const $ = (id) => document.getElementById(id);
  const talkBtn = $("talk-btn"), muteBtn = $("mute-btn"), listenBtn = $("listen-btn");
  let serverState = { talking: false, muted: false, listeners: 0 };
  let talking = false;

  const setVoiceError = (text) => { $("voice-error").textContent = text || ""; };

  function renderListenStatus() {
    const s = serverState;
    let text;
    if (s.error) text = s.error;
    else if (talking) text = "You are the speaker.";
    else if (!s.talking) text = "Nobody is speaking.";
    else if (s.muted) text = "The speaker is muted.";
    else text = listener.enabled ? "Speaker is live — you're hearing them." : "Speaker is live. Click Listen to hear them.";
    $("listen-status").textContent = text;
    const n = serverState.listening || 0;
    $("talk-count").textContent = talking ? `${n} listening` : "";
    listenBtn.disabled = talking; // listening while speaking would feed back into the mic
  }

  const listener = new VoiceListener({
    onState: (s) => { serverState = { ...serverState, ...s, error: s.error }; renderListenStatus(); },
    onConnection: (ok) => {
      $("voice-conn").textContent = ok ? "" : "Voice server unreachable — retrying…";
      if (!ok) { serverState = { talking: false, muted: false, listeners: 0 }; renderListenStatus(); }
    },
    onLevel: (l) => { $("listen-level").style.width = `${Math.round(l * 100)}%`; },
  });

  const broadcaster = new VoiceBroadcaster({
    onStatus: (state, text) => {
      talking = state === "live" || state === "reconnecting" || state === "connecting";
      talkBtn.textContent = talking ? "⏹ Stop talking" : "🎤 Start talking";
      talkBtn.classList.toggle("on-air", state === "live");
      muteBtn.hidden = !talking;
      muteBtn.textContent = broadcaster.muted ? "Unmute" : "Mute";
      $("mic-meter").hidden = !talking;
      $("talk-status").textContent = text;
      if (talking && listener.enabled) { listener.disable(); listenBtn.textContent = "🔊 Listen"; }
      renderListenStatus();
    },
    onLevel: (l) => { $("mic-level").style.width = `${Math.round(l * 100)}%`; },
    onError: setVoiceError,
  });

  talkBtn.addEventListener("click", async () => {
    setVoiceError("");
    if (broadcaster.active) { broadcaster.stop(); return; }
    talkBtn.disabled = true;
    try { await broadcaster.start(); } catch { /* message already shown */ }
    finally { talkBtn.disabled = false; }
  });

  muteBtn.addEventListener("click", () => broadcaster.setMuted(!broadcaster.muted));

  listenBtn.addEventListener("click", async () => {
    setVoiceError("");
    if (listener.enabled) { listener.disable(); listenBtn.textContent = "🔊 Listen"; }
    else {
      try { await listener.enable(); listenBtn.textContent = "🔇 Stop listening"; }
      catch (e) { setVoiceError(e.message); }
    }
    renderListenStatus();
  });

  $("volume").addEventListener("input", (e) => listener.setVolume(e.target.value / 100));
  window.addEventListener("pagehide", () => broadcaster.active && broadcaster.stop());

  window.voice = { broadcaster, listener }; // handy for debugging in the console
  renderListenStatus();
})();
