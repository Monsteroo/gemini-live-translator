"""OpenAI gpt-realtime-translate client — streaming speech-to-speech translation.

Used for the OUTGOING direction (you → foreigner): your Ukrainian speech is
streamed in and translated English audio streams back with low latency. The
model is purely stream-based (no turn lifecycle / no response.create). Mirrors
the method surface of LiveTranslateClient so bidirectional.py can drive both.

Protocol (OpenAI Realtime *translations* endpoint):
  URL   : wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
  Auth  : Authorization: Bearer <OPENAI_API_KEY>
  Config: session.update -> session.audio.output.language = target
  Audio : PCM16 @ 24 kHz, both input and output
  Send  : {"type":"session.input_audio_buffer.append","audio": <b64>}
  Recv  : session.output_audio.delta (b64 PCM16), session.output_transcript.delta
          (target text), session.input_transcript.delta (source text)

NOTE: gpt-realtime-translate has only ~13 OUTPUT languages — Ukrainian is NOT one
(it IS a valid input). So this engine is used only for the →foreigner direction;
hearing the foreigner in Ukrainian stays on Qwen (livetranslate_client.py).
"""
import time
import base64
import asyncio
import json
import queue
import threading
import traceback

import websockets
import pyaudio

API_URL = ("wss://api.openai.com/v1/realtime/translations"
           "?model=gpt-realtime-translate")
# Per the translations-endpoint docs the client event is session-prefixed.
APPEND_TYPE = "session.input_audio_buffer.append"


