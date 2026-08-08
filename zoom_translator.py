#!/usr/bin/env python3
# zoom_translator_macos.py

import os, sys, base64, signal, threading
import sounddevice as sd
import numpy as np
from dashscope.audio.qwen_omni import OmniRealtimeConversation, OmniRealtimeCallback, MultiModality
from dashscope.audio.qwen_omni.omni_realtime import TranslationParams

API_KEY = os.getenv("DASHSCOPE_API_KEY")
if not API_KEY:
    sys.exit("❌ Встановіть DASHSCOPE_API_KEY")

WS_URL = "wss://dashscope-intl.aliyuncs.com/api-ws/v1/realtime"
INPUT_LANG = "en"   # мова іноземця
OUTPUT_LANG = "uk"  # ваша мова

SAMPLE_RATE_IN = 16000
SAMPLE_RATE_OUT = 24000
CHUNK_DURATION = 0.1  # 100ms

class ZoomCallback(OmniRealtimeCallback):
    def __init__(self):
        self.lock = threading.Lock()
        self.conversation = None

    def on_open(self): print("✅ Підключено до DashScope")
    def on_close(self, code, msg): print(f"🔌 Закрито: {code} {msg}")
    def on_event(self, resp):
        t = resp.get("type", "")
        if t == "input_audio_buffer.speech_started": print("🎤 Говорять...")
        elif t == "input_audio_buffer.speech_stopped": print("⏸ Пауза")
        elif t == "conversation.item.input_audio_transcription.completed":
            print(f"🗣️ Оригінал: {resp.get('transcript', '')}")
        elif t == "response.audio_transcript.done":
            print(f" Переклад: {resp.get('transcript', '')}")
        elif t == "response.audio.delta":
            audio_b64 = resp.get("delta", "")
            if audio_b64:
                try:
                    audio_bytes = base64.b64decode(audio_b64)
                    # sounddevice працює з numpy arrays
                    audio_np = np.frombuffer(audio_bytes, dtype=np.int16)
                    sd.play(audio_np, SAMPLE_RATE_OUT, blocking=False)
                except Exception as e:
                    print(f"⚠️ Аудіо-помилка: {e}")

callback = ZoomCallback()

conv = OmniRealtimeConversation(model="qwen3.5-livetranslate-flash-realtime", url=WS_URL, callback=callback)
callback.conversation = conv

print("🔗 Підключення...")
conv.connect()

conv.update_session(
    output_modalities=[MultiModality.TEXT, MultiModality.AUDIO],
    voice="Tina",
    input_audio_transcription_model="qwen3-asr-flash-realtime",
    translation_params=TranslationParams(language=OUTPUT_LANG)
)

# Знайдіть індекс BlackHole 2ch через sd.query_devices()
DEVICE_IN = 0   # <-- ЗАМІНІТЬ НА ВАШ ІНДЕКС BlackHole
DEVICE_OUT = None  # sounddevice.play() використовує дефолтний вивід

print(f"🎤 Вхід: device {DEVICE_IN} | Вихід: default")
print(" Готово! Говоріть у Zoom")

def audio_callback(indata, frames, time, status):
    if status: print(f"⚠️ Аудіо-статус: {status}")
    # Перетворюємо на bytes для WebSocket
    audio_bytes = indata.flatten().tobytes()
    conv.append_audio(base64.b64encode(audio_bytes).decode())

try:
    with sd.InputStream(device=DEVICE_IN, channels=1, samplerate=SAMPLE_RATE_IN,
                        callback=audio_callback, dtype=np.int16, blocksize=int(SAMPLE_RATE_IN * CHUNK_DURATION)):
        signal.pause()
except KeyboardInterrupt:
    print("\n Завершення")
    conv.close()