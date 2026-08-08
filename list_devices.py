#!/usr/bin/env python3
"""Показати всі аудіопристрої з індексами. Запустіть, щоб перевірити,
що віртуальні кабелі видні системі."""
import pyaudio
from config import list_devices, find_cable, CABLE_IN_NAMES, CABLE_OUT_NAMES

pa = pyaudio.PyAudio()
list_devices(pa)
print("Автовизначення для двостороннього режиму:")
print(f"  Translate Speaker / BlackHole 16ch (вхід від співрозмовника): {find_cable(pa, CABLE_IN_NAMES, want_input=True)}")
print(f"  Translate Microphone / BlackHole 2ch (вихід до співрозмовника): {find_cable(pa, CABLE_OUT_NAMES, want_input=False)}")
pa.terminate()
