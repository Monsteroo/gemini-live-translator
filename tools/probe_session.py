#!/usr/bin/env python3
"""Перевірка: чи приймає сервер наш session.update (зокрема turn_detection).
Лише конект — без аудіо. Друкує session.created / session.updated / error.

Запуск:
  python3 probe_session.py            # з turn_detection
  python3 probe_session.py --no-vad   # без turn_detection (для порівняння)
"""
import asyncio
import json
import sys
import time

import websockets

import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import load_env, get_api_key

URL = ("wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
       "?model=qwen3.5-livetranslate-flash-realtime")


async def main(send_vad: bool):
    load_env()
    key = get_api_key()
    session = {
        "modalities": ["text", "audio"],
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "translation": {"language": "en"},
    }
    if send_vad:
        session["turn_detection"] = {
            "type": "server_vad",
            "threshold": 0.5,
            "prefix_padding_ms": 300,
            "silence_duration_ms": 300,
        }
    cfg = {"event_id": f"event_{int(time.time()*1000)}",
           "type": "session.update", "session": session}

    print(f"→ turn_detection: {'ТАК' if send_vad else 'ні'}")
    async with websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        await ws.send(json.dumps(cfg))
        try:
            for _ in range(12):
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                ev = json.loads(msg)
                t = ev.get("type")
                print(f"  ← {t}")
                if t == "session.updated":
                    td = ev.get("session", {}).get("turn_detection")
                    print(f"     turn_detection у відповіді сервера: {json.dumps(td, ensure_ascii=False)}")
                    print("  ✅ session.update ПРИЙНЯТО")
                    return
                if t == "error":
                    print(f"  ❌ ПОМИЛКА: {json.dumps(ev.get('error', ev), ensure_ascii=False)}")
                    return
        except asyncio.TimeoutError:
            print("  ⏱ більше повідомлень немає (таймаут)")


if __name__ == "__main__":
    asyncio.run(main(send_vad="--no-vad" not in sys.argv))
