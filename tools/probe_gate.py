#!/usr/bin/env python3
"""Діагностика гейту: чи стабільно ASR віддає українську КИРИЛИЦЕЮ і чи завжди
синтезується аудіо, коли увімкнено input_audio_transcription. Без мікрофона."""
import asyncio
import base64
import json
import subprocess
import time
import wave

import websockets

import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import load_env, get_api_key, detect_script

URL = ("wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
       "?model=qwen3.5-livetranslate-flash-realtime")

UK_PHRASES = [
    "Доброго дня, дуже приємно з вами познайомитися.",
    "Якщо почнемо наступного тижня, прототип буде готовий за місяць.",
    "Це попередня оцінка, фінальна сума суттєво не зміниться.",
    "Перші три місяці підтримка безкоштовна.",
    "Я підготую договір і надішлю вам на пошту до кінця тижня.",
]


def make_pcm(voice, text):
    aiff, wav = f"/tmp/_g_{abs(hash(text))}.aiff", f"/tmp/_g_{abs(hash(text))}.wav"
    subprocess.run(["say", "-v", voice, "-o", aiff, text], check=True)
    subprocess.run(["afconvert", aiff, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", wav], check=True)
    with wave.open(wav, "rb") as wf:
        return wf.readframes(wf.getnframes())


async def run_case(key, text, transcription_on, voice="Lesya"):
    pcm = make_pcm(voice, text)
    session = {
        "modalities": ["text", "audio"],
        "input_audio_format": "pcm", "output_audio_format": "pcm",
        "translation": {"language": "en"},
        "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
    }
    if transcription_on:
        session["input_audio_transcription"] = {"model": "qwen3-asr-flash-realtime"}
    cfg = {"type": "session.update", "session": session}
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        await ws.send(json.dumps(cfg))
        await asyncio.sleep(0.6)

        async def send():
            ch = 1600 * 2
            for i in range(0, len(pcm), ch):
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(pcm[i:i+ch]).decode()}))
                await asyncio.sleep(0.08)
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        from config import has_script
        t = asyncio.create_task(send())
        src, audio = "", 0
        gate = "pending"  # симуляція НОВОЇ логіки: allow коли зʼявилась кирилиця

        def decide(final):
            nonlocal gate
            if gate != "pending":
                return
            if has_script(src, "cyrillic"):
                gate = "allow"
            elif final:
                gate = "block"
        try:
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
                ty = ev.get("type")
                if ty == "conversation.item.input_audio_transcription.text":
                    src = ev.get("stash") or ev.get("text") or src
                    decide(False)
                elif ty == "conversation.item.input_audio_transcription.completed":
                    src = ev.get("transcript") or src
                    decide(True)
                elif ty == "response.audio.delta":
                    audio += len(base64.b64decode(ev.get("delta", "")))
                elif ty == "response.done":
                    if gate == "pending":
                        gate = "allow"  # fallback
                    break
        except asyncio.TimeoutError:
            pass
        t.cancel()
        sec = audio / 2 / 24000
        verdict = ""
        if transcription_on:
            verdict = ("✅ allow" if gate == "allow" else "❌ BLOCK")
        print(f"  аудіо={sec:4.1f}с | ASR-фінал={src!r:45} | {verdict}")
        return sec, gate


async def main():
    load_env()
    key = get_api_key()
    print("=== БЕЗ транскрипції (чи завжди є аудіо?) ===")
    for p in UK_PHRASES:
        await run_case(key, p, transcription_on=False)
    print("\n=== З транскрипцією: УКРАЇНСЬКА (нова логіка гейту) ===")
    blocked = 0
    for p in UK_PHRASES:
        sec, g = await run_case(key, p, transcription_on=True)
        if g != "allow":
            blocked += 1
    print(f"  → {blocked}/{len(UK_PHRASES)} укр. фраз помилково заблоковано (має бути 0).")

    print("\n=== З транскрипцією: АНГЛІЙСЬКА (має блокуватись) ===")
    en_phrases = ["Good afternoon, it is a pleasure to meet you.",
                  "When do you think the first version will be ready?",
                  "That works for us, what is the next step?"]
    played = 0
    for p in en_phrases:
        sec, g = await run_case(key, p, transcription_on=True, voice="Daniel")
        if g == "allow":
            played += 1
    print(f"  → {played}/{len(en_phrases)} англ. фраз помилково озвучено (має бути 0).")


if __name__ == "__main__":
    asyncio.run(main())
