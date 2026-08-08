#!/usr/bin/env python3
"""Автономний тест напрямку «англієць → ви»: синтезуємо англійську фразу
системним голосом macOS (`say`), згодовуємо її в модель і друкуємо
розпізнану англійську та переклад. Zoom / друга людина не потрібні.

Запуск:  python3 test_english.py [target_lang]   (за замовч. uk)
"""
import asyncio, base64, json, os, subprocess, sys, time, wave
import websockets
from config import load_env, get_api_key, LANGUAGES

PHRASE = "Hello! Can you hear me clearly? I think the new pricing looks reasonable, but I'd like to discuss the delivery timeline before we sign anything."
TARGET = sys.argv[1] if len(sys.argv) > 1 else "uk"
WAV = "/tmp/en_test.wav"
URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime?model=qwen3.5-livetranslate-flash-realtime"


def make_english_wav():
    # macOS `say` -> 16 kHz, 16-bit, mono PCM у WAV-контейнері.
    subprocess.run(
        ["say", "-v", "Daniel", "--data-format=LEI16@16000", "-o", WAV, PHRASE],
        check=True,
    )
    with wave.open(WAV, "rb") as w:
        assert w.getframerate() == 16000 and w.getsampwidth() == 2 and w.getnchannels() == 1
        return w.readframes(w.getnframes())


async def run():
    load_env()
    key = get_api_key()
    pcm = make_english_wav()
    print(f"🇬🇧 Англійська фраза ({len(pcm)} байт PCM):\n   \"{PHRASE}\"")
    print(f"🎯 Переклад на: {LANGUAGES.get(TARGET, TARGET)} ({TARGET})\n")

    ws = await websockets.connect(URL, additional_headers={"Authorization": f"Bearer {key}"})
    await ws.send(json.dumps({
        "event_id": f"event_{int(time.time()*1000)}",
        "type": "session.update",
        "session": {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "output_audio_format": "pcm",
            # Просимо також віддавати розпізнану англійську (джерело).
            "input_audio_transcription": {"model": "qwen3-asr-flash-realtime", "language": "en"},
            "translation": {"language": TARGET},
        },
    }))

    async def sender():
        # Даємо сесії налаштуватись, інакше перші чанки губляться (немає VAD).
        await asyncio.sleep(0.6)
        # Потокова передача чанками по 100 мс, як у реальному часі.
        chunk = 16000 * 2 // 10  # 100 ms, 16-bit mono
        for i in range(0, len(pcm), chunk):
            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm[i:i+chunk]).decode(),
            }))
            await asyncio.sleep(0.1)
        # Хвіст тиші + commit, щоб завершити репліку (для файлу скінченної довжини).
        silence = base64.b64encode(b"\x00" * chunk * 5).decode()
        await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": silence}))
        await asyncio.sleep(0.2)
        await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    asyncio.create_task(sender())

    got_translation = False
    try:
        while True:
            ev = json.loads(await asyncio.wait_for(ws.recv(), timeout=20))
            t = ev.get("type", "")
            if t == "conversation.item.input_audio_transcription.completed":
                print(f"🗣️  Розпізнано (EN): {ev.get('transcript','')}")
            elif t == "response.audio_transcript.done":
                print(f"🌐 Переклад:        {ev.get('transcript','')}")
                got_translation = True
            elif t == "response.text.done":
                print(f"🌐 Переклад:        {ev.get('text','')}")
                got_translation = True
            elif t == "response.done":
                usage = ev.get("response", {}).get("usage", {})
                if usage:
                    print(f"\n📊 Токени: {json.dumps(usage, ensure_ascii=False)}")
                if got_translation:
                    break
            elif t == "error":
                print(f"⚠️ Помилка сервера: {json.dumps(ev.get('error', ev), ensure_ascii=False)[:300]}")
                break
    except asyncio.TimeoutError:
        print("⚠️ Тайм-аут очікування відповіді.")
    finally:
        await ws.close()
        print("\n✅ Тест завершено." if got_translation else "\n❌ Переклад не отримано.")


if __name__ == "__main__":
    asyncio.run(run())
