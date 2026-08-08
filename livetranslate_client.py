import os
import time
import base64
import asyncio
import json
import websockets
import pyaudio
import queue
import threading
import traceback

class LiveTranslateClient:
    def __init__(self, api_key: str, target_language: str = "en", *, audio_enabled: bool = True,
                 input_device_index: int | None = None, output_device_index: int | None = None,
                 monitor_device_index: int | None = None, monitor_delay: float = 1.2,
                 label: str = "", voice: str = "default",
                 enable_voice_clone: bool = False, voice_clone_frequency: str = "once",
                 buffer_full_audio: bool = False, playback_delay: float = 0.3,
                 vad_silence_ms: int | None = 500, log_latency: bool = False,
                 suppress_echo: bool = False, quiet: bool = False,
                 pa=None, audio_dump_path: str | None = None):
        if not api_key:
            raise ValueError("API key cannot be empty.")

        self.api_key = api_key
        self.target_language = target_language
        self.audio_enabled = audio_enabled
        # Voice / voice-cloning. Cloning makes the translated speech sound like
        # the source speaker; it requires audio output (modalities text+audio).
        self.voice = voice
        self.enable_voice_clone = enable_voice_clone
        self.voice_clone_frequency = voice_clone_frequency  # never | once | always
        # Streaming display: text already printed for the current response.
        self._stream_buf = ""
        # If True, collect a whole utterance's audio and play it in one piece
        # (smooth, ~1s extra latency) instead of streaming bursty deltas.
        self.buffer_full_audio = buffer_full_audio
        self._audio_buf = bytearray()
        # Streaming playback: how much audio (seconds) to pre-buffer before the
        # first sound. A larger value delays the start but keeps playback smooth.
        self.playback_delay = playback_delay
        # Server-VAD turn detection: how long a pause (ms) finalizes a turn.
        # Lower = the server commits/translates sooner = less latency, but a
        # natural mid-sentence pause may split a turn. None = don't send (server
        # default). Set via session.update -> turn_detection.silence_duration_ms.
        self.vad_silence_ms = vad_silence_ms
        # Quiet mode: suppress all technical chatter (config dump, connection /
        # mic / shutdown logs) — leave only the translation lines for a clean read.
        self.quiet = quiet
        # Diagnostics: print timing from VAD speech-stop to first audio/text.
        self.log_latency = log_latency
        self._t_speech_stopped = None
        self._t_first_audio = None
        # Echo suppression (solo testing): drop the synthesized audio when the
        # SOURCE you spoke is already the target language — i.e. when you role-
        # play the foreigner in English (target=en) and it would just be dubbed
        # back ("повторення від англійця"). We decide by SIMILARITY between the
        # recognized source transcript and the translation: English→English ⇒
        # source ≈ translation ⇒ block; Ukrainian (even when the ASR romanizes
        # it to Latin) ⇒ source ≠ translation ⇒ play. This is robust where the
        # earlier script-based gate failed (real-mic Ukrainian gets romanized).
        # Requires the whole utterance buffered (buffer_full_audio) since the
        # decision needs the final translation. Per-turn state:
        self.suppress_echo = suppress_echo
        self._src_text = ""             # recognized source transcript (this turn)
        self._last_translation = ""     # finalized translation text (this turn)
        # Debug: save every received audio byte to inspect data vs playback.
        self.audio_dump_path = audio_dump_path
        self._dump = bytearray() if audio_dump_path else None
        # Audio routing: which physical/virtual devices to use.
        # None means PortAudio default device.
        self.input_device_index = input_device_index
        self.output_device_index = output_device_index
        # Optional second output ("monitor"): play the same audio to your own
        # earbuds in parallel with the main output (e.g. the BlackHole→Zoom
        # cable), so you can hear the outgoing translation for solo testing.
        # It runs on its OWN thread + queue + pre-roll so it never blocks (and is
        # never blocked by) the main output — they are independent audio clocks
        # (e.g. BlackHole vs Bluetooth) and must NOT be coupled, or both stutter.
        # Bluetooth earbuds need a generous cushion, hence a larger default delay.
        self.monitor_device_index = monitor_device_index
        self.monitor_delay = monitor_delay
        self.monitor_queue = queue.Queue()
        self.monitor_thread = None
        # Short tag prefixed to logs so two concurrent clients are distinguishable.
        self.label = label
        # Optional UI hook: on_stream(direction, text, final) — lets a web UI
        # render the translation live in addition to the terminal. direction is
        # a free tag ("in"/"out") set by the caller.
        self.on_stream = None
        self.direction = ""
        self.ws = None
        self.api_url = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3.5-livetranslate-flash-realtime"
        
        # Audio input configuration (from microphone)
        self.input_rate = 16000
        self.input_chunk = 1600
        self.input_format = pyaudio.paInt16
        self.input_channels = 1
        
        # Audio output configuration (for playback)
        self.output_rate = 24000
        self.output_chunk = 2400
        self.output_format = pyaudio.paInt16
        self.output_channels = 1
        
        # State management
        self.is_connected = False
        self.audio_player_thread = None
        self.audio_playback_queue = queue.Queue()
        # Shared vs own PyAudio: a long-running app (web.py with start/stop) must
        # pass ONE shared instance — creating/terminating several PyAudio objects
        # across threads double-frees CoreAudio resources and crashes (segfault).
        self._own_pa = pa is None
        self.pyaudio_instance = pa if pa is not None else pyaudio.PyAudio()

    async def connect(self):
        """Establish a WebSocket connection to the translation service."""
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            self.ws = await websockets.connect(self.api_url, additional_headers=headers)
            self.is_connected = True
            if not self.quiet:
                print(f"Successfully connected to the server: {self.api_url}")
            await self.configure_session()
        except Exception as e:
            print(f"Connection failed: {e}")
            self.is_connected = False
            raise

    async def configure_session(self):
        """Configure the translation session, setting the target language, voice, etc."""
        config = {
            "event_id": f"event_{int(time.time() * 1000)}",
            "type": "session.update",
            "session": {
                # 'modalities' controls the output type.
                # ["text", "audio"]: Returns both translated text and synthesized audio (recommended).
                # ["text"]: Returns only the translated text.
                "modalities": ["text", "audio"] if self.audio_enabled else ["text"],
                "input_audio_format": "pcm",
                "output_audio_format": "pcm",
                # 'input_audio_transcription' configures source language recognition.
                # Set 'model' to 'qwen3-asr-flash-realtime' to also output the source language recognition result.
                # "input_audio_transcription": {
                #     "model": "qwen3-asr-flash-realtime",
                #     "language": "zh"  # source language, default 'en'
                # },
                "translation": {
                    "language": self.target_language,
                    # 'corpus' configures hotwords to improve the translation accuracy of specific terms.
                    # "corpus": {
                    #     "phrases": {
                    #         "Artificial Intelligence": "Artificial Intelligence",
                    #         "Machine Learning": "Machine Learning"
                    #     }
                    # }
                }
            }
        }
        # Recognize the source speech — needed to compare it with the translation
        # for echo suppression (English→English detection).
        if self.suppress_echo:
            config["session"]["input_audio_transcription"] = {
                "model": "qwen3-asr-flash-realtime"
            }
        # Server-VAD turn detection. A shorter silence_duration_ms makes the
        # server finalize a turn (and start translating/synthesizing) sooner
        # after you pause — the main lever for cutting end-to-end latency.
        if self.vad_silence_ms is not None:
            config["session"]["turn_detection"] = {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": self.vad_silence_ms,
            }
        # Voice settings only apply when audio output is enabled.
        # NOTE: the server rejects voice="default" when cloning is OFF, so in the
        # normal case we omit `voice` entirely (server picks its own default).
        if self.audio_enabled:
            if self.enable_voice_clone:
                # Clones the source speaker's voice into the translated speech.
                # frequency: 'once' (clone at session start) | 'always' (re-clone
                # per response, adapts to speaker changes) | 'never' (needs a
                # pre-cloned custom voice id in `voice`). With cloning on, the
                # server accepts voice="default".
                config["session"]["voice"] = self.voice
                config["session"]["enable_voice_clone"] = True
                config["session"]["voice_clone_options"] = {"frequency": self.voice_clone_frequency}
            elif self.voice and self.voice != "default":
                # Explicit custom voice id (no cloning).
                config["session"]["voice"] = self.voice
        if not self.quiet:
            print(f"{self.label}Sending session configuration: {json.dumps(config, indent=2, ensure_ascii=False)}")
        await self.ws.send(json.dumps(config))

    async def send_audio_chunk(self, audio_data: bytes):
        """Encode and send an audio chunk to the server."""
        if not self.is_connected:
            return
            
        event = {
            "event_id": f"event_{int(time.time() * 1000)}",
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(audio_data).decode()
        }
        await self.ws.send(json.dumps(event))

    async def send_image_frame(self, image_bytes: bytes, *, event_id: str | None = None):
        # Send an image frame to the server.
        if not self.is_connected:
            return

        if not image_bytes:
            raise ValueError("image_bytes cannot be empty.")

        # Encode to Base64
        image_b64 = base64.b64encode(image_bytes).decode()

        event = {
            "event_id": event_id or f"event_{int(time.time() * 1000)}",
            "type": "input_image_buffer.append",
            "image": image_b64,
        }

        await self.ws.send(json.dumps(event))

    def _player_loop(self, *, output_device_index, q, playback_delay, tag=""):
        """One independent playback loop: pull PCM from `q`, jitter-buffer it, and
        write to `output_device_index`. Each output device gets its OWN loop so
        two devices on different clocks (e.g. BlackHole vs Bluetooth) never block
        each other (the cause of stutter when they shared one thread)."""
        try:
            stream = self.pyaudio_instance.open(
                format=self.output_format,
                channels=self.output_channels,
                rate=self.output_rate,
                output=True,
                output_device_index=output_device_index,
                frames_per_buffer=self.output_chunk,
            )
        except Exception as e:
            print(f"{self.label}⚠️ вихід {tag} недоступний: {e}", flush=True)
            return
        # Jitter buffer: accumulate `playback_delay` seconds before the first
        # sound, then stream continuously. The head start absorbs the bursty /
        # slower-than-realtime arrival of audio deltas so playback stays smooth.
        # In smooth (buffer_full) mode the whole utterance is enqueued as one
        # block, so no pre-roll is needed (and a pre-roll larger than a short
        # clip would otherwise stall it) — play immediately and contiguously.
        prebuffer = 0 if self.buffer_full_audio else int(self.output_rate * 2 * playback_delay)
        buf = bytearray()
        started = False
        ended = False
        try:
            while not ended and (self.is_connected or not q.empty() or buf):
                try:
                    audio_chunk = q.get(timeout=0.05)
                    if audio_chunk is None:  # Termination signal
                        ended = True
                    else:
                        buf += audio_chunk
                        q.task_done()
                except queue.Empty:
                    pass
                # Coalesce everything available right now into one contiguous write.
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
                # Hold playback until the pre-roll cushion is filled.
                if not started and not ended and len(buf) < prebuffer:
                    continue
                started = True
                if buf:
                    stream.write(bytes(buf))
                    buf.clear()
            if buf:  # flush tail
                stream.write(bytes(buf))
        finally:
            stream.stop_stream()
            stream.close()

    def _audio_player_task(self):
        self._player_loop(output_device_index=self.output_device_index,
                          q=self.audio_playback_queue,
                          playback_delay=self.playback_delay, tag="(основний)")

    def _monitor_player_task(self):
        self._player_loop(output_device_index=self.monitor_device_index,
                          q=self.monitor_queue,
                          playback_delay=self.monitor_delay, tag="(монітор)")

    def _enqueue_audio(self, data: bytes):
        """Queue audio for the main output and, if enabled, the monitor — each
        on its own independent queue/thread."""
        self.audio_playback_queue.put(data)
        if self.monitor_device_index is not None:
            self.monitor_queue.put(data)

    def _reset_turn(self):
        self._src_text = ""
        self._last_translation = ""

    def _is_echo(self) -> bool:
        """True if the source you spoke is basically the target language already
        (English→English) — judged by similarity of source transcript vs the
        translation. Robust to the ASR romanizing Ukrainian, because romanized
        Ukrainian still differs strongly from the English translation."""
        import re
        from difflib import SequenceMatcher
        norm = lambda s: re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()
        src, tr = norm(self._src_text), norm(self._last_translation)
        if not src or not tr:
            return False  # не впевнені → НЕ глушимо (краще зайвий дубль, ніж втрата)
        return SequenceMatcher(None, src, tr).ratio() >= 0.6

    def _on_audio(self, data: bytes):
        """Streaming playback (no echo gate here — gate needs the full turn)."""
        self._enqueue_audio(data)

    def _flush_full_audio(self):
        """Smooth/gate mode: the whole utterance has arrived — optionally drop it
        if it's an English→English echo, else enqueue it as ONE contiguous block.
        Playing a complete clip is immune to the server's bursty/late audio
        deltas (gaps up to ~0.75s) that cause choppiness when streaming with a
        small pre-buffer."""
        if not self._audio_buf:
            return
        data = bytes(self._audio_buf)
        self._audio_buf.clear()
        if self.suppress_echo and self._is_echo():
            if self.log_latency:
                print(f"\n{self.label}🔇 ваша власна мова цільової "
                      f"({self._src_text!r}≈переклад) — не дублюю", flush=True)
            return
        self._enqueue_audio(data)

    def start_audio_player(self):
        """Start the audio player thread(s) (only when audio output is enabled).
        If a monitor device is set, start a SECOND independent player for it."""
        if not self.audio_enabled:
            return
        if self.monitor_device_index is not None and (
                self.monitor_thread is None or not self.monitor_thread.is_alive()):
            self.monitor_thread = threading.Thread(target=self._monitor_player_task, daemon=True)
            self.monitor_thread.start()
        if self.audio_player_thread is None or not self.audio_player_thread.is_alive():
            self.audio_player_thread = threading.Thread(target=self._audio_player_task, daemon=True)
            self.audio_player_thread.start()

    def _stream_print(self, full: str):
        """Print a cumulative translation incrementally (only the new suffix)."""
        if not full:
            return
        if self._stream_buf == "":
            # Для аналізу: позначка часу появи першого слова перекладу та (якщо
            # відома) затримка від паузи мовця до цього моменту.
            stamp = ""
            if self.log_latency:
                stamp = f"[{time.strftime('%H:%M:%S')}"
                if self._t_speech_stopped:
                    stamp += f" +{time.time() - self._t_speech_stopped:.1f}с"
                stamp += "] "
            print(f"\n{self.label}{stamp}🌐 ", end="", flush=True)
        if full.startswith(self._stream_buf):
            print(full[len(self._stream_buf):], end="", flush=True)
        else:
            # Divergence (model revised earlier text) — reprint the whole line.
            print(f"\r{self.label}{'🌐'} {full}", end="", flush=True)
        self._stream_buf = full
        if self.on_stream:
            self.on_stream(self.direction, full, False)

    def _stream_finish(self, full: str | None = None) -> str:
        """Finalize the current streamed line (newline) and reset state."""
        if full and full != self._stream_buf:
            self._stream_print(full)
        last = self._stream_buf
        if last:
            self._last_translation = last  # для виявлення ехо (джерело≈переклад)
            if self.log_latency and self._t_speech_stopped:
                # повна затримка: пауза мовця → завершений переклад
                print(f"   ⏱ всього +{time.time() - self._t_speech_stopped:.1f}с", end="", flush=True)
            print(flush=True)  # newline to close the streamed line
            if self.on_stream:
                self.on_stream(self.direction, last, True)
        self._stream_buf = ""
        return last

    async def handle_server_messages(self, on_text_received):
        """Handle incoming messages from the server in a loop."""
        try:
            async for message in self.ws:
                event = json.loads(message)
                event_type = event.get("type")
                # --- VAD timing diagnostics ---
                if event_type == "input_audio_buffer.speech_started":
                    self._reset_turn()  # новий хід — скидаємо джерело/переклад
                    if self.log_latency:
                        print(f"\n{self.label}⏱ VAD: мова почалась", flush=True)
                elif event_type == "input_audio_buffer.speech_stopped":
                    self._t_speech_stopped = time.time()
                    self._t_first_audio = None
                    if self.log_latency:
                        print(f"\n{self.label}⏱ VAD: пауза — сервер обробляє…", flush=True)

                if event_type == "response.audio.delta" and self.audio_enabled:
                    audio_b64 = event.get("delta", "")
                    if audio_b64:
                        audio_data = base64.b64decode(audio_b64)
                        if self.log_latency and self._t_first_audio is None and self._t_speech_stopped:
                            self._t_first_audio = time.time()
                            print(f"\n{self.label}⏱ перше аудіо через "
                                  f"{self._t_first_audio - self._t_speech_stopped:.1f} с після паузи", flush=True)
                        if self._dump is not None:
                            self._dump += audio_data
                        if self.buffer_full_audio:
                            # Плавний режим: накопичуємо всю репліку, гейт — на фіналі.
                            self._audio_buf += audio_data
                        else:
                            self._on_audio(audio_data)
                elif event_type == "response.audio.done" and self.audio_enabled:
                    # Не віддаємо тут: чекаємо response.done, щоб переклад уже був
                    # фіналізований (потрібно для виявлення ехо джерело≈переклад).
                    pass

                # --- Розпізнаний ТЕКСТ ДЖЕРЕЛА (для гейту аудіо за скриптом) ---
                elif event_type == "conversation.item.input_audio_transcription.text":
                    # партіал у полі 'stash', фінал — у 'text'
                    self._src_text = event.get("stash") or event.get("text") or self._src_text
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    self._src_text = event.get("transcript") or self._src_text

                # --- Потоковий (частковий) переклад: друкуємо по мірі надходження ---
                elif event_type == "response.text.text":
                    # Кумулятивний текст (кожна подія містить весь текст дотепер).
                    self._stream_print(event.get("text", ""))
                elif event_type == "response.audio_transcript.text":
                    # Кумулятивний транскрипт (режим з аудіо) — як response.text.text.
                    self._stream_print(event.get("text", ""))
                elif event_type == "response.audio_transcript.delta":
                    # Інкрементальний фрагмент транскрипту (на випадок, якщо сервер
                    # надсилає .delta замість .text).
                    self._stream_print(self._stream_buf + event.get("delta", ""))

                # --- Завершення репліки ---
                elif event_type == "response.text.done":
                    last = self._stream_finish(event.get("text", ""))
                    if last and on_text_received:
                        on_text_received(last)
                elif event_type == "response.audio_transcript.done":
                    last = self._stream_finish(event.get("transcript", ""))
                    if last and on_text_received:
                        on_text_received(last)
                elif event_type == "response.done":
                    # Тихо: репліку вже завершено у *.done вище.
                    self._stream_finish()
                    if self.buffer_full_audio:  # плавний режим: гейт + віддати фразу
                        self._flush_full_audio()
                    self._reset_turn()

        except websockets.exceptions.ConnectionClosed as e:
            print(f"[WARNING] Connection closed: {e}")
            self.is_connected = False
        except Exception as e:
            print(f"[ERROR] An unexpected error occurred while processing messages: {e}")
            traceback.print_exc()
            self.is_connected = False

    async def start_microphone_streaming(self):
        """Capture audio from the microphone and stream it to the server."""
        stream = self.pyaudio_instance.open(
            format=self.input_format,
            channels=self.input_channels,
            rate=self.input_rate,
            input=True,
            input_device_index=self.input_device_index,
            frames_per_buffer=self.input_chunk
        )
        if not self.quiet:
            print(f"{self.label}Microphone is on. Start speaking...")
        try:
            while self.is_connected:
                audio_chunk = await asyncio.get_event_loop().run_in_executor(
                    None, stream.read, self.input_chunk
                )
                await self.send_audio_chunk(audio_chunk)
        finally:
            stream.stop_stream()
            stream.close()

    async def close(self):
        """Gracefully close the connection and release resources."""
        self.is_connected = False
        if self._dump is not None and self._dump:
            import wave as _wave
            with _wave.open(self.audio_dump_path, "wb") as wf:
                wf.setnchannels(self.output_channels)
                wf.setsampwidth(2)
                wf.setframerate(self.output_rate)
                wf.writeframes(bytes(self._dump))
            print(f"💾 Отримане аудіо збережено: {self.audio_dump_path} "
                  f"({len(self._dump)/2/self.output_rate:.1f} с)")
        if self.ws:
            await self.ws.close()
            if not self.quiet:
                print("WebSocket connection closed.")

        if self.audio_player_thread:
            self.audio_playback_queue.put(None) # Send termination signal
            self.audio_player_thread.join(timeout=2)
            if not self.quiet:
                print("Audio player thread stopped.")
        if self.monitor_thread:
            self.monitor_queue.put(None)  # Send termination signal
            self.monitor_thread.join(timeout=2)
            if not self.quiet:
                print("Monitor player thread stopped.")

        # Terminate ТІЛЬКИ власний екземпляр; спільний (web.py) — не чіпаємо.
        if self._own_pa:
            self.pyaudio_instance.terminate()
        if not self.quiet:
            print("PyAudio instance released.")