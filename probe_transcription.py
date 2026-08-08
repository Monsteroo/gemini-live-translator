#!/usr/bin/env python3
"""Перевірка: що повертає input_audio_transcription для укр. і англ. джерела.
Мета — зрозуміти, чи можемо ми визначити МОВУ/СКРИПТ джерела, щоб не озвучувати
англ→англ ('повторення від англійця'). Без мікрофона: озвучуємо `say`, женемо в API.

Запуск: python3 probe_transcription.py
"""
import asyncio
import base64
import json
import os
import subprocess
import time
import wave

import websockets

from config import load_env, get_api_key

URL = ("wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
       "?model=qwen3.5-livetranslate-flash-realtime")


def make_pcm(voice: str, text: str) -> bytes:
    aiff = f"/tmp/_probe_{voice}.aiff"
    wav = f"/tmp/_probe_{voice}.wav"
    subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True)
    subprocess.run(["afconvert", aiff, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", wav], check=True)
    with wave.open(wav, "rb") as wf:
        return wf.readframes(wf.getnframes())


async def run_case(key: str, label: str, pcm: bytes):
    print(f"\n===== {label} =====")
    session = {
        "modalities": ["text", "audio"],
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "translation": {"language": "en"},
        # Увімкнути розпізнавання джерела (без language → сподіваємось на автовизначення).
        "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
        "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
    }
    cfg = {"event_id": f"e{int(time.time()*1000)}", "type": "session.update", "session": session}
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        await ws.send(json.dumps(cfg))
        await asyncio.sleep(0.6)

        async def sender():
            chunk = 1600 * 2  # 100ms @16k int16
            for i in range(0, len(pcm), chunk):
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(pcm[i:i+chunk]).decode()}))
                await asyncio.sleep(0.08)
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

        asyncio.create_task(sender())
        src_text, tr_text = "", ""
        audio_bytes = 0
        try:
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=8)
                ev = json.loads(msg)
                t = ev.get("type")
                if "transcription" in t:
                    # друкуємо повністю — шукаємо поле мови
                    print(f"  ← {t}: {json.dumps({k: v for k, v in ev.items() if k not in ('type','event_id')}, ensure_ascii=False)}")
                    src_text = ev.get("text") or ev.get("transcript") or src_text
                elif t in ("response.audio_transcript.text", "response.text.text"):
                    tr_text = ev.get("text", tr_text)
                elif t in ("response.audio_transcript.done", "response.text.done"):
                    tr_text = ev.get("transcript") or ev.get("text") or tr_text
                elif t == "response.audio.delta":
                    audio_bytes += len(base64.b64decode(ev.get("delta", "")))
                elif t == "error":
                    print(f"  ❌ {json.dumps(ev.get('error', ev), ensure_ascii=False)}")
                elif t == "response.done":
                    print(f"  → ДЖЕРЕЛО (ASR): {src_text!r}")
                    print(f"  → ПЕРЕКЛАД    : {tr_text!r}")
                    print(f"  → аудіо синтезовано: {audio_bytes/2/24000:.1f} с")
                    return
        except asyncio.TimeoutError:
            print("  ⏱ таймаут")
            print(f"  → ДЖЕРЕЛО (ASR): {src_text!r}")
            print(f"  → ПЕРЕКЛАД    : {tr_text!r}")
            print(f"  → аудіо синтезовано: {audio_bytes/2/24000:.1f} с")


async def main():
    load_env()
    key = get_api_key()
    uk = make_pcm("Lesya", "Доброго дня, дуже приємно з вами познайомитися.")
    en = make_pcm("Daniel", "Good afternoon, it is a real pleasure to meet you.")
    await run_case(key, "УКРАЇНСЬКА джерело (Lesya)", uk)
    await run_case(key, "АНГЛІЙСЬКА джерело (Daniel)", en)


if __name__ == "__main__":
    asyncio.run(main())
