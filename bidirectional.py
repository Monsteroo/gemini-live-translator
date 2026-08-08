#!/usr/bin/env python3
"""ГІБРИДНИЙ двосторонній real-time переклад для Zoom.

Два двигуни — кожен робить те, що вміє найкраще:

  НАПРЯМОК A (співрозмовник → ви):   Qwen livetranslate
      Zoom Speaker → BlackHole 16ch → [переклад на ВАШУ мову, uk] → ТЕКСТ у терміналі
  НАПРЯМОК B (ви → співрозмовник):   OpenAI gpt-realtime-translate
      ваш мікрофон → [переклад на ЙОГО мову, голос, низька затримка] → BlackHole 2ch → Zoom

Чому гібрид: OpenAI gpt-realtime-translate дає НИЗЬКУ затримку голосу, але НЕ
підтримує українську на виході (лише 13 мов). Тому напрямок «чути співрозмовника
українською» лишається на Qwen (миттєвий текст), а напрямок «ваш голос →
англійська співрозмовнику» — на OpenAI (швидкий голос).

Потрібні ОБИДВА ключі у .env: DASHSCOPE_API_KEY (Qwen) і OPENAI_API_KEY (OpenAI).

Приклад:  python3 bidirectional.py --hear uk --speak en
"""
import argparse
import asyncio
import pyaudio

from config import (
    load_env, get_api_key, get_openai_key, LANGUAGES, VOICE_CAPABLE,
    find_device, find_cable, list_devices, CABLE_IN_NAMES, CABLE_OUT_NAMES,
)
from livetranslate_client import LiveTranslateClient
from openai_translate_client import OpenAITranslateClient

# Вихідні мови, які підтримує gpt-realtime-translate (≈13). Українська — НЕ серед них.
OPENAI_OUTPUT_LANGS = {
    "en", "de", "fr", "es", "it", "pt", "nl", "pl", "ja", "ko", "zh", "ru", "ar",
}

# Підписи для друку (фактичний пошук — за CABLE_IN_NAMES / CABLE_OUT_NAMES).
CABLE_INCOMING = "Translate Speaker (BlackHole 16ch)"   # Zoom Speaker → скрипт
CABLE_OUTGOING = "Translate Microphone (BlackHole 2ch)"  # скрипт → Zoom Microphone


def choose_language(prompt: str, default: str) -> str:
    items = list(LANGUAGES.items())
    print(f"\n{prompt}")
    for i, (code, name) in enumerate(items, 1):
        voice = "🔊" if code in VOICE_CAPABLE else "📝"
        print(f"  {i:2}. {voice} {name} ({code})")
    raw = input(f"Номер або код мови [{default}]: ").strip().lower()
    if not raw:
        return default
    if raw in LANGUAGES:
        return raw
    if raw.isdigit() and 1 <= int(raw) <= len(items):
        return items[int(raw) - 1][0]
    print(f"⚠️ Не розпізнано '{raw}', беру {default}")
    return default


