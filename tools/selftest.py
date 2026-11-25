#!/usr/bin/env python3
"""Самотестування БЕЗ Zoom і БЕЗ BlackHole — ви граєте обидві ролі.

Читає ваш мікрофон напряму, перекладає і виводить у навушники/термінал.
Серверний VAD сам завершує репліку на паузі (як у реальній розмові) —
просто говоріть і робіть паузу.

Режими:
  out  — ви говорите УКРАЇНСЬКОЮ → чуєте АНГЛІЙСЬКИЙ голос (що почув би
         співрозмовник). З --clone звучить вашим голосом. [напрямок «ви →»]
  in   — ви говорите АНГЛІЙСЬКОЮ → читаєте УКРАЇНСЬКИЙ текст (що бачили б
         ви на дзвінку). [напрямок «→ вам»]

Приклади:
  python3 selftest.py out --clone
  python3 selftest.py in
"""
import argparse
import asyncio

import pyaudio

import sys as _sys, pathlib as _pl; _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent.parent))  # project root
from config import load_env, get_api_key, LANGUAGES, VOICE_CAPABLE, find_device, list_devices
from livetranslate_client import LiveTranslateClient


def _resolve(pa, spec, want_input):
    """spec може бути індексом ('5') або частиною назви ('MacBook')."""
    if spec is None:
        return None
    if spec.isdigit():
        return int(spec)
    return find_device(pa, spec, want_input=want_input)


def pick_devices(mode, mic_spec, spk_spec):
    """Повертає (input_idx, output_idx, names). Уникає Bluetooth-дуплексу:
    якщо вхід і вихід — той самий пристрій (BT перейде в HFP і звук рватиме),
    переносимо ВХІД на вбудований мікрофон MacBook."""
    pa = pyaudio.PyAudio()
    list_devices(pa)
    in_idx = _resolve(pa, mic_spec, True)
    out_idx = _resolve(pa, spk_spec, False)
    if in_idx is None:
        try:
            in_idx = pa.get_default_input_device_info()["index"]
        except Exception:
            in_idx = None
    # Для режиму з відтворенням (out) уникаємо дуплексу на одному пристрої.
    if mode == "out" and out_idx is None:
        try:
            out_idx = pa.get_default_output_device_info()["index"]
        except Exception:
            out_idx = None
        # Той самий ФІЗИЧНИЙ пристрій має однакову назву для входу й виходу
        # (у Bluetooth вхід і вихід — окремі індекси, але назва спільна).
        in_nm = pa.get_device_info_by_index(in_idx)["name"] if in_idx is not None else ""
        out_nm = pa.get_device_info_by_index(out_idx)["name"] if out_idx is not None else ""
        if in_idx is not None and out_idx is not None and in_nm == out_nm:
            mac_mic = find_device(pa, "MacBook", want_input=True)
            if mac_mic is not None and pa.get_device_info_by_index(mac_mic)["name"] != out_nm:
                in_idx = mac_mic
                print(f"⚠️ Вхід і вихід були одним пристроєм ({out_nm}) — перемкнув ВХІД на "
                      "вбудований мікрофон MacBook, щоб Bluetooth не падав у HFP і звук не рвало.")
    def nm(i):
        return pa.get_device_info_by_index(i)["name"] if i is not None else "(default)"
    names = (nm(in_idx), nm(out_idx))
    pa.terminate()
    return in_idx, out_idx, names


async def run(mode: str, hear: str, speak: str, clone: bool, mic_spec=None, spk_spec=None, delay=1.5, dump=None):
    load_env()
    api_key = get_api_key()
    in_idx, out_idx, (in_name, out_name) = pick_devices(mode, mic_spec, spk_spec)
    print(f"🎤 Вхід: {in_name}   🔊 Вихід: {out_name}")

    if mode == "out":
        target = speak                      # перекладаємо НА мову співрозмовника
        audio = target in VOICE_CAPABLE     # хочемо чути голос
        src_hint = "українською (як ВИ)"
        out_hint = f"{LANGUAGES.get(target, target)} голосом (як почує співрозмовник)"
    else:  # in
        target = hear                       # перекладаємо НА вашу мову
        audio = False                       # лише текст у терміналі
        src_hint = f"{LANGUAGES.get(speak, speak)} (як співрозмовник)"
        out_hint = f"{LANGUAGES.get(target, target)} текстом у терміналі"

    use_clone = clone and audio
    client = LiveTranslateClient(
        api_key=api_key, target_language=target, audio_enabled=audio,
        input_device_index=in_idx, output_device_index=out_idx,
        label="🗣️→🌐 ", enable_voice_clone=use_clone, voice_clone_frequency="once",
        playback_delay=delay,  # потокове аудіо із буфером на старті (плавно)
        audio_dump_path=dump,
    )
    if audio:
        print(f"⏱️  Затримка старту аудіо (буфер): {delay:.1f} с — змінити: --delay")

    print("\n" + "=" * 60)
    print(f"  РЕЖИМ: {mode.upper()}")
    print(f"  Говоріть {src_hint}")
    print(f"  Отримаєте: {out_hint}")
    if use_clone:
        print("  🧬 Клонування голосу увімкнено")
    print("  Говоріть, робіть паузу — і слухайте/дивіться результат.")
    print("  Ctrl+C для виходу.")
    print("=" * 60 + "\n")

    await client.connect()
    client.start_audio_player()
    try:
        await asyncio.gather(
            client.handle_server_messages(None),
            client.start_microphone_streaming(),
        )
    finally:
        await client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Самотест перекладача без Zoom.")
    ap.add_argument("mode", choices=["out", "in"], help="out: ви→англ (голос) | in: англ→ви (текст)")
    ap.add_argument("--hear", default="uk", help="ваша мова для режиму in (за замовч. uk)")
    ap.add_argument("--speak", default="en", help="мова співрозмовника для режиму out/in (за замовч. en)")
    ap.add_argument("--clone", action="store_true", help="клонувати ваш голос (тільки режим out)")
    ap.add_argument("--mic", help="мікрофон: індекс або частина назви (напр. MacBook)")
    ap.add_argument("--speaker", help="вихід: індекс або частина назви (напр. Redmi)")
    ap.add_argument("--delay", type=float, default=1.5, help="буфер на старті аудіо, с (за замовч. 1.5)")
    ap.add_argument("--dump", help="зберегти отримане аудіо у WAV для діагностики")
    args = ap.parse_args()
    try:
        asyncio.run(run(args.mode, args.hear, args.speak, args.clone, args.mic, args.speaker, args.delay, args.dump))
    except KeyboardInterrupt:
        print("\n\nЗавершення.")
