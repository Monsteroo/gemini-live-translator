#!/usr/bin/env python3
"""Перевірка OpenAI gpt-realtime-translate: під'єднання + реальний протокол.
Без мікрофона — озвучує українську через `say`, конвертує у 24kHz PCM16, шле в API,
і ДРУКУЄ ВСІ типи подій (щоб підтвердити назви: session.output_audio.delta тощо).

Запуск:  python3 probe_openai.py
Потрібен OPENAI_API_KEY у .env.
"""
import asyncio
import base64
import json
import subprocess
import time
import wave

import websockets

from config import load_env, get_openai_key
from openai_translate_client import API_URL, APPEND_TYPE


def make_pcm24(text: str) -> bytes:
    a, w = "/tmp/_oa.aiff", "/tmp/_oa.wav"
    subprocess.run(["say", "-v", "Lesya", "-o", a, text], check=True)
    subprocess.run(["afconvert", a, "-f", "WAVE", "-d", "LEI16@24000", "-c", "1", w], check=True)
    with wave.open(w, "rb") as wf:
        return wf.readframes(wf.getnframes())


async def main():
    load_env()
    key = get_openai_key()
    pcm = make_pcm24("Доброго дня, дуже приємно з вами познайомитися.")
    print(f"URL: {API_URL}\nappend type: {APPEND_TYPE}\n")

    async with websockets.connect(API_URL, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        await ws.send(json.dumps({
            "type": "session.update",
            "session": {"audio": {
                "input": {"transcription": {"model": "gpt-realtime-whisper"},
                          "noise_reduction": {"type": "near_field"}},
                "output": {"language": "en"}}}}))
        await asyncio.sleep(0.5)

        async def send():
            ch = 2400 * 2  # 100ms @24k int16
            for i in range(0, len(pcm), ch):
                await ws.send(json.dumps({"type": APPEND_TYPE,
                                          "audio": base64.b64encode(pcm[i:i+ch]).decode()}))
                await asyncio.sleep(0.08)
        asyncio.create_task(send())

        seen, audio_bytes, src, tgt = {}, 0, "", ""
        try:
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                t = ev.get("type", "?")
                seen[t] = seen.get(t, 0) + 1
                if t.endswith("output_audio.delta"):
                    audio_bytes += len(base64.b64decode(ev.get("delta") or ev.get("audio") or ""))
                elif t.endswith("output_transcript.delta"):
                    tgt += ev.get("delta") or ev.get("text") or ""
                elif t.endswith("input_transcript.delta"):
                    src += ev.get("delta") or ev.get("text") or ""
                elif t == "error":
                    print(f"❌ ERROR: {json.dumps(ev.get('error', ev), ensure_ascii=False)}")
        except asyncio.TimeoutError:
            pass

    print("\n=== Отримані типи подій ===")
    for t, n in seen.items():
        print(f"  {t}  ×{n}")
    print(f"\nДжерело (укр): {src!r}")
    print(f"Переклад (en): {tgt!r}")
    print(f"Аудіо отримано: {audio_bytes/2/24000:.1f} с")
    if audio_bytes and tgt:
        print("\n✅ Працює: є переклад і аудіо. Протокол підтверджено.")
    elif "error" in seen:
        print("\n⚠️ Сервер повернув error — можливо, інша назва append-типу або поля. "
              "Скинь мені вивід вище, виправлю.")
    else:
        print("\n⚠️ Аудіо/переклад не прийшли — скинь мені список типів подій вище.")


if __name__ == "__main__":
    asyncio.run(main())
