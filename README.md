# GPT Real-Time Translator (Zoom hybrid voice translator)

**TL;DR (EN):** Real-time bidirectional voice translator for Zoom calls, routed
through virtual audio devices (BlackHole). Hybrid engine: incoming speech is
transcribed/translated by **Qwen** (`qwen3.5-livetranslate-flash-realtime`,
60 languages, low cost) into on-screen text; outgoing speech is translated
and re-synthesized by **OpenAI's realtime translate API** into spoken English
so the other participant hears you translated live. Round-trip latency
brought down from ~15s (naive STT → LLM → TTS pipeline) to ~3s by streaming
both legs concurrently instead of sequentially. Ships both a CLI (`main.py`)
and a local web UI (`web.py` + `webui.html`) with live status, running cost
estimate, and a "hear your own translation" monitor toggle.

## Чому гібрид

- **Співрозмовник → ви**: Qwen (`qwen3.5-livetranslate-flash-realtime`) —
  текст миттєво, дешево, підтримує 60 мов (29 із синтезом голосу).
- **Ви → співрозмовник**: OpenAI `gpt-realtime-translate` — озвучує ваш
  переклад англійською з низькою затримкою. Обмеження: OpenAI озвучує лише
  ~13 мов, українська не серед них — тому цей напрямок завжди йде в текст
  через Qwen, а голос генерує OpenAI.

Обидва легі працюють паралельно (не послідовно), звідси падіння затримки
з ~15с до ~3с.

## Архітектура

```
Мікрофон → BlackHole 2ch → Zoom "Microphone"
Zoom "Speaker" → BlackHole 16ch → цей скрипт → переклад → навушники/вихід
```

- `main.py` — CLI-запуск гібридного перекладача.
- `web.py` + `webui.html` — локальний веб-інтерфейс (вибір мови, старт/стоп,
  дві панелі перекладу, лічильник вартості).
- `bidirectional.py` — оркестрація двох одночасних напрямків перекладу.
- `openai_translate_client.py` / `gemini_translate_client.py` /
  `livetranslate_client.py` — клієнти конкретних realtime-API.
- `config.py` — мови, аудіо-девайси, завантаження `.env`.
- `probe_*.py`, `selftest.py`, `test_english.py` — точкові перевірки
  окремих API без повного запуску (ключ, протокол, транскрипція).

## Запуск

```bash
cp .env.example .env   # вписати DASHSCOPE_API_KEY і OPENAI_API_KEY
python3 web.py          # -> http://localhost:8000
# або
python3 main.py          # CLI-режим
```

Налаштування Zoom/BlackHole та повний протокол — `INSTRUCTION.md` та
`PERED-DZVINKOM.md`.

## Вартість

$0 інфраструктури — лише pay-per-use API (DashScope + OpenAI realtime),
орієнтовно центи за дзвінок залежно від тривалості.

## Застереження

- Потрібні власні API-ключі (DashScope + OpenAI) — не входять у репозиторій.
- BlackHole — сторонній віртуальний аудіодрайвер для macOS, встановлюється
  окремо.
