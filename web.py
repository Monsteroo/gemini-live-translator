#!/usr/bin/env python3
"""Локальний веб-інтерфейс для гібридного перекладача.

Backend: цей процес тримає двигун (Qwen — вхідний укр. текст; OpenAI — вихідний
англ. голос) і WebSocket-сервер, що шле живий переклад у браузер та приймає
команди Старт/Стоп. Сторінка віддається звичайним http.server.

Запуск:  python3 web.py        → відкриє http://localhost:8000 у браузері
Потрібні DASHSCOPE_API_KEY та OPENAI_API_KEY у .env.
"""
import asyncio
import http.server
import json
import pathlib
import socketserver
import threading
import time
import webbrowser

import pyaudio
import websockets

from config import (
    load_env, get_api_key, get_openai_key, get_gemini_key, LANGUAGES, VOICE_CAPABLE,
    find_device, find_cable, CABLE_IN_NAMES, CABLE_OUT_NAMES,
)
from livetranslate_client import LiveTranslateClient
from openai_translate_client import OpenAITranslateClient
from gemini_translate_client import GeminiTranslateClient
from bidirectional import OPENAI_OUTPUT_LANGS

HTTP_PORT = 8000
WS_PORT = 8765
HTML = (pathlib.Path(__file__).parent / "webui.html").read_text(encoding="utf-8")


def serve_http():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode("utf-8"))

        def log_message(self, *a):
            pass
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("localhost", HTTP_PORT), H) as httpd:
        httpd.serve_forever()


def detect_devices(pa, mic_spec=None, monitor=False):
    info = {"in": find_cable(pa, CABLE_IN_NAMES, want_input=True),
            "out": find_cable(pa, CABLE_OUT_NAMES, want_input=False),
            "mic": None, "monitor": None,
            "in_name": "", "out_name": "", "mic_name": "", "monitor_name": ""}
    # мікрофон
    mic = None
    if mic_spec:
        mic = int(mic_spec) if str(mic_spec).isdigit() else find_device(pa, mic_spec, want_input=True)
    if mic is None:
        mic = find_device(pa, "MacBook", want_input=True)
    if mic is None:
        try:
            mic = pa.get_default_input_device_info()["index"]
        except Exception:
            mic = None
    info["mic"] = mic
    # монітор (опційно — чути свій переклад у навушниках)
    if monitor:
        try:
            m = pa.get_default_output_device_info()["index"]
        except Exception:
            m = None
        if m is not None and "blackhole" in pa.get_device_info_by_index(m)["name"].lower():
            m = find_device(pa, "MacBook", want_input=False)
        info["monitor"] = m
    for k, idx in (("in", info["in"]), ("out", info["out"]),
                   ("mic", info["mic"]), ("monitor", info["monitor"])):
        if idx is not None:
            info[k + "_name"] = pa.get_device_info_by_index(idx)["name"]
    return info


