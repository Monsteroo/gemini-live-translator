"""Shared configuration: .env loading, language presets, audio-device lookup."""
import os

# --- Supported target languages for qwen3.5-livetranslate-flash-realtime ---
# Source per official docs: 29 audio+text languages, 31 text-only (60 total).
# https://www.alibabacloud.com/help/en/model-studio/qwen3-5-livetranslate-flash-realtime

# 29 languages with synthesized AUDIO output (+ text).
VOICE_CAPABLE = {
    "zh", "en", "ar", "de", "fr", "es", "pt", "id", "it", "ko", "ru", "th",
    "vi", "ja", "tr", "hi", "ms", "nl", "ur", "nb", "sv", "da", "he", "fi",
    "pl", "is", "cs", "fil", "fa",
}

# All 60 supported target languages (voice-capable ones above + text-only ones below).
LANGUAGES = {
    # --- audio + text ---
    "zh": "Chinese", "en": "English", "ar": "Arabic", "de": "German",
    "fr": "French", "es": "Spanish", "pt": "Portuguese", "id": "Indonesian",
    "it": "Italian", "ko": "Korean", "ru": "Russian", "th": "Thai",
    "vi": "Vietnamese", "ja": "Japanese", "tr": "Turkish", "hi": "Hindi",
    "ms": "Malay", "nl": "Dutch", "ur": "Urdu", "nb": "Norwegian Bokmål",
    "sv": "Swedish", "da": "Danish", "he": "Hebrew", "fi": "Finnish",
    "pl": "Polish", "is": "Icelandic", "cs": "Czech", "fil": "Filipino",
    "fa": "Persian",
    # --- text only (no synthesized voice) ---
    "yue": "Cantonese", "el": "Greek", "af": "Afrikaans", "ast": "Asturian",
    "be": "Belarusian", "bg": "Bulgarian", "bn": "Bengali", "bs": "Bosnian",
    "ca": "Catalan", "ceb": "Cebuano", "et": "Estonian", "gl": "Galician",
    "gu": "Gujarati", "hr": "Croatian", "hu": "Hungarian", "jv": "Javanese",
    "kk": "Kazakh", "kn": "Kannada", "ky": "Kyrgyz", "lv": "Latvian",
    "mk": "Macedonian", "ml": "Malayalam", "mr": "Marathi", "pa": "Punjabi",
    "ro": "Romanian", "sk": "Slovak", "sl": "Slovenian", "sw": "Swahili",
    "tg": "Tajik", "az": "Azerbaijani", "uk": "Ukrainian",
}

# NOTE: Ukrainian (uk) IS supported, but TEXT-ONLY (no synthesized voice).


# Мови з кириличним письмом (для гейту аудіо за скриптом джерела).
CYRILLIC_LANGS = {"uk", "ru", "be", "bg", "mk", "kk", "ky", "tg", "sr", "mn"}


def lang_script(code: str) -> str:
    """Очікуваний скрипт письма для коду мови: 'cyrillic' або 'latin'."""
    return "cyrillic" if code in CYRILLIC_LANGS else "latin"


def has_script(text: str, script: str) -> bool:
    """Чи містить текст хоч один символ заданого скрипту ('cyrillic'/'latin')."""
    if script == "cyrillic":
        return any("Ѐ" <= ch <= "ӿ" for ch in text)
    if script == "latin":
        return any(("a" <= ch <= "z") or ("A" <= ch <= "Z") for ch in text)
    return False


def detect_script(text: str) -> str | None:
    """Визначити скрипт тексту: 'cyrillic' (є кирилиця), 'latin' (є лат. літери),
    або None, якщо літер немає (лише цифри/пунктуація — рішення відкласти)."""
    for ch in text:
        if "Ѐ" <= ch <= "ӿ":
            return "cyrillic"
    for ch in text:
        if ("a" <= ch <= "z") or ("A" <= ch <= "Z"):
            return "latin"
    return None


def load_env(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency). Sets os.environ keys
    only if they are not already set in the real environment."""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def get_api_key() -> str:
    key = os.environ.get("DASHSCOPE_API_KEY")
    if not key:
        raise SystemExit(
            "❌ DASHSCOPE_API_KEY не знайдено.\n"
            "   Створіть файл .env (див. .env.example) або зробіть:\n"
            "   export DASHSCOPE_API_KEY='sk-...'"
        )
    return key


def get_openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise SystemExit(
            "❌ OPENAI_API_KEY не знайдено.\n"
            "   Додайте у файл .env рядок:  OPENAI_API_KEY=sk-...\n"
            "   (ключ з platform.openai.com — потрібен для напрямку ВИ→співрозмовник)"
        )
    return key


def get_gemini_key() -> str:
    # Google AI Studio key (aistudio.google.com). Auth — через ?key= у URL.
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit(
            "❌ GEMINI_API_KEY не знайдено.\n"
            "   Додайте у файл .env рядок:  GEMINI_API_KEY=...\n"
            "   (ключ з aistudio.google.com — для Gemini Live Translate)"
        )
    return key


# Назви віртуальних кабелів для пошуку. Скрипт працює з СИРИМ BlackHole, тому
# шукаємо його ПЕРШИМ (щоб не зачепити однойменний агрегат-обгортку, який може
# додавати ресемплінг). «Дружня» назва — лише як запасний варіант.
#   BlackHole 16ch (Zoom Speaker → скрипт читає)   [вхід]
#   BlackHole 2ch  (скрипт пише → Zoom Microphone) [вихід]
CABLE_IN_NAMES = ["BlackHole 16ch", "translate speaker"]
CABLE_OUT_NAMES = ["BlackHole 2ch", "translate microphone"]


def find_cable(pyaudio_instance, names, *, want_input: bool):
    """Шукає пристрій за списком можливих назв (перша знайдена перемагає)."""
    for n in names:
        idx = find_device(pyaudio_instance, n, want_input=want_input)
        if idx is not None:
            return idx
    return None


def find_device(pyaudio_instance, name_substring: str, *, want_input: bool):
    """Return the index of the first device whose name contains name_substring
    and has the requested I/O capability. None if not found."""
    name_substring = name_substring.lower()
    for i in range(pyaudio_instance.get_device_count()):
        info = pyaudio_instance.get_device_info_by_index(i)
        if name_substring not in info["name"].lower():
            continue
        if want_input and info["maxInputChannels"] > 0:
            return i
        if not want_input and info["maxOutputChannels"] > 0:
            return i
    return None


def list_devices(pyaudio_instance) -> None:
    """Pretty-print all audio devices with their indices and I/O capability."""
    print("\n=== Аудіопристрої ===")
    for i in range(pyaudio_instance.get_device_count()):
        info = pyaudio_instance.get_device_info_by_index(i)
        io = []
        if info["maxInputChannels"] > 0:
            io.append(f"IN:{info['maxInputChannels']}")
        if info["maxOutputChannels"] > 0:
            io.append(f"OUT:{info['maxOutputChannels']}")
        print(f"  [{i}] {info['name']}  ({', '.join(io)})  @ {int(info['defaultSampleRate'])}Hz")
    print()