class OpenAITranslateClient:
    def __init__(self, api_key: str, target_language: str = "en", *,
                 audio_enabled: bool = True,
                 input_device_index: int | None = None,
                 output_device_index: int | None = None,
                 monitor_device_index: int | None = None, monitor_delay: float = 1.0,
                 label: str = "", playback_delay: float = 1.0,
                 transcription_model: str = "gpt-realtime-whisper",
                 log_latency: bool = False, quiet: bool = False,
                 pa=None, audio_dump_path: str | None = None):
        if not api_key:
            raise ValueError("API key cannot be empty.")
        self.api_key = api_key
        self.target_language = target_language
        self.audio_enabled = audio_enabled
        self.input_device_index = input_device_index
        self.output_device_index = output_device_index
        self.monitor_device_index = monitor_device_index
        self.monitor_delay = monitor_delay
        self.playback_delay = playback_delay
        self.transcription_model = transcription_model
        self.label = label
        # UI hook: on_stream(direction, text, final) for a web UI (optional).
        self.on_stream = None
        self.direction = ""
        self.log_latency = log_latency
        self.quiet = quiet
        self.audio_dump_path = audio_dump_path
        self._dump = bytearray() if audio_dump_path else None

        self.ws = None
        self.api_url = API_URL
        self.is_connected = False

        # OpenAI realtime audio is PCM16 @ 24 kHz BOTH ways (unlike Qwen's 16k in).
        self.input_rate = 24000
        self.input_chunk = 2400          # 100 ms
        self.input_format = pyaudio.paInt16
        self.input_channels = 1
        self.output_rate = 24000
        self.output_chunk = 2400
        self.output_format = pyaudio.paInt16
        self.output_channels = 1

        # Playback (main + optional monitor), each on its own thread/queue.
        self.audio_playback_queue = queue.Queue()
        self.monitor_queue = queue.Queue()
        self.audio_player_thread = None
        self.monitor_thread = None
        # Спільний екземпляр PyAudio (web.py) — щоб не падати з double-free.
        self._own_pa = pa is None
        self.pyaudio_instance = pa if pa is not None else pyaudio.PyAudio()

        # Streaming text display (incremental deltas).
        self._stream_buf = ""

    async def connect(self):
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            self.ws = await websockets.connect(self.api_url, additional_headers=headers)
            self.is_connected = True
            if not self.quiet:
                print(f"OpenAI translate connected: {self.api_url}")
            await self.configure_session()
        except Exception as e:
            print(f"OpenAI connection failed: {e}")
            self.is_connected = False
            raise

    async def configure_session(self):
        # NB: цей endpoint НЕ приймає підказку мови джерела
        # (session.audio.input.transcription.language → unknown_parameter, перевірено).
        # Та вона й не потрібна: модель точно авто-визначає українську (чиста
        # кирилиця в input_transcript), на відміну від романізації в Qwen-ASR.
        config = {
            "type": "session.update",
            "session": {
                "audio": {
                    "input": {
                        "transcription": {"model": self.transcription_model},
                        "noise_reduction": {"type": "near_field"},
                    },
                    "output": {"language": self.target_language},
                }
            },
        }
        if not self.quiet:
            print(f"{self.label}OpenAI session config: {json.dumps(config, ensure_ascii=False)}")
        await self.ws.send(json.dumps(config))

    async def send_audio_chunk(self, audio_data: bytes):
        if not self.is_connected:
            return
        await self.ws.send(json.dumps({
            "type": APPEND_TYPE,
            "audio": base64.b64encode(audio_data).decode(),
        }))

    # ---------- playback (shared loop, main + monitor) ----------
    def _player_loop(self, *, output_device_index, q, playback_delay, tag=""):
        try:
            stream = self.pyaudio_instance.open(
                format=self.output_format, channels=self.output_channels,
                rate=self.output_rate, output=True,
                output_device_index=output_device_index,
                frames_per_buffer=self.output_chunk)
        except Exception as e:
            print(f"{self.label}⚠️ вихід {tag} недоступний: {e}", flush=True)
            return
        prebuffer = int(self.output_rate * 2 * playback_delay)  # int16 mono
        buf = bytearray()
        started = ended = False
        try:
            while not ended and (self.is_connected or not q.empty() or buf):
                try:
                    chunk = q.get(timeout=0.05)
                    if chunk is None:
                        ended = True
                    else:
                        buf += chunk
                        q.task_done()
                except queue.Empty:
                    pass
                while True:
                    try:
                        c2 = q.get_nowait()
                        if c2 is None:
                            ended = True
                            break
                        buf += c2
                        q.task_done()
                    except queue.Empty:
                        break
                if not started and not ended and len(buf) < prebuffer:
                    continue
                started = True
                if buf:
                    stream.write(bytes(buf))
                    buf.clear()
            if buf:
                stream.write(bytes(buf))
        finally:
            stream.stop_stream()
            stream.close()

    def _enqueue_audio(self, data: bytes):
        self.audio_playback_queue.put(data)
        if self.monitor_device_index is not None:
            self.monitor_queue.put(data)

    def start_audio_player(self):
        if not self.audio_enabled:
            return
        if self.monitor_device_index is not None and (
                self.monitor_thread is None or not self.monitor_thread.is_alive()):
            self.monitor_thread = threading.Thread(
                target=lambda: self._player_loop(
                    output_device_index=self.monitor_device_index,
                    q=self.monitor_queue, playback_delay=self.monitor_delay,
                    tag="(монітор)"), daemon=True)
            self.monitor_thread.start()
        if self.audio_player_thread is None or not self.audio_player_thread.is_alive():
            self.audio_player_thread = threading.Thread(
                target=lambda: self._player_loop(
                    output_device_index=self.output_device_index,
                    q=self.audio_playback_queue, playback_delay=self.playback_delay,
                    tag="(основний)"), daemon=True)
            self.audio_player_thread.start()

    # ---------- streaming text display ----------
    def _print_delta(self, delta: str):
        if not delta:
            return
        if self._stream_buf == "":
            stamp = ""
            if self.log_latency:
                stamp = f"[{time.strftime('%H:%M:%S')}] "
            print(f"\n{self.label}{stamp}🌐 ", end="", flush=True)
        print(delta, end="", flush=True)
        self._stream_buf += delta
        # Завершуємо рядок на кінці речення для читабельності (немає 'done'-події).
        ended = delta.strip().endswith((".", "!", "?"))
        if self.on_stream:
            self.on_stream(self.direction, self._stream_buf, ended)
        if ended:
            print(flush=True)
            self._stream_buf = ""

    # ---------- receive loop ----------
    async def handle_server_messages(self, on_text_received=None):
        try:
            async for message in self.ws:
                event = json.loads(message)
                t = event.get("type", "")
                if t.endswith("output_audio.delta") and self.audio_enabled:
                    b64 = event.get("delta") or event.get("audio") or ""
                    if b64:
                        data = base64.b64decode(b64)
                        if self._dump is not None:
                            self._dump += data
                        self._enqueue_audio(data)
                elif t.endswith("output_transcript.delta"):
                    self._print_delta(event.get("delta") or event.get("text") or "")
                elif t.endswith("input_transcript.delta"):
                    if self.log_latency:
                        d = event.get("delta") or ""
                        if d:
                            print(f"\n{self.label}🎙 {d}", end="", flush=True)
                elif t == "error":
                    print(f"\n{self.label}[OpenAI ERROR] "
                          f"{json.dumps(event.get('error', event), ensure_ascii=False)}", flush=True)
                elif self.log_latency and t not in (
                        "session.updated", "session.created"):
                    # Невідома подія — корисно для діагностики протоколу.
                    print(f"\n{self.label}· {t}", flush=True)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[WARNING] OpenAI connection closed: {e}")
            self.is_connected = False
        except Exception as e:
            print(f"[ERROR] OpenAI message loop: {e}")
            traceback.print_exc()
            self.is_connected = False

    async def start_microphone_streaming(self):
        stream = self.pyaudio_instance.open(
            format=self.input_format, channels=self.input_channels,
            rate=self.input_rate, input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=self.input_chunk)
        if not self.quiet:
            print(f"{self.label}Microphone is on (24kHz → OpenAI). Start speaking...")
        try:
            while self.is_connected:
                chunk = await asyncio.get_event_loop().run_in_executor(
                    None, stream.read, self.input_chunk)
                await self.send_audio_chunk(chunk)
        finally:
            stream.stop_stream()
            stream.close()

    async def close(self):
        self.is_connected = False
        if self._dump is not None and self._dump:
            import wave as _wave
            with _wave.open(self.audio_dump_path, "wb") as wf:
                wf.setnchannels(self.output_channels)
                wf.setsampwidth(2)
                wf.setframerate(self.output_rate)
                wf.writeframes(bytes(self._dump))
            print(f"💾 Збережено: {self.audio_dump_path} "
                  f"({len(self._dump)/2/self.output_rate:.1f} с)")
        if self.ws:
            await self.ws.close()
            if not self.quiet:
                print("OpenAI WebSocket closed.")
        for th, q in ((self.audio_player_thread, self.audio_playback_queue),
                      (self.monitor_thread, self.monitor_queue)):
            if th:
                q.put(None)
                th.join(timeout=2)
        if self._own_pa:
            self.pyaudio_instance.terminate()
