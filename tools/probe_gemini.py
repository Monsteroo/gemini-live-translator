#!/usr/bin/env python3
"""Перевірка Gemini 3.5 Live Translate: під'єднання + реальний протокол.

Без мікрофона — озвучує українську через `say`, конвертує у 16kHz PCM16 (вхід
Live API), шле в модель і ДРУКУЄ ВСІ типи повідомлень. Збирає переклад (текст)
і вихідне аудіо (24kHz), зберігає його у /tmp/_gemini_out.wav, щоб ти міг
ПОСЛУХАТИ й переконатися, що цільова мова виходить чистим голосом.

Мета тесту — підтвердити для напрямку UK→EN (і опційно UK→Uk-echo):
  • українська коректно розпізнається (inputTranscription),
  • переклад приходить текстом (outputTranscription),
  • і головне — приходить ГОЛОС цільовою мовою.

Запуск:  python3 probe_gemini.py            # ціль en
         python3 probe_gemini.py uk         # ціль uk (перевірити укр. голос!)
Потрібен GEMINI_API_KEY у .env (ключ з aistudio.google.com).

Док: https://ai.google.dev/gemini-api/docs/live-api/live-translate
"""
import asyncio
import base64
import json
import subprocess
import sys
import time
import wave

import websockets

import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import load_env, get_gemini_key

MODEL = "models/gemini-3.5-live-translate-preview"
WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
          "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")

IN_RATE = 16000     # вхід Live API: 16kHz PCM16 mono
OUT_RATE = 24000    # вихід: 24kHz PCM16 mono


def make_pcm16(text: str) -> bytes:
    """Українська → 16kHz mono PCM16 (формат входу Live API)."""
    a, w = "/tmp/_gm.aiff", "/tmp/_gm.wav"
    subprocess.run(["say", "-v", "Lesya", "-o", a, text], check=True)
    subprocess.run(["afconvert", a, "-f", "WAVE", "-d", f"LEI16@{IN_RATE}", "-c", "1", w], check=True)
    with wave.open(w, "rb") as wf:
        return wf.readframes(wf.getnframes())


def collect_audio(msg: dict) -> bytes:
    """Витягнути всі аудіо-частини з modelTurn.parts[].inlineData.data."""
    out = b""
    parts = (((msg.get("serverContent") or {}).get("modelTurn") or {}).get("parts") or [])
    for p in parts:
        data = (p.get("inlineData") or {}).get("data")
        if data:
            out += base64.b64decode(data)
    return out


