# GPT Real-Time Translator (Zoom hybrid voice translator)

Real-time bidirectional voice translator for Zoom calls, routed through
virtual audio devices (BlackHole). Hybrid engine: incoming speech is
transcribed/translated by **Qwen** (`qwen3.5-livetranslate-flash-realtime`,
60 languages, low cost) into on-screen text; outgoing speech is translated
and re-synthesized by **OpenAI's realtime translate API** into spoken
English, so the other participant hears you translated live. Round-trip
latency was brought down from ~15s (a naive sequential STT → LLM → TTS
pipeline) to ~3s by streaming both directions concurrently instead of one
after another. Ships both a CLI (`main.py`) and a local web UI (`web.py` +
`webui.html`) with live status, a running cost estimate, and a
"hear your own translation" monitor toggle.

## Why a hybrid engine

- **Other participant → you**: Qwen (`qwen3.5-livetranslate-flash-realtime`)
  — instant text, cheap, supports 60 languages (29 with synthesized voice).
- **You → other participant**: OpenAI `gpt-realtime-translate` — speaks
  your translation out loud in English with low latency. Limitation: OpenAI
  only synthesizes ~13 languages, and Ukrainian isn't one of them — so that
  direction always goes through Qwen for text, with OpenAI generating the
  voice.

Both legs run concurrently rather than sequentially, which is what cuts
round-trip latency from ~15s to ~3s.

## Architecture

```
Microphone → BlackHole 2ch → Zoom "Microphone"
Zoom "Speaker" → BlackHole 16ch → this app → translation → headphones/output
```

- `main.py` — CLI entry point for the hybrid translator.
- `web.py` + `webui.html` — local web UI (language picker, start/stop, two
  translation panels, running cost counter).
- `bidirectional.py` — orchestrates both concurrent translation directions.
- `openai_translate_client.py` / `gemini_translate_client.py` /
  `livetranslate_client.py` — clients for the individual realtime APIs.
- `config.py` — languages, audio device lookup, `.env` loading.
- `probe_*.py`, `selftest.py`, `test_english.py` — targeted checks of
  individual APIs without a full run (key validity, protocol, transcription).

## Running it

```bash
cp .env.example .env   # fill in DASHSCOPE_API_KEY and OPENAI_API_KEY
python3 web.py          # -> http://localhost:8000
# or
python3 main.py          # CLI mode
```

Zoom/BlackHole setup and the full run protocol are in [`INSTRUCTION.md`](INSTRUCTION.md)
and the pre-call checklist [`PERED-DZVINKOM.md`](PERED-DZVINKOM.md).

## Cost

$0 infrastructure — pay-per-use API calls only (DashScope + OpenAI realtime),
roughly cents per call depending on duration.

## Caveats

- Requires your own API keys (DashScope + OpenAI) — not included in the repo.
- BlackHole is a third-party virtual audio driver for macOS, installed separately.
