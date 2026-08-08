"""Gemini 3.5 Live Translate client — streaming speech-to-speech translation.

One engine for BOTH directions (unlike the Qwen+OpenAI hybrid): Ukrainian works
as input AND as output voice. Mirrors the method surface of LiveTranslateClient /
OpenAITranslateClient so web.py / bidirectional.py can drive it the same way.

Protocol (verified live — see probe_gemini.py):
  URL  : wss://generativelanguage.googleapis.com/ws/
         google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent?key=KEY
  Setup: {"setup": {"model": "models/gemini-3.5-live-translate-preview",
           "generationConfig": {"responseModalities": ["AUDIO"],
             "translationConfig": {"targetLanguageCode": <lang>, "echoTargetLanguage": true}},
           "inputAudioTranscription": {}, "outputAudioTranscription": {}}}
  Audio IN : 16kHz PCM16 mono — {"realtimeInput": {"audio": {"data": b64,
             "mimeType": "audio/pcm;rate=16000"}}}, 100ms chunks.
  Audio OUT: 24kHz PCM16 — serverContent.modelTurn.parts[].inlineData.data (b64).
  Text     : serverContent.inputTranscription.text (source),
             serverContent.outputTranscription.text (target).

NOTES:
- No turnComplete in translate mode; the model emits filler silence chunks. We skip
  all-zero chunks so the playback buffer doesn't drift.
- voiceName is IGNORED on the preview model (fixed female voice) — see memory.
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

MODEL = "models/gemini-3.5-live-translate-preview"
WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


class GeminiTranslateClient:
    def __init__(self, api_key: str, target_language: str = "en", *,
                 audio_enabled: bool = True,
                 input_device_index: int | None = None,
                 output_device_index: int | None = None,
                 monitor_device_index: int | None = None, monitor_delay: float = 1.0,
                 label: str = "", playback_delay: float = 1.0,
                 voice: str = "", log_latency: bool = False, quiet: bool = False,
                 pa=None):
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
        self.voice = voice  # наразі ігнорується моделлю (preview)
        self.label = label
        # UI hook: on_stream(direction, text, final).
        self.on_stream = None
        self.direction = ""
        self.log_latency = log_latency
        self.quiet = quiet

        self.ws = None
        self.is_connected = False

        # Gemini Live: вхід 16kHz PCM16, вихід 24kHz PCM16 (mono).
        self.input_rate = 16000
        self.input_chunk = 1600          # 100 ms
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

        self._stream_buf = ""

    async def connect(self):
        url = f"{WS_URL}?key={self.api_key}"
        try:
            self.ws = await websockets.connect(url, max_size=None)
            self.is_connected = True
            if not self.quiet:
                print(f"{self.label}Gemini translate connected.")
            await self.configure_session()
            # Чекаємо setupComplete, перш ніж стрімити мікрофон.
            first = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=10))
            if "error" in first:
                raise RuntimeError(f"Gemini setup error: {first['error']}")
        except Exception as e:
            print(f"{self.label}Gemini connection failed: {e}")
            self.is_connected = False
            raise

    async def configure_session(self):
        gen = {"responseModalities": ["AUDIO"], "translationConfig":
               {"targetLanguageCode": self.target_language, "echoTargetLanguage": True}}
        if self.voice:
            gen["speechConfig"] = {"voiceConfig":
                                   {"prebuiltVoiceConfig": {"voiceName": self.voice}}}
        setup = {"setup": {"model": MODEL, "generationConfig": gen,
                           "inputAudioTranscription": {}, "outputAudioTranscription": {}}}
        if not self.quiet:
            print(f"{self.label}Gemini setup: target={self.target_language}")
        await self.ws.send(json.dumps(setup))

    async def send_audio_chunk(self, audio_data: bytes):
        if not self.is_connected:
            return
        await self.ws.send(json.dumps({"realtimeInput": {"audio": {
            "data": base64.b64encode(audio_data).decode(),
            "mimeType": f"audio/pcm;rate={self.input_rate}"}}}))

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
        silence = b"\x00\x00" * int(self.output_rate * 0.02)    # 20ms тиші (анти-underrun)
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
                elif not ended and self.is_connected:
                    # Черга порожня → годуємо вихід тишею, щоб BlackHole/Zoom не
                    # зациклював останній буфер (underrun → «остання фраза повторюється»).
                    stream.write(silence)
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
            stamp = f"[{time.strftime('%H:%M:%S')}] " if self.log_latency else ""
            print(f"\n{self.label}{stamp}🌐 ", end="", flush=True)
        print(delta, end="", flush=True)
        self._stream_buf += delta
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
                sc = event.get("serverContent") or {}
                if sc.get("outputTranscription"):
                    self._print_delta(sc["outputTranscription"].get("text") or "")
                if self.log_latency and sc.get("inputTranscription"):
                    d = sc["inputTranscription"].get("text") or ""
                    if d:
                        print(f"\n{self.label}🎙 {d}", end="", flush=True)
                if self.audio_enabled:
                    for part in ((sc.get("modelTurn") or {}).get("parts") or []):
                        b64 = (part.get("inlineData") or {}).get("data")
                        if not b64:
                            continue
                        data = base64.b64decode(b64)
                        # пропускаємо filler-тишу, щоб буфер не дрейфував
                        if data.strip(b"\x00"):
                            self._enqueue_audio(data)
                if "error" in event:
                    print(f"\n{self.label}[Gemini ERROR] "
                          f"{json.dumps(event['error'], ensure_ascii=False)}", flush=True)
        except websockets.exceptions.ConnectionClosed as e:
            print(f"[WARNING] Gemini connection closed: {e}")
            self.is_connected = False
        except Exception as e:
            print(f"[ERROR] Gemini message loop: {e}")
            traceback.print_exc()
            self.is_connected = False

    async def start_microphone_streaming(self):
        stream = self.pyaudio_instance.open(
            format=self.input_format, channels=self.input_channels,
            rate=self.input_rate, input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=self.input_chunk)
        if not self.quiet:
            print(f"{self.label}Microphone is on (16kHz → Gemini). Start speaking...")
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
        if self.ws:
            await self.ws.close()
            if not self.quiet:
                print(f"{self.label}Gemini WebSocket closed.")
        for th, q in ((self.audio_player_thread, self.audio_playback_queue),
                      (self.monitor_thread, self.monitor_queue)):
            if th:
                q.put(None)
                th.join(timeout=2)
        if self._own_pa:
            self.pyaudio_instance.terminate()