async def run(hear=None, speak=None, clone=False, mic=None,
              delay=1.0, vad=500, latency=False, monitor=None, monitor_delay=1.0,
              only_mine=False, smooth=True, quiet=False):
    load_env()
    api_key = get_api_key()        # Qwen (напрямок A)
    openai_key = get_openai_key()  # OpenAI (напрямок B)

    pa = pyaudio.PyAudio()
    if not quiet:
        list_devices(pa)

    # --- Автовизначення пристроїв ---
    in_cable = find_cable(pa, CABLE_IN_NAMES, want_input=True)
    out_cable = find_cable(pa, CABLE_OUT_NAMES, want_input=False)
    # Мікрофон: явний (--mic за індексом/назвою) або системний за замовчуванням,
    # але НЕ віртуальний кабель (інакше скрипт читатиме тишу/власний вивід).
    default_mic, mic_name = None, ""
    if mic is not None:
        default_mic = int(mic) if mic.isdigit() else find_device(pa, mic, want_input=True)
        if default_mic is not None:
            mic_name = pa.get_device_info_by_index(default_mic)["name"]
    if default_mic is None:
        try:
            default_mic = pa.get_default_input_device_info()["index"]
            mic_name = pa.get_device_info_by_index(default_mic)["name"]
        except Exception:
            default_mic, mic_name = None, ""
    # Уникаємо віртуального кабелю та Bluetooth-мікрофона (Bluetooth впаде в HFP
    # і зіпсує звук у навушниках) — віддаємо перевагу вбудованому мікрофону.
    bad = ("blackhole" in mic_name.lower()) or ("redmi" in mic_name.lower()) or ("airpods" in mic_name.lower())
    if mic is None and (default_mic is None or bad):
        mac = find_device(pa, "MacBook", want_input=True)
        if mac is not None:
            default_mic = mac
            mic_name = pa.get_device_info_by_index(mac)["name"]

    # --- Монітор: куди ще ДУБЛЮВАТИ переклад для співрозмовника (ваші навушники),
    #     щоб чути його самому при сольному тесті. ---
    monitor_idx, monitor_name = None, ""
    if monitor is not None:
        if monitor == "__default__":
            # системний вихід за замовчуванням (зазвичай ваші навушники)
            try:
                monitor_idx = pa.get_default_output_device_info()["index"]
            except Exception:
                monitor_idx = None
        else:
            monitor_idx = int(monitor) if monitor.isdigit() else find_device(pa, monitor, want_input=False)
        if monitor_idx is not None:
            monitor_name = pa.get_device_info_by_index(monitor_idx)["name"]
            # не дублюємо у віртуальний кабель — це не для слухання
            if "blackhole" in monitor_name.lower():
                spk = find_device(pa, "MacBook", want_input=False)
                monitor_idx = spk
                monitor_name = pa.get_device_info_by_index(spk)["name"] if spk is not None else ""
        if monitor_idx is None:
            print(f"⚠️ Монітор-пристрій '{monitor}' не знайдено — переклад НЕ дублюватиметься у навушники.")
    pa.terminate()

    if in_cable is None:
        raise SystemExit(
            f"❌ Не знайдено вхідний пристрій '{CABLE_INCOMING}'.\n"
            "   Перевірте, що BlackHole 16ch встановлено і CoreAudio перезавантажено\n"
            "   (sudo killall coreaudiod)."
        )
    if out_cable is None:
        raise SystemExit(
            f"❌ Не знайдено вихідний пристрій '{CABLE_OUTGOING}'.\n"
            "   Встановіть BlackHole 2ch і перезавантажте CoreAudio."
        )

    if default_mic is None:
        raise SystemExit("❌ Не знайдено реальний мікрофон для вашого голосу.")

    if not quiet:
        print(f"✅ Вхід (від співрозмовника): [{in_cable}] {CABLE_INCOMING}")
        print(f"✅ Ваш мікрофон: [{default_mic}] {mic_name}")
        print(f"✅ Вихід (до співрозмовника): [{out_cable}] {CABLE_OUTGOING}")
        if monitor_idx is not None:
            print(f"🎧 Монітор (ви теж чуєте переклад): [{monitor_idx}] {monitor_name}")

    # --- Вибір мов (CLI має пріоритет, інакше інтерактивно) ---
    def resolve(val, prompt, default):
        if val and val.lower() in LANGUAGES:
            return val.lower()
        if val:
            print(f"⚠️ Невідома мова '{val}', питаю інтерактивно.")
        return choose_language(prompt, default)

    my_lang = resolve(hear, "Якою мовою ВИ хочете ЧУТИ співрозмовника?", "en")
    their_lang = resolve(speak, "На яку мову перекладати ДЛЯ співрозмовника?", "fr")

    in_voice = my_lang in VOICE_CAPABLE
    if not in_voice:
        print(f"📝 {LANGUAGES[my_lang]} — лише текст (так і має бути для української).")
    if their_lang not in OPENAI_OUTPUT_LANGS:
        raise SystemExit(
            f"❌ OpenAI gpt-realtime-translate не вміє '{LANGUAGES.get(their_lang, their_lang)}' "
            f"на виході.\n   Доступні: {', '.join(sorted(OPENAI_OUTPUT_LANGS))}.\n"
            "   Оберіть іншу --speak або скористайтесь Qwen-версією проєкту."
        )

    # НАПРЯМОК A (співрозмовник → ви): Qwen → ваша мова ТЕКСТОМ. Вхід = BlackHole 16ch.
    client_in = LiveTranslateClient(
        api_key=api_key, target_language=my_lang, audio_enabled=in_voice,
        input_device_index=in_cable, output_device_index=None,
        label="[← ВАМ] ",
        vad_silence_ms=vad, log_latency=latency, quiet=quiet,
    )
    # НАПРЯМОК B (ви → співрозмовник): OpenAI gpt-realtime-translate → ГОЛОС, низька
    # затримка. Вхід = ваш мікрофон (24kHz), вихід = BlackHole 2ch (+ монітор).
    if clone:
        print("ℹ️ --clone ігнорується: gpt-realtime-translate не клонує голос (генеричний голос).")
    if only_mine:
        print("ℹ️ --only-mine недоступний у GPT-режимі (потоковий, без меж фрази).")
    client_out = OpenAITranslateClient(
        api_key=openai_key, target_language=their_lang, audio_enabled=True,
        input_device_index=default_mic, output_device_index=out_cable,
        monitor_device_index=monitor_idx, monitor_delay=monitor_delay,
        label="[ВИ →] ",
        playback_delay=delay, log_latency=latency, quiet=quiet,
    )

    if quiet:
        print(f"\n  {LANGUAGES[my_lang]} ⟵ ВИ ЧУЄТЕ   |   ВИ ГОВОРИТЕ ⟶ {LANGUAGES[their_lang]}"
              f"   (Ctrl+C — вихід)\n")
    else:
        print("\n" + "=" * 60)
        print(f"  {LANGUAGES[my_lang]} ⟵ ВИ ЧУЄТЕ   |   ВИ ГОВОРИТЕ ⟶ {LANGUAGES[their_lang]}")
        print("  Ctrl+C для виходу")
        print("=" * 60 + "\n")

    await asyncio.gather(client_in.connect(), client_out.connect())
    client_in.start_audio_player()
    client_out.start_audio_player()

    tasks = [
        asyncio.create_task(client_in.handle_server_messages(None)),
        asyncio.create_task(client_in.start_microphone_streaming()),
        asyncio.create_task(client_out.handle_server_messages(None)),
        asyncio.create_task(client_out.start_microphone_streaming()),
    ]
    try:
        await asyncio.gather(*tasks)
    finally:
        await client_in.close()
        await client_out.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Двосторонній real-time переклад для Zoom (Qwen 3.5).")
    ap.add_argument("--hear", help="мова, якою ВИ чуєте/читаєте співрозмовника (напр. uk, ru, en)")
    ap.add_argument("--speak", help="мова, на яку перекладати ДЛЯ співрозмовника (напр. en, fr, de)")
    ap.add_argument("--clone", action="store_true", help="клонувати ваш голос: переклад звучить вашим голосом")
    ap.add_argument("--mic", help="мікрофон для вашого голосу: індекс або назва (напр. MacBook)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="пре-буфер аудіо для співрозмовника, с (покриває розриви дельт ~0.75с; дефолт 1.0)")
    ap.add_argument("--vad", type=int, default=500,
                    help="пауза для завершення фрази, мс (менше=швидше переклад; дефолт 500)")
    ap.add_argument("--latency", action="store_true",
                    help="показувати таймінги VAD→аудіо для діагностики затримки")
    ap.add_argument("--monitor", nargs="?", const="__default__", default=None,
                    help="ДУБЛЮВАТИ переклад для співрозмовника у ваші навушники "
                         "(сольний тест). Без значення — системний вихід; або вкажіть "
                         "назву/індекс (напр. --monitor Redmi)")
    ap.add_argument("--monitor-delay", type=float, default=1.0,
                    help="пре-буфер для монітора-навушників, с (дефолт 1.0)")
    ap.add_argument("--only-mine", action="store_true",
                    help="озвучувати лише вашою рідною мовою (--hear); ваше англ. "
                         "мовлення не дублюватиметься назад (для сольного тесту)")
    ap.add_argument("--stream", action="store_true",
                    help="потокове аудіо (стартує раніше на довгих фразах, але може "
                         "ЛЕГКО тріщати). За замовч. — плавний режим: буферить фразу "
                         "(без тріску; на коротких фразах затримка ~1с)")
    ap.add_argument("--quiet", action="store_true",
                    help="чистий вивід: лише переклад, без технічних логів "
                         "(для швидкої перевірки перед дзвінком)")
    args = ap.parse_args()
    if args.quiet:
        args.latency = False  # чистий тест — без таймінгів/VAD
    try:
        asyncio.run(run(hear=args.hear, speak=args.speak, clone=args.clone, mic=args.mic,
                        delay=args.delay, vad=args.vad, latency=args.latency,
                        monitor=args.monitor, monitor_delay=args.monitor_delay,
                        only_mine=args.only_mine, smooth=not args.stream, quiet=args.quiet))
    except KeyboardInterrupt:
        print("\n\nЗавершення.")
