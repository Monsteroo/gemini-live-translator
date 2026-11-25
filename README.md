# GPT Real-Time Translator (Zoom voice translator)

Real-time bidirectional voice translator for Zoom calls, routed through
virtual audio devices (BlackHole). Two interchangeable engines:

- **Gemini (default)** — one model for both directions:
  [`gemini-3.5-live-translate-preview`](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-live-3-5-translate/)
  (70+ languages in, 70+ out, **Ukrainian works as a spoken output**). Needs
  only `GEMINI_API_KEY`.
- **Hybrid (fallback)** — incoming speech goes through **Qwen**
  (`qwen3.5-livetranslate-flash-realtime`, text), your outgoing speech through
  **OpenAI `gpt-realtime-translate`** (spoken, 13 output languages, no
  Ukrainian). Needs `DASHSCOPE_API_KEY` + `OPENAI_API_KEY`.

If Gemini is selected but `GEMINI_API_KEY` is missing, the app falls back to
the hybrid engine automatically. Both directions stream concurrently, which
cut the round trip from ~15s (sequential STT → LLM → TTS) to a few seconds.
Ships a CLI (`bidirectional.py`, hybrid only) and a local web UI (`web.py` +
`webui.html`) with an engine switch, live status, a running cost estimate and a
"hear your own translation" monitor toggle.

## Engines compared

| | Gemini 3.5 Live Translate | Hybrid (Qwen + OpenAI) |
|---|---|---|
| Direction → you | Gemini (text) | Qwen (text) |
| Direction → them | Gemini (voice, 70+ langs) | OpenAI (voice, 13 langs) |
| Ukrainian voice output | yes | no |
| Status | public preview | OpenAI GA, Qwen GA |
| Cost (approx.) | ≈ $0.037/min **per direction** → ≈ $0.074/min | ≈ $0.034/min (OpenAI) + Qwen |

OpenAI output languages: `en, de, fr, es, it, pt, nl, pl, ja, ko, zh, ru, ar`.

## Architecture

```
Microphone → BlackHole 2ch → Zoom "Microphone"
Zoom "Speaker" → BlackHole 16ch → this app → translation → headphones/output
```

Wired headphones are required: speakers leak into the mic and get re-translated.

## Project layout

```
web.py, webui.html          local web UI (engine switch, panels, cost, latency test)
config.py                   languages, audio devices, .env loading, mic_loop
gemini_translate_client.py  Gemini Live Translate client (default engine)
openai_translate_client.py  OpenAI gpt-realtime-translate client (hybrid, voice out)
livetranslate_client.py     Qwen livetranslate client (hybrid, text in)
bidirectional.py, main.py   CLIs (hybrid / Qwen only)
tools/                      probes, self-tests, device listing, fake Zoom
docs/                       setup guide, pre-call checklist, test dialogue
```

## Running it

```bash
cp .env.example .env            # fill in GEMINI_API_KEY (and/or DASHSCOPE + OPENAI keys)
pip install -r requirements.txt # websockets, pyaudio (needs portaudio: brew install portaudio)
python3 web.py                  # -> http://localhost:8000
```

Quick API checks (run from the project root): `python3 tools/probe_gemini.py [uk|en]`,
`python3 tools/probe_openai.py`, `python3 tools/list_devices.py`.

Zoom/BlackHole setup and the full run protocol: [`docs/INSTRUCTION.md`](docs/INSTRUCTION.md);
pre-call checklist: [`docs/PERED-DZVINKOM.md`](docs/PERED-DZVINKOM.md).

## Latency

Measured with the built-in **⏱ Latency test** (Gemini engine): first translated
audio reaches the output device ≈2.3–3.4 s after speech starts; the tail ends
≈2.7–3 s after you stop. This is the model's floor — buffer and VAD tweaks gave
no measurable gain. Palabra (S2S) was tested and was slower (≈3.1 s first
audio, ≈5 s tail), so the app stays on Gemini. Results are logged to
`latency_log.jsonl` (git-ignored).

Known limitation: Gemini replicates the speaker's voice and offers no voice
selection, so the voice can drift between phrases.

## Cost

$0 infrastructure — pay-per-use API calls only. The web UI shows a running
estimate (Gemini: two sessions × ≈$0.037/min; hybrid: ≈$0.034/min).

## Caveats

- Requires your own API keys — not included in the repo.
- Gemini 3.5 Live Translate is a **preview** model: behavior and pricing may change.
- BlackHole is a third-party virtual audio driver for macOS, installed separately.
