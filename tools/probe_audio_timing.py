#!/usr/bin/env python3
"""Вимір: чи встигають аудіо-дельти приходити в реальному часі.
Якщо span приходу > тривалості аудіо → буфер 'висихає' → рваність неминуча
при малому пре-буфері, і треба буферити більше / цілу фразу."""
import asyncio
import base64
import json
import subprocess
import time
import wave

import websockets

import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import load_env, get_api_key

URL = ("wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
       "?model=qwen3.5-livetranslate-flash-realtime")

PHRASES = [
    "Доброго дня, дуже приємно з вами познайомитися.",
    "Якщо почнемо наступного тижня, прототип буде готовий приблизно за місяць.",
    "Я підготую договір і надішлю вам на пошту до кінця тижня, після підписання почнемо.",
]


def make_pcm(text):
    a, w = f"/tmp/_at.aiff", f"/tmp/_at.wav"
    subprocess.run(["say", "-v", "Lesya", "-o", a, text], check=True)
    subprocess.run(["afconvert", a, "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", w], check=True)
    with wave.open(w, "rb") as wf:
        return wf.readframes(wf.getnframes())


async def run_case(key, text):
    pcm = make_pcm(text)
    session = {
        "modalities": ["text", "audio"],
        "input_audio_format": "pcm", "output_audio_format": "pcm",
        "translation": {"language": "en"},
        "voice": "default", "enable_voice_clone": True,
        "voice_clone_options": {"frequency": "once"},
        "turn_detection": {"type": "server_vad", "silence_duration_ms": 300},
    }
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        await ws.send(json.dumps({"type": "session.update", "session": session}))
        await asyncio.sleep(0.6)

        async def send():
            ch = 1600 * 2
            for i in range(0, len(pcm), ch):
                await ws.send(json.dumps({"type": "input_audio_buffer.append",
                                          "audio": base64.b64encode(pcm[i:i+ch]).decode()}))
                await asyncio.sleep(0.08)
            await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
        t = asyncio.create_task(send())

        times = []   # (wall_time, n_bytes) кожної дельти
        total = 0
        try:
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                ty = ev.get("type")
                if ty == "response.audio.delta":
                    b = len(base64.b64decode(ev.get("delta", "")))
                    times.append((time.time(), b)); total += b
                elif ty == "response.done":
                    break
        except asyncio.TimeoutError:
            pass
        t.cancel()

        if not times:
            print("  (немає аудіо)"); return
        audio_dur = total / 2 / 24000
        span = times[-1][0] - times[0][0]
        # найбільший розрив між сусідніми дельтами
        gaps = [times[i+1][0] - times[i][0] for i in range(len(times) - 1)]
        max_gap = max(gaps) if gaps else 0
        # кумулятивно доставлене аудіо (с) на кожен момент приходу дельти
        cum, c = [], 0.0
        for tw, b in times:
            c += b / 2 / 24000
            cum.append((tw, c))

        def sim(prebuffer_s):
            """Старт коли в буфері >= prebuffer; далі відтворення в реальному часі.
            Повертає (час_старту_від_першої_дельти, к-ть_висихань)."""
            play_start = next((tw for tw, cb in cum if cb >= prebuffer_s - 1e-9), cum[-1][0])
            dry = sum(1 for tw, cb in cum if tw >= play_start and (tw - play_start) > cb + 1e-6)
            return play_start - times[0][0], dry

        ratio = span / audio_dur if audio_dur else 0
        line = (f"  аудіо={audio_dur:4.1f}с надходило={span:4.1f}с rt×{ratio:4.2f} "
                f"розрив={max_gap*1000:4.0f}мс | ")
        for pb in (0.3, 0.6, 1.0, 1.5):
            start, dry = sim(pb)
            line += f"@{pb}: старт+{start:.1f}с {'ТРІСК' if dry else 'ok':5} | "
        print(line)


async def main():
    load_env()
    key = get_api_key()
    print("ratio>1.0 → дельти повільніші за реальний час (буфер висихає)\n")
    for p in PHRASES:
        await run_case(key, p)


if __name__ == "__main__":
    asyncio.run(main())
