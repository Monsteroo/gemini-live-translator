#!/usr/bin/env python3
"""Діагностика відтворення: грає ту саму фразу двома способами —
(1) через наш pyaudio-движок (як у перекладачі) і (2) через системний afplay.
Так чути, чи проблема саме в pyaudio.

Запуск:
  python3 play_test.py            # 24000 Гц (як у моделі), пристрій за замовч.
  python3 play_test.py 48000      # відкрити потік на 48000 (ресемпл нами)
  python3 play_test.py 24000 Redmi   # вказати пристрій за назвою
"""
import subprocess
import sys
import time
import wave

import numpy as np
import pyaudio

import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import find_device

OPEN_RATE = int(sys.argv[1]) if len(sys.argv) > 1 else 24000
DEV_NAME = sys.argv[2] if len(sys.argv) > 2 else None
SRC_RATE = 24000
WAV = "/tmp/play24k.wav"
PHRASE = "Good afternoon. I generally agree with the price, but I would like to discuss the delivery timeline before signing."


def make_wav():
    # Англійська say -> 24000 Гц моно (як вихід моделі).
    subprocess.run(["say", "-v", "Daniel", f"--data-format=LEI16@{SRC_RATE}", "-o", WAV, PHRASE], check=True)


def resample(pcm: bytes, src: int, dst: int) -> bytes:
    if src == dst or not pcm:
        return pcm
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = int(round(len(a) * dst / src))
    x_old = np.linspace(0, 1, num=len(a), endpoint=False)
    x_new = np.linspace(0, 1, num=n, endpoint=False)
    return np.interp(x_new, x_old, a).astype(np.int16).tobytes()


def play_pyaudio():
    with wave.open(WAV, "rb") as w:
        pcm = w.readframes(w.getnframes())
    pa = pyaudio.PyAudio()
    dev = find_device(pa, DEV_NAME, want_input=False) if DEV_NAME else None
    info = pa.get_device_info_by_index(dev) if dev is not None else pa.get_default_output_device_info()
    print(f"   pyaudio → [{info['index']}] {info['name']} | потік {OPEN_RATE} Гц (вихідні дані {SRC_RATE} Гц)")
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=OPEN_RATE, output=True,
                     output_device_index=dev, frames_per_buffer=2400)
    data = resample(pcm, SRC_RATE, OPEN_RATE)
    stream.write(data)
    time.sleep(0.2)
    stream.stop_stream(); stream.close(); pa.terminate()


def main():
    make_wav()
    print("\n🔈 1/2 — через afplay (еталон, системний плеєр):")
    subprocess.run(["afplay", WAV])
    time.sleep(0.5)
    print("🔈 2/2 — через наш pyaudio-движок:")
    play_pyaudio()
    print("\nЯкий варіант рваний? (1=afplay, 2=pyaudio, обидва, жоден)")


if __name__ == "__main__":
    main()
