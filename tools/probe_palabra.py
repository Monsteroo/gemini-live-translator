#!/usr/bin/env python3
"""Тест Palabra (speech-to-speech, WebSocket): затримка і голос. Без мікрофона.

Подає озвучену `say` українську фразу в реальному темпі й міряє від початку фрази:
перший переклад-текст, перший звук, кінець. Зберігає звук у /tmp/_palabra_out.wav.

  python3 probe_palabra.py              # голос за замовчуванням (default_low/male)
  python3 probe_palabra.py clone        # клонування вашого голосу (експериментально)
Док: https://docs.palabra.ai/docs/quick-start/websockets
"""
import array, asyncio, base64, json, os, subprocess, sys, time, uuid, wave
import websockets
import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import load_env, get_palabra_key

RATE = 24000          # вхід 24k mono (рекомендовано докою; вихід завжди 24k)
CHUNK = 0.32          # 320 мс — рекомендований розмір
TEXT = ("Доброго дня. Сьогодні я хочу коротко розповісти про наш новий проєкт, "
        "і після цього відповім на ваші запитання.")


def pcm_from_say():
    a, w = "/tmp/_pl.aiff", "/tmp/_pl.wav"
    subprocess.run(["say", "-v", "Lesya", "-o", a, TEXT], check=True)
    subprocess.run(["afconvert", a, "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", w], check=True)
    with wave.open(w, "rb") as f:
        pcm = f.readframes(f.getnframes())
    s = array.array("h"); s.frombytes(pcm)
    loud = [i for i, x in enumerate(s) if abs(x) > 300]
    return s[loud[0]: loud[-1] + 1].tobytes()


async def main():
    load_env(); key = get_palabra_key()
    mode = sys.argv[1] if len(sys.argv) > 1 else "default"
    gen = {"voice_cloning": True} if mode == "clone" else {"voice_id": "default_low"}
    pcm = pcm_from_say()
    url = f"wss://streaming.palabra.ai/streaming-api/{uuid.uuid4().hex}/v1/speech-to-speech/stream?token={key}"
    ws = await websockets.connect(url, ping_interval=10, ping_timeout=30)
    print("url: wss://streaming.palabra.ai/…?token=***  mode:", mode)

    def msg(t, d): return json.dumps({"message_type": t, "data": d})
    await ws.send(msg("set_task", {
        "input_stream": {"content_type": "audio", "source": {"type": "ws", "format": "pcm_s16le", "sample_rate": RATE, "channels": 1}},
        "output_stream": {"content_type": "audio", "target": {"type": "ws", "format": "pcm_s16le"}},
        "pipeline": {"preprocessing": {}, "transcription": {"source_language": "uk"},
                     "translations": [{"target_language": "en", "speech_generation": gen}]}}))
    t_end = time.monotonic() + 30
    while True:                                   # чекаємо підтвердження set_task
        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, t_end - time.monotonic())))
        if m.get("message_type") == "error":
            print("❌ error:", json.dumps(m.get("data"), ensure_ascii=False)); return
        if m.get("message_type") == "task_status":
            print("✓ task:", m["data"].get("event")); break

    n = int(RATE * CHUNK) * 2
    marks, audio, tr = {}, bytearray(), ""
    async def send():
        t0 = time.monotonic(); i = 0
        stream = pcm + b"\x00\x00" * int(RATE * 2.0)        # 2 с тиші в кінці
        marks["start"] = t0
        for off in range(0, len(stream), n):
            await ws.send(msg("input_audio_data", {"data": base64.b64encode(stream[off:off + n]).decode()}))
            if off + n >= len(pcm) and "end" not in marks:
                marks["end"] = time.monotonic()
            i += 1
            await asyncio.sleep(max(0, t0 + i * CHUNK - time.monotonic()))
    task = asyncio.create_task(send())
    last = time.monotonic()
    while time.monotonic() - last < 6 and time.monotonic() - marks.get("start", time.monotonic()) < 40:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout=1))
        except asyncio.TimeoutError:
            continue
        now = time.monotonic(); t, d = m.get("message_type"), m.get("data") or {}
        if t == "output_audio_data":
            b = base64.b64decode(d.get("data", ""))
            audio += b; marks.setdefault("first_audio", now); marks["last_audio"] = now; last = now
        elif t in ("partial_translated_transcription", "translated_transcription"):
            txt = (d.get("transcription") or {}).get("text", "")
            if txt:
                marks.setdefault("first_text", now); last = now
                if t == "translated_transcription": tr += " " + txt
        elif t in ("warning", "error"):
            print(f"  [{t}]", json.dumps(d, ensure_ascii=False)[:200])
    task.cancel()
    try: await ws.send(msg("end_task", {"force": True}))
    except Exception: pass
    s0, se = marks["start"], marks.get("end", marks["start"])
    rel = lambda k: round(marks[k] - s0, 2) if k in marks else None
    print(f"фраза {se - s0:.1f}с | перший текст {rel('first_text')} | перший звук {rel('first_audio')} "
          f"| кінець звуку {rel('last_audio')} (хвіст {round(marks['last_audio'] - se, 2) if 'last_audio' in marks else None}) — від початку фрази, с")
    print("EN:", tr.strip())
    with wave.open("/tmp/_palabra_out.wav", "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(24000); f.writeframes(bytes(audio))
    print("звук: /tmp/_palabra_out.wav  →  afplay /tmp/_palabra_out.wav")

asyncio.run(main())