async def main():
    load_env()
    key = get_gemini_key()
    target = (sys.argv[1] if len(sys.argv) > 1 else "en").lower()
    voice = sys.argv[2] if len(sys.argv) > 2 else ""   # напр. Charon, Puck (чол.)
    pcm = make_pcm16("Доброго дня, дуже приємно з вами познайомитися. Як ваші справи?")

    url = f"{WS_URL}?key={key}"
    print(f"URL: {WS_URL}?key=***\nmodel: {MODEL}\ntarget: {target}"
          f"{'  voice='+voice if voice else ''}\n")

    # Структура setup, підтверджена живим запуском (варіант A):
    # transcription — top-level; translationConfig — у generationConfig.
    gen = {"responseModalities": ["AUDIO"], "translationConfig":
           {"targetLanguageCode": target, "echoTargetLanguage": True}}
    if voice:
        gen["speechConfig"] = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}
    setup = {"model": MODEL, "generationConfig": gen,
             "inputAudioTranscription": {}, "outputAudioTranscription": {}}

    try:
        ws = await websockets.connect(url, max_size=None)
    except Exception as e:
        print(f"❌ Не вдалось під'єднатись: {e}")
        return
    await ws.send(json.dumps({"setup": setup}))
    try:
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"❌ setup відхилено: {str(e)[:200]}")
        if voice:
            print("   → можливо, модель не приймає speechConfig/цей voiceName. "
                  "Спробуй інший голос або запусти без аргументу голосу.")
        return
    if "error" in first:
        print(f"❌ ERROR на setup: {json.dumps(first['error'], ensure_ascii=False)}")
        return
    print(f"✓ setup пройшов; відповідь: {list(first.keys())}")
    if setup is None:
        print("\n⚠️ Жоден варіант setup не пройшов. Скинь мені вивід — підправлю структуру.")
        return

    async with ws:
        # ~1.5с тиші в кінці — щоб автоматичний VAD зафіксував кінець репліки
        # (інакше модель «чекає продовження» й не починає переклад).
        tail = b"\x00\x00" * int(IN_RATE * 1.5)
        stream = pcm + tail

        async def send():
            ch = int(IN_RATE * 0.1) * 2  # 100ms @16k int16
            for i in range(0, len(stream), ch):
                await ws.send(json.dumps({"realtimeInput": {"audio": {
                    "data": base64.b64encode(stream[i:i + ch]).decode(),
                    "mimeType": f"audio/pcm;rate={IN_RATE}"}}}))
                await asyncio.sleep(0.05)
            # сигнал кінця потоку (модель завершить хід)
            try:
                await ws.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))
            except Exception:
                pass
            print(f"✅ аудіо надіслано ({len(pcm)/2/IN_RATE:.1f}с мови + тиша). Чекаю переклад…")
        asyncio.create_task(send())

        # У режимі перекладу turnComplete не приходить, а модель безкінечно шле
        # шматочки тиші. Тому зупиняємось через QUIET с після ОСТАННЬОГО тексту.
        QUIET, MAXRUN = 2.5, 45.0
        seen, audio, src, tgt = {}, b"", "", ""
        audio_chunks = 0
        last_text = t0 = time.monotonic()
        try:
            while True:
                try:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=QUIET))
                except asyncio.TimeoutError:
                    print("  ✔ переклад завершено (повна тиша)", flush=True)
                    break
                for k in msg:
                    seen[k] = seen.get(k, 0) + 1
                sc = msg.get("serverContent") or {}
                if sc.get("inputTranscription"):
                    t = sc["inputTranscription"].get("text") or ""
                    src += t
                    if t:
                        last_text = time.monotonic()
                        print(f"  🎙 {t!r}", flush=True)
                if sc.get("outputTranscription"):
                    t = sc["outputTranscription"].get("text") or ""
                    tgt += t
                    if t:
                        last_text = time.monotonic()
                        print(f"  🌐 {t!r}", flush=True)
                a = collect_audio(msg)
                if a:
                    audio += a
                    audio_chunks += 1
                    if audio_chunks == 1:
                        print("  🔊 аудіо пішло…", flush=True)
                if sc.get("turnComplete"):
                    print("  ✔ turnComplete", flush=True)
                    break
                if "error" in msg:
                    print(f"❌ ERROR: {json.dumps(msg['error'], ensure_ascii=False)}")
                now = time.monotonic()
                if tgt and now - last_text > QUIET:
                    print("  ✔ переклад завершено (тиша після тексту)", flush=True)
                    break
                if now - t0 > MAXRUN:
                    print("  ⏹ ліміт часу", flush=True)
                    break
        except websockets.exceptions.ConnectionClosedError as e:
            print(f"  ⚠️ зʼєднання закрито: {str(e)[:160]}")

    audio = audio.rstrip(b"\x00")  # прибрати хвіст тиші

    print("\n=== Отримані типи повідомлень (top-level) ===")
    for k, n in seen.items():
        print(f"  {k}  ×{n}")
    print(f"\nДжерело (укр): {src!r}")
    print(f"Переклад ({target}): {tgt!r}")
    print(f"Аудіо отримано: {len(audio)/2/OUT_RATE:.1f} с")

    if audio:
        out = "/tmp/_gemini_out.wav"
        with wave.open(out, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(OUT_RATE)
            wf.writeframes(audio)
        print(f"💾 Збережено голос: {out}  →  послухай:  afplay {out}")

    if audio and tgt:
        print("\n✅ Працює: є переклад і голос. Протокол підтверджено.")
    elif "error" in seen:
        print("\n⚠️ Сервер повернув error — скинь мені вивід вище, виправлю.")
    else:
        print("\n⚠️ Аудіо/переклад не прийшли — скинь мені список типів повідомлень вище.")


if __name__ == "__main__":
    asyncio.run(main())
