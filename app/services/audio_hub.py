"""Real-time voice relay: one speaker, many listeners.

Audio is raw 16-bit little-endian mono PCM at ``SAMPLE_RATE`` in small (~20 ms) frames. The hub only
copies frames from the speaker to each listener's bounded queue; it never decodes or stores audio.
Everything runs on the event loop, so no locks are needed. A slow listener drops its *oldest* frames
(real-time audio prefers skipping to lagging) and can never stall the speaker or other listeners.
"""

import asyncio

SAMPLE_RATE = 16_000
MAX_FRAME_BYTES = 16 * 1024  # a normal frame is 640 bytes (20 ms); this is just an abuse cap
MAX_BYTES_PER_SECOND = SAMPLE_RATE * 2 * 4  # speaker may burst to 4x real time, no more


class Listener:
    def __init__(self, queue_size: int):
        self.queue: asyncio.Queue[bytes | dict] = asyncio.Queue(maxsize=queue_size)
        self.dropped = 0
        self.listening = False  # True once the viewer pressed Listen; only then are audio frames sent


class AudioHub:
    def __init__(self, max_listeners: int = 50, queue_size: int = 100):
        self.max_listeners = max_listeners
        self._queue_size = queue_size  # 100 frames = 2 s of audio
        self._listeners: set[Listener] = set()
        self._talker: object | None = None
        self.muted = False
        self.frames_relayed = 0

    # -- state -------------------------------------------------------------
    @property
    def talking(self) -> bool:
        return self._talker is not None

    @property
    def listener_count(self) -> int:
        """Connected viewers (including those with audio off)."""
        return len(self._listeners)

    @property
    def listening_count(self) -> int:
        """Viewers who actually have audio on."""
        return sum(1 for l in self._listeners if l.listening)

    def state(self) -> dict:
        return {
            "type": "state",
            "talking": self.talking,
            "muted": self.muted,
            "listeners": self.listener_count,
            "listening": self.listening_count,
            "sample_rate": SAMPLE_RATE,
        }

    # -- speaker -----------------------------------------------------------
    def claim_talker(self, token: object) -> bool:
        if self._talker is not None:
            return False
        self._talker, self.muted = token, False
        self._broadcast_state()
        return True

    def release_talker(self, token: object) -> None:
        if self._talker is token:
            self._talker, self.muted = None, False
            self._broadcast_state()

    def set_muted(self, token: object, muted: bool) -> None:
        if self._talker is token and self.muted != muted:
            self.muted = muted
            self._broadcast_state()

    def publish(self, token: object, frame: bytes) -> bool:
        """Relay one frame to all listeners. Returns False if dropped (not the speaker / muted)."""
        if self._talker is not token or self.muted:
            return False
        self.frames_relayed += 1
        self._push(frame, audio=True)
        return True

    # -- listeners ---------------------------------------------------------
    def add_listener(self) -> Listener | None:
        if len(self._listeners) >= self.max_listeners:
            return None
        listener = Listener(self._queue_size)
        self._listeners.add(listener)
        self._broadcast_state(exclude=listener)  # newcomer gets its own hello instead
        return listener

    def set_listening(self, listener: Listener, on: bool) -> None:
        if listener in self._listeners and listener.listening != on:
            listener.listening = on
            self._broadcast_state()

    def remove_listener(self, listener: Listener) -> None:
        if listener in self._listeners:
            self._listeners.discard(listener)
            self._broadcast_state()

    # -- internals ---------------------------------------------------------
    def _broadcast_state(self, exclude: Listener | None = None) -> None:
        self._push(self.state(), exclude)

    def _push(self, item: bytes | dict, exclude: Listener | None = None, audio: bool = False) -> None:
        for listener in self._listeners:
            if listener is exclude or (audio and not listener.listening):
                continue
            q = listener.queue
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                    listener.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(item)