class App:
    def __init__(self, qwen_key, openai_key, gemini_key=""):
        self.qwen_key = qwen_key
        self.openai_key = openai_key
        self.gemini_key = gemini_key
        self.clients = set()            # browser websockets
        self.client_in = None
        self.client_out = None
        self.tasks = []
        self.cost_task = None
        self.t_start = None
        self.loop = None
        self.testing = False
        self.lock = asyncio.Lock()     # Старт/Стоп — строго по черзі
        self.watch_task = None
        # ОДИН спільний екземпляр PyAudio на весь процес (інакше double-free/segfault).
        self.pa = pyaudio.PyAudio()

    # ---- broadcast to browsers ----
    async def broadcast(self, obj):
        if not self.clients:
            return
        msg = json.dumps(obj, ensure_ascii=False)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def _on_stream(self, direction, text, final):
        # викликається з event-loop (handle_server_messages) → плануємо надсилання
        asyncio.create_task(self.broadcast(
            {"type": "line", "dir": direction, "text": text, "final": final}))

    # ---- session lifecycle ----
    async def start_session(self, hear, speak, mic, monitor, engine="gemini"):
        async with self.lock:
            try:
                await self._start_session(hear, speak, mic, monitor, engine)
            except Exception as e:
                await self._teardown()
                await self.broadcast({"type": "status", "state": "error",
                                      "text": f"Не вдалося запустити: {e}"})

    async def _start_session(self, hear, speak, mic, monitor, engine="gemini"):
        if self.client_out is not None:
            await self._teardown()
        hear = str(hear or "uk").strip().lower()
        speak = str(speak or "en").strip().lower()
        import re
        if not (re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", hear)
                and re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})*", speak)):
            await self.broadcast({"type": "status", "state": "error",
                                  "text": "Невірний код мови. Оберіть мову зі списку."})
            return
        engine = (engine or "gemini").lower()
        # Якщо обрано Gemini, але ключа нема — відкочуємось на гібрид.
        if engine == "gemini" and not self.gemini_key:
            engine = "hybrid"
        # Валідація вихідної мови: гібрид обмежений OpenAI; Gemini — 70+ мов.
        if engine == "hybrid" and speak not in OPENAI_OUTPUT_LANGS:
            await self.broadcast({"type": "status", "state": "error",
                "text": f"OpenAI не озвучує «{LANGUAGES.get(speak, speak)}». "
                        f"Доступні: {', '.join(sorted(OPENAI_OUTPUT_LANGS))}"})
            return
        await self.broadcast({"type": "status", "state": "connecting",
                              "text": f"Під'єднання… (движок: {engine})"})
        try:
            dev = detect_devices(self.pa, mic, monitor)
        except Exception as e:
            await self.broadcast({"type": "status", "state": "error",
                "text": f"Не вдалося прочитати аудіопристрої ({e}). Перевірте поле «Мік»."})
            return
        if dev["in"] is None or dev["out"] is None or dev["mic"] is None:
            await self.broadcast({"type": "status", "state": "error",
                "text": "Не знайдено пристрої (BlackHole / мікрофон). "
                        "Перевірте BlackHole і sudo killall coreaudiod."})
            return
        await self.broadcast({"type": "devices",
            "in": dev["in_name"], "mic": dev["mic_name"],
            "out": dev["out_name"], "monitor": dev["monitor_name"]})

        if engine == "gemini":
            # ОДНА модель на обидва напрямки. Вхідний — лише текст (ви читаєте),
            # вихідний — голос. Українська працює і як вихідний голос.
            self.client_in = GeminiTranslateClient(
                api_key=self.gemini_key, target_language=hear, audio_enabled=False,
                input_device_index=dev["in"], output_device_index=None,
                label="[← ВАМ] ", quiet=True, pa=self.pa)
            self.client_out = GeminiTranslateClient(
                api_key=self.gemini_key, target_language=speak, audio_enabled=True,
                input_device_index=dev["mic"], output_device_index=dev["out"],
                monitor_device_index=dev["monitor"], monitor_delay=0.5,
                label="[ВИ →] ", playback_delay=0.5, quiet=True, pa=self.pa)
        else:
            in_voice = hear in VOICE_CAPABLE
            # НАПРЯМОК A: співрозмовник → ви (Qwen, текст вашою мовою)
            self.client_in = LiveTranslateClient(
                api_key=self.qwen_key, target_language=hear, audio_enabled=in_voice,
                input_device_index=dev["in"], output_device_index=None,
                label="[← ВАМ] ", vad_silence_ms=300, quiet=True, pa=self.pa)
            # НАПРЯМОК B: ви → співрозмовник (OpenAI, голос)
            self.client_out = OpenAITranslateClient(
                api_key=self.openai_key, target_language=speak, audio_enabled=True,
                input_device_index=dev["mic"], output_device_index=dev["out"],
                monitor_device_index=dev["monitor"], monitor_delay=0.5,
                label="[ВИ →] ", playback_delay=0.5, quiet=True, pa=self.pa)
        self.client_in.direction = "in"
        self.client_in.on_stream = self._on_stream
        self.client_out.direction = "out"
        self.client_out.on_stream = self._on_stream

        try:
            await asyncio.gather(self.client_in.connect(), self.client_out.connect())
        except Exception as e:
            await self._teardown()
            await self.broadcast({"type": "status", "state": "error", "text": f"Помилка з'єднання: {e}"})
            return
        self.client_in.start_audio_player()
        self.client_out.start_audio_player()
        self.t_start = time.time()
        # Приблизна ставка $/хв для лічильника. Gemini 3.5 Live Translate ≈ $0.037/хв
        # на сесію (вхід $0.0053 + вихід $0.0315), а сесій дві (по одній на напрямок).
        # Гібрид: OpenAI $0.034/хв (лише напрямок «ви →»).
        self.rate = 2 * 0.037 if engine == "gemini" else 0.034
        self.tasks = [
            asyncio.create_task(self.client_in.handle_server_messages(None)),
            asyncio.create_task(self.client_in.start_microphone_streaming()),
            asyncio.create_task(self.client_out.handle_server_messages(None)),
            asyncio.create_task(self.client_out.start_microphone_streaming()),
        ]
        self.cost_task = asyncio.create_task(self._cost_ticker())
        self.watch_task = asyncio.create_task(self._watch())
        await self.broadcast({"type": "status", "state": "live",
            "text": f"Працює [{engine}]: {LANGUAGES.get(hear, hear)} ⟵ ви читаєте | "
                    f"ви говорите ⟶ {LANGUAGES.get(speak, speak)} (голос)"})

    async def _cost_ticker(self):
        try:
            while True:
                await asyncio.sleep(3)
                if self.t_start:
                    sec = int(time.time() - self.t_start)
                    await self.broadcast({"type": "cost", "sec": sec,
                                          "usd": round(sec / 60 * getattr(self, "rate", 0.034), 3)})
        except asyncio.CancelledError:
            pass

    async def _watch(self):
        """Якщо з'єднання з моделлю обірвалось — не вдавати, що «Працює»."""
        try:
            while True:
                await asyncio.sleep(1)
                cs = (self.client_in, self.client_out)
                if self.client_out and any(c and not c.is_connected for c in cs):
                    asyncio.create_task(self._lost())
                    return
        except asyncio.CancelledError:
            pass

    async def _lost(self):
        async with self.lock:
            if not self.client_out:
                return
            await self._teardown()
        await self.broadcast({"type": "status", "state": "error",
            "text": "З'єднання з моделлю втрачено. Натисніть Старт, щоб під'єднатись знову."})

    async def stop_session(self):
        async with self.lock:
            await self._teardown()
        await self.broadcast({"type": "status", "state": "idle", "text": "Зупинено."})

    async def _teardown(self):
        for t in (self.cost_task, self.watch_task):
            if t and t is not asyncio.current_task():
                t.cancel()
        self.cost_task = self.watch_task = None
        for c in (self.client_in, self.client_out):
            if c:
                c.is_connected = False
        tasks, self.tasks = self.tasks, []
        for t in tasks:
            t.cancel()
        if tasks:      # дочекатись, поки мікрофонні потоки закриються (інакше SIGSEGV)
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 5)
            except Exception:
                pass
        for c in (self.client_in, self.client_out):
            if c:
                try:
                    await c.close()
                except Exception:
                    pass
        self.client_in = self.client_out = None
        self.t_start = None

    # ---- ТЕСТ вхідного напрямку: подати англійську мову «співрозмовника» ----
    async def inject_english(self, text):
        if not self.client_in or not self.client_in.is_connected:
            await self.broadcast({"type": "status", "state": "error",
                                  "text": "Спершу натисніть Старт."})
            return
        text = (text or "Good afternoon! How are you today?").strip()

        def _play():
            import subprocess, wave
            wav = "/tmp/_inject_en.wav"
            subprocess.run(["say", "-v", "Daniel", "--data-format=LEI16@16000",
                            "-o", wav, text], check=True)
            with wave.open(wav, "rb") as w:
                pcm = w.readframes(w.getnframes())
            bh16 = find_cable(self.pa, CABLE_IN_NAMES, want_input=False)
            if bh16 is None:
                return
            st = self.pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                              output=True, output_device_index=bh16)
            st.write(pcm)
            st.stop_stream()
            st.close()

        await self.broadcast({"type": "status", "state": "live",
                              "text": f"🧪 Тест: подаю англійською — «{text}»"})
        try:
            await asyncio.get_event_loop().run_in_executor(None, _play)
        except Exception as e:
            await self.broadcast({"type": "status", "state": "error", "text": f"Тест: {e}"})

    # ---- ТЕСТ ЗАТРИМКИ (лише Gemini) ----
    # Фрази озвучує `say`, а в сесію вони йдуть замість мікрофона — у реальному темпі,
    # через ті самі клієнти, плеєр і пристрої, що й у дзвінку.
    LAT_PHRASES = [
        ("out", "Lesya", "Доброго дня. Дуже приємно з вами познайомитися. "
                         "Розкажіть, будь ласка, більше про ваш проєкт."),
        ("out", "Lesya", "Дякую за уточнення. Давайте обговоримо терміни та бюджет, "
                         "а потім домовимося про наступну зустріч."),
        ("in", "Daniel", "Good afternoon. Thank you for joining the call today. "
                         "Could you tell me a little more about your project?"),
    ]

    @staticmethod
    def _say_pcm(voice, text):
        import array, subprocess, tempfile, wave, os
        fd, wav = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            subprocess.run(["say", "-v", voice, "--data-format=LEI16@16000", "-o", wav, text],
                           check=True, capture_output=True)
            with wave.open(wav, "rb") as w:
                pcm = w.readframes(w.getnframes())
        finally:
            os.unlink(wav)
        s = array.array("h")
        s.frombytes(pcm[: len(pcm) // 2 * 2])
        loud = [i for i, x in enumerate(s) if abs(x) > 300]
        if not loud:
            raise RuntimeError(f"голос {voice} не дав звуку")
        return s[loud[0]: loud[-1] + 1].tobytes()      # без тиші на початку/в кінці

    async def latency_test(self):
        if self.testing:
            return
        c_in, c_out = self.client_in, self.client_out
        if not (c_out and c_out.is_connected and isinstance(c_out, GeminiTranslateClient)):
            await self.broadcast({"type": "status", "state": "error",
                "text": "Тест затримки: спершу Старт з движком Gemini."})
            return
        self.testing = True
        for c in (c_in, c_out):
            c._mute = True
        rows = []
        try:
            loop = asyncio.get_event_loop()
            await self.broadcast({"type": "latency_start"})
            for n, (d, voice, text) in enumerate(self.LAT_PHRASES, 1):
                cl = c_out if d == "out" else c_in
                await self.broadcast({"type": "status", "state": "live",
                    "text": f"⏱ Тест затримки {n}/{len(self.LAT_PHRASES)} — не говоріть і не зупиняйте"})
                pcm = await loop.run_in_executor(None, self._say_pcm, voice, text)
                await asyncio.sleep(1.5)           # дати доспівати хвосту попередньої фрази
                cl.start_probe(pcm)
                t_wait = time.monotonic()
                while cl._inject or "speech_end" not in (cl._marks or {}):
                    if not cl.is_connected or time.monotonic() - t_wait > 40:
                        raise RuntimeError("сесію зупинено або мікрофон не віддає звук")
                    await asyncio.sleep(0.1)
                m = cl._marks
                quiet_since = time.monotonic()
                seen = None
                while time.monotonic() - m["speech_end"] < 25:     # чекаємо, поки стихне
                    await asyncio.sleep(0.25)
                    stamp = tuple(m.get(k) for k in ("last_out_text", "last_audio_rx", "last_audio_play"))
                    if stamp != seen:
                        seen, quiet_since = stamp, time.monotonic()
                    elif time.monotonic() - quiet_since > 3.0:
                        break
                cl._marks = None
                t0 = m["speech_start"] - cl.input_chunk / cl.input_rate         # мітка = кінець 100-мс блоку
                te = m["speech_end"]
                rel = lambda k: round(m[k] - t0, 2) if k in m else None
                row = {"dir": d, "phrase": n, "speech": round(te - t0, 1),
                       "text": rel("first_out_text"), "audio_rx": rel("first_audio_rx"),
                       "audio_play": rel("first_audio_play"),
                       "tail": round(m["last_audio_play"] - te, 2) if "last_audio_play" in m
                               else (round(m["last_out_text"] - te, 2) if "last_out_text" in m else None)}
                rows.append(row)
                await self.broadcast({"type": "latency_row", **row})
            with open(pathlib.Path(__file__).parent / "latency_log.jsonl", "a") as f:
                f.write(json.dumps({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "engine": "gemini", "rows": rows}) + "\n")
        except Exception as e:
            await self.broadcast({"type": "status", "state": "error", "text": f"Тест затримки: {e}"})
        finally:
            for c in (c_in, c_out):
                if c:
                    c._inject = bytearray()
                    c._marks = None
                    c._mute = False
            self.testing = False
            if self.client_out:
                await self.broadcast({"type": "status", "state": "live", "text": "Тест затримки завершено."})
            await self.broadcast({"type": "latency_end"})

    # ---- websocket handler ----
    async def handler(self, ws, *args):
        self.clients.add(ws)
        # початковий стан + список мов
        await ws.send(json.dumps({"type": "langs",
            "out": sorted(OPENAI_OUTPUT_LANGS),
            "names": {k: LANGUAGES.get(k, k) for k in OPENAI_OUTPUT_LANGS},
            "gemini": bool(self.gemini_key)},
            ensure_ascii=False))
        await ws.send(json.dumps({"type": "status",
            "state": "live" if self.client_out else "idle",
            "text": "Готово." if not self.client_out else "Працює."}, ensure_ascii=False))
        try:
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(m, dict):
                    continue
                cmd = m.get("cmd")
                try:
                    await self._dispatch(cmd, m)
                except Exception as e:
                    print(f"[handler] {cmd}: {e!r}", flush=True)
                    await self.broadcast({"type": "status", "state": "error",
                                          "text": f"Помилка команди «{cmd}»: {e}"})
        finally:
            self.clients.discard(ws)

    async def _dispatch(self, cmd, m):
        if cmd == "start":
            await self.start_session(m.get("hear"), m.get("speak"),
                                     m.get("mic"), bool(m.get("monitor")),
                                     m.get("engine", "gemini"))
        elif cmd == "stop":
            await self.stop_session()
        elif cmd == "inject":
            await self.inject_english(m.get("text"))
        elif cmd == "latency":
            asyncio.create_task(self.latency_test())


async def main():
    load_env()
    qwen = get_api_key()
    openai_key = get_openai_key()
    try:
        gemini_key = get_gemini_key()
    except SystemExit:
        gemini_key = ""   # Gemini-движок просто не буде доступний у перемикачі
    app = App(qwen, openai_key, gemini_key)
    app.loop = asyncio.get_event_loop()
    threading.Thread(target=serve_http, daemon=True).start()
    url = f"http://localhost:{HTTP_PORT}"
    print(f"🌐 Веб-інтерфейс: {url}  (Ctrl+C для виходу)")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    server = await websockets.serve(app.handler, "localhost", WS_PORT)
    try:
        await asyncio.Future()
    finally:
        # Чисте завершення: зупинити сесію (потоки/стріми), тоді один раз PyAudio.
        await app.stop_session()
        server.close()
        try:
            await server.wait_closed()
        except Exception:
            pass
        app.pa.terminate()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nЗавершення.")
