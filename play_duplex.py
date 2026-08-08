#!/usr/bin/env python3
"""Діагностика одночасних потоків: відкриває МІКРОФОН і паралельно ГРАЄ фразу
(без моделі та мережі). Відтворює саме умови режиму 'out'.

  python3 play_duplex.py            # мік=MacBook, вихід=за замовч.
  python3 play_duplex.py Redmi      # вихід на Redmi
  python3 play_duplex.py Redmi same # мік теж на тому ж пристрої (як було спочатку)
"""
import subprocess
import sys
import threading
import time
import wave

import numpy as np
import pyaudio

from config import find_device

OUT_NAME = sys.argv[1] if len(sys.argv) > 1 else None
MIC_SAME = len(sys.argv) > 2 and sys.argv[2] == "same"
SRC_RATE = 24000
WAV = "/tmp/play24k.wav"
PHRASE = "Good afternoon. I generally agree with the price, but I would like to discuss the delivery timeline before signing the contract next week."

_stop = threading.Event()


def make_wav():
    subprocess.run(["say", "-v", "Daniel", f"--data-format=LEI16@{SRC_RATE}", "-o", WAV, PHRASE], check=True)


def mic_loop(pa, dev):
    info = pa.get_device_info_by_index(dev) if dev is not None else pa.get_default_input_device_info()
    print(f"   🎤 мікрофон відкрито: [{info['index']}] {info['name']}")
    st = pa.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True,
                 input_device_index=dev, frames_per_buffer=1600)
    while not _stop.is_set():
        try:
            st.read(1600, exception_on_overflow=False)
        except Exception as e:
            print("   mic err:", e)
            break
    st.stop_stream(); st.close()


def main():
    make_wav()
    with wave.open(WAV, "rb") as w:
        pcm = w.readframes(w.getnframes())
    pa = pyaudio.PyAudio()
    out_dev = find_device(pa, OUT_NAME, want_input=False) if OUT_NAME else None
    if MIC_SAME and OUT_NAME:
        mic_dev = find_device(pa, OUT_NAME, want_input=True)
    else:
        mic_dev = find_device(pa, "MacBook", want_input=True)

    # 1) Спочатку граємо БЕЗ мікрофона (контроль).
    oinfo = pa.get_device_info_by_index(out_dev) if out_dev is not None else pa.get_default_output_device_info()
    print(f"\n🔈 1/2 — вихід [{oinfo['index']}] {oinfo['name']} БЕЗ мікрофона:")
    s1 = pa.open(format=pyaudio.paInt16, channels=1, rate=SRC_RATE, output=True,
                 output_device_index=out_dev, frames_per_buffer=2400)
    s1.write(pcm); time.sleep(0.2); s1.stop_stream(); s1.close()

    # 2) Тепер граємо З відкритим мікрофоном паралельно.
    time.sleep(0.6)
    print("🔈 2/2 — те саме, але З ВІДКРИТИМ МІКРОФОНОМ паралельно:")
    t = threading.Thread(target=mic_loop, args=(pa, mic_dev), daemon=True)
    t.start()
    time.sleep(0.5)  # дати мікрофону стартувати
    s2 = pa.open(format=pyaudio.paInt16, channels=1, rate=SRC_RATE, output=True,
                 output_device_index=out_dev, frames_per_buffer=2400)
    s2.write(pcm); time.sleep(0.2); s2.stop_stream(); s2.close()
    _stop.set(); t.join(timeout=1)
    pa.terminate()
    print("\nЧи 2-й (з мікрофоном) рваніший за 1-й (без)?")


if __name__ == "__main__":
    main()
