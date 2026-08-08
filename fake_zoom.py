#!/usr/bin/env python3
"""Імітація співрозмовника в Zoom через РЕАЛЬНІ кабелі BlackHole — для тесту
без Zoom і без другої людини. Запускати ПАРАЛЕЛЬНО з bidirectional.py.

Грає роль Zoom:
  • Zoom Speaker = BlackHole 16ch → сюди ПИШЕМО англійську мову «співрозмовника»
    (bidirectional.py прочитає її і покаже український переклад).
  • Zoom Microphone = BlackHole 2ch → звідси ЧИТАЄМО ваш переклад і граємо вам
    у навушники (те, що почув би співрозмовник вашим клонованим голосом).

Порядок:
  Термінал 1:  python3 bidirectional.py --hear uk --speak en --clone --mic MacBook
  Термінал 2:  python3 fake_zoom.py

У цьому вікні тисніть Enter — «співрозмовник» скаже чергову фразу (дивіться
український переклад у терміналі 1). Або введіть свій англійський текст.
Говоріть українською у мікрофон MacBook — почуєте переклад у навушниках.
"""
import queue
import subprocess
import sys
import threading
import wave

import pyaudio

from config import find_device, find_cable, CABLE_IN_NAMES, CABLE_OUT_NAMES

RECORD_PATH = "/tmp/sent_to_foreigner.wav"  # що реально йде в кабель (для перевірки)

INJECT_RATE = 16000   # частота мови «співрозмовника», що пишемо в кабель
BRIDGE_RATE = 24000   # частота читання вашого перекладу (вихід моделі)
VOICE = "Daniel"      # англійський системний голос «співрозмовника»

PHRASES = [
    "Hi there! Can you hear me okay?",
    "I've reviewed your proposal and I think the pricing is fair.",
    "Could we move the delivery date to the end of next month?",
    "Great. Let's schedule a follow-up call for Friday afternoon.",
    "Thank you so much, it was a pleasure talking to you today.",
]

_stop = threading.Event()


def bridge_loop(spk_idx):
    """BlackHole 2ch (ваш переклад) → навушники: що чує співрозмовник.
    Розв'язуємо читання й запис окремими потоками + буфер на старті, бо два
    тактові генератори (BlackHole і Bluetooth) розходяться → інакше рве."""
    pa = pyaudio.PyAudio()
    bh2 = find_cable(pa, CABLE_OUT_NAMES, want_input=True)
    if bh2 is None:
        print("❌ Не знайдено Translate Microphone / BlackHole 2ch (вхід).")
        return
    src = pa.open(format=pyaudio.paInt16, channels=1, rate=BRIDGE_RATE, input=True,
                  input_device_index=bh2, frames_per_buffer=1024)
    dst = pa.open(format=pyaudio.paInt16, channels=1, rate=BRIDGE_RATE, output=True,
                  output_device_index=spk_idx, frames_per_buffer=2048)
    sname = pa.get_device_info_by_index(spk_idx)["name"] if spk_idx is not None else "(default)"
    print(f"🔊 «Вуха співрозмовника»: BlackHole 2ch → {sname}")

    q = queue.Queue()
    rec = bytearray()

    def reader():
        while not _stop.is_set():
            try:
                data = src.read(1024, exception_on_overflow=False)
            except Exception:
                break
            q.put(data)
            rec.extend(data)

    rt = threading.Thread(target=reader, daemon=True)
    rt.start()

    prebuffer = int(BRIDGE_RATE * 2 * 0.4)  # 400 мс запас
    buf = bytearray()
    started = False
    try:
        while not _stop.is_set():
            try:
                buf += q.get(timeout=0.05)
            except queue.Empty:
                pass
            while True:
                try:
                    buf += q.get_nowait()
                except queue.Empty:
                    break
            if not started and len(buf) < prebuffer:
                continue
            started = True
            if buf:
                dst.write(bytes(buf)); buf.clear()
    except Exception as e:
        print("bridge err:", e)
    finally:
        rt.join(timeout=1)
        src.stop_stream(); src.close(); dst.stop_stream(); dst.close(); pa.terminate()
        if rec:
            with wave.open(RECORD_PATH, "wb") as wf:
                wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(BRIDGE_RATE)
                wf.writeframes(bytes(rec))
            print(f"💾 Те, що реально пішло в кабель, збережено: {RECORD_PATH}")
            print(f"   Перевірте чистоту сигналу:  afplay {RECORD_PATH}")


def inject(text):
    """Синтезує англійську мову і пише її у BlackHole 16ch (наче співрозмовник)."""
    subprocess.run(["say", "-v", VOICE, f"--data-format=LEI16@{INJECT_RATE}", "-o", "/tmp/foreigner.wav", text], check=True)
    with wave.open("/tmp/foreigner.wav", "rb") as w:
        pcm = w.readframes(w.getnframes())
    pa = pyaudio.PyAudio()
    bh16 = find_cable(pa, CABLE_IN_NAMES, want_input=False)
    if bh16 is None:
        print("❌ Не знайдено Translate Speaker / BlackHole 16ch (вихід).")
        pa.terminate(); return
    st = pa.open(format=pyaudio.paInt16, channels=1, rate=INJECT_RATE, output=True,
                 output_device_index=bh16, frames_per_buffer=1600)
    st.write(pcm)
    st.stop_stream(); st.close(); pa.terminate()


def main():
    pa = pyaudio.PyAudio()
    spk = find_device(pa, "Redmi", want_input=False)
    if spk is None:
        try:
            spk = pa.get_default_output_device_info()["index"]
        except Exception:
            spk = None
    pa.terminate()

    t = threading.Thread(target=bridge_loop, args=(spk,), daemon=True)
    t.start()

    print("\n" + "=" * 60)
    print("  Імітація співрозмовника. Enter — наступна фраза, текст — своя,")
    print("  'q' + Enter — вихід.")
    print("=" * 60)
    i = 0
    try:
        while True:
            line = input(f"\n[Enter=фраза {i % len(PHRASES) + 1}] > ").strip()
            if line.lower() == "q":
                break
            text = line if line else PHRASES[i % len(PHRASES)]
            if not line:
                i += 1
            print(f"🇬🇧 Співрозмовник: {text}")
            inject(text)
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        _stop.set()
        t.join(timeout=1)
        print("\nЗавершення fake_zoom.")


if __name__ == "__main__":
    main()
