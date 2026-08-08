# Setup guide — hybrid Zoom translator (Qwen + OpenAI)

> **Hybrid:** the "other participant → you" direction runs on **Qwen**
> (instant, free, text output); the "you → other participant" direction runs
> on **OpenAI's `gpt-realtime-translate`** (spoken English, low latency).
> **Both keys** are required in `.env`.
>
> Limitation: OpenAI only synthesizes ~13 languages (Ukrainian is **not**
> one of them — that's why that leg stays on Qwen for text). `--clone` and
> `--only-mine` **do nothing** in this mode (`gpt-realtime-translate` doesn't
> clone voices). The other participant's voice is a generic one.

## 🌐 Web UI (easiest)

```bash
cd "GPT real-time translator"
python3 web.py
```
Opens `http://localhost:8000` in the browser:
- top bar — the other participant's language, microphone, **Start/Stop**, status, elapsed time and ≈cost;
- left panel (large) — **translation of what the other participant says**, in your language (you read it);
- right panel — what **the other participant hears** in English (the voice goes into Zoom).
- "hear my own translation" checkbox — duplicates the English voice into your own headphones (for testing).

Same engine underneath (Qwen text-in + OpenAI voice-out) — the browser is just the UI on top.
Zoom and BlackHole are set up the same way (sections below). Ctrl+C in the terminal to quit.

## 0. Which devices to pick in Zoom

⚠️ **BlackHole device names can't be changed** (driver devices in Audio MIDI
Setup have locked names). So just remember the mapping — Zoom only asks
once and remembers it after:

| In Zoom | Device | Hint |
|---|---|---|
| **Microphone** | `BlackHole 2ch` | **2** = microphone |
| **Speaker** | `BlackHole 16ch` | **16** = you're listening to the other participant |

**(Optional) Rename via aggregate devices:** aggregate devices CAN be
renamed, so you can wrap each BlackHole in an aggregate named
`Translate Microphone` / `Translate Speaker` (Audio MIDI Setup → `+` →
Create Aggregate Device → check one BlackHole → rename). The script still
talks to the raw BlackHole device either way. Downside: an extra layer and
possible resampling, so leaving it as-is is more reliable.

## 1. One-time setup: two API keys

`.env` must contain **both** keys:
```
DASHSCOPE_API_KEY=sk-...   # Qwen (international Model Studio console) — direction toward you
OPENAI_API_KEY=sk-...      # OpenAI (platform.openai.com) — direction toward the other participant
```

**Verify the OpenAI key + protocol** (no microphone needed, runs uk→en through the API):
```bash
python3 probe_openai.py
```
Should print an English translation and return audio.

## 2. Zoom setup (check every time)

Zoom → **Settings → Audio**:
- **Microphone** → `BlackHole 2ch`
- **Speaker** → `BlackHole 16ch`
  - *(optional)* if you also want to HEAR the other participant's original
    voice, create a **Multi-Output Device** in Audio MIDI Setup
    (`BlackHole 16ch` + your headphones) and select that as the Speaker.

⚠️ **The single most important fix for choppy audio on the other end.** By
default Zoom processes the microphone (noise suppression / echo
cancellation / AGC) and **chops up the synthesized voice**, treating it as
"noise". Locally the audio is smooth — it only breaks up once it hits Zoom.
Turn these off:

1. **Settings → Audio → Music and Professional Audio** → enable
   **"Original sound for musicians"**.
2. A **"Original Sound: On"** button should then appear top-left in the
   call — make sure it's **on** (blue).
3. In the same panel, expand the options and **disable**:
   - **Echo cancellation** → Off
   - **High fidelity music mode** → On (smoother audio) *(optional)*
4. **Settings → Audio → Background noise suppression** → **Low** or **Off**
   (on "Auto/High" Zoom chops up TTS the hardest).

## 3. Before the call

- Wear **headphones** (to avoid echo).
- Speak into the **MacBook's built-in microphone** — NOT the headphone
  mic (Bluetooth would drop to HFP quality and break up the audio).

## 4. Running the translator

```bash
cd "GPT real-time translator"
python3 bidirectional.py --hear uk --speak en --mic MacBook
```

Options:
- `--hear uk` — the language YOU read the other participant's speech in
  (Qwen, text).
- `--speak en` — the **voice** language for the other participant (OpenAI).
  Only 13 output languages supported: `en, de, fr, es, it, pt, nl, pl, ja,
  ko, zh, ru, ar`. Anything else → an error with a hint.
- `--mic MacBook` — guarantees the built-in mic (24 kHz for OpenAI).
- ℹ️ This endpoint doesn't take a source-language hint, and it doesn't need
  one — verified that the model reliably detects Ukrainian without it
  (clean Cyrillic input).
- `--delay 1.0` — audio pre-buffer for the other participant (smoothness).
  Lower = faster.
- `--vad 300` — only affects the **toward-you** direction (Qwen text).
- `--latency` — timing info for diagnostics.
- `--quiet` — clean output, translation only (for a pre-call check).
- ⚠️ `--clone` and `--only-mine` are **ignored** — `gpt-realtime-translate`
  doesn't clone voices.

### Latency in GPT mode
OpenAI streams the translation **while you're speaking** (it doesn't wait
for a 2-second pause like the older model), so the toward-participant
direction is noticeably faster. Still, **speak in complete phrases with
short pauses** — the model synthesizes per finished clause (word order
differs between languages, so word-by-word sync is impossible — that's a
hard limit of the technology, not this implementation).
- Verify the server accepts the VAD settings: `python3 probe_session.py`.

In the terminal:
- `[← TO YOU] 🌐 …` — translation of what the other participant is saying (streamed)
- `[YOU →] 🌐 …` — translation of what went out to the other participant

Quit with **Ctrl+C**.

## 4½. Quick pre-call check (`--quiet`)

Fast sanity check that everything works — **translation only, no technical noise**:

```bash
python3 bidirectional.py --hear uk --speak en --clone --mic MacBook \
  --monitor "External Headphones" --vad 300 --only-mine --quiet
```

Steps:
1. Say a **short phrase** in your language (e.g. "Good afternoon, testing the connection.").
2. After ~1s you should **see** the English translation as text and **hear** it in your headphones.
3. Works → close it (Ctrl+C) and start the call.

`--quiet` strips out the JSON config dump, connection logs, timings and VAD
info — the screen shows only:
```
[YOU →] 🌐 Good day, communication check.
```
To get the diagnostic details back, drop `--quiet` and add `--latency`.

## 5. Testing without Zoom and without a second person

**Simplest — a solo test right in `bidirectional.py`:** add `--monitor`,
and the translation meant for the other participant is also duplicated
into your own headphones. Speak in your language → immediately hear how it
sounds in English (your cloned voice):

```bash
python3 bidirectional.py --hear uk --speak en --clone --mic MacBook \
  --monitor "External Headphones" --vad 300 --only-mine --latency
```

- `--only-mine` — doesn't echo your own English back (for when you're
  playing "the other participant"). Decided by **source↔translation
  similarity** (English=English → muted; your language, even if ASR writes
  it in Latin script, → still synthesized). This mode **buffers the whole
  phrase** (the decision needs the full translation), so latency is a bit
  higher — expected for a solo test.
- **Default audio mode buffers each phrase** (no crackling). Short phrases
  add ~1s latency. `--stream` = streaming mode (faster, but can crackle).
- Speak in **short phrases with a pause** — each one gets synthesized
  separately, fast and smooth.

- `--only-mine` — only **your own language** gets synthesized. When you're
  playing "the other participant" and speaking English, the translation
  is NOT echoed back into your headphones (it detects the source script and
  decides: Cyrillic = yours, Latin = not). In the terminal, an English turn
  is marked `🔇 ... audio muted`.
- `--latency` — adds timestamps and latency figures to the log:
  `[12:34:56 +1.2s] 🌐 translation …   ⏱ total +2.4s`
  (time to first word; latency from the pause; total latency to end of phrase).
- `--monitor` with no value → system output (your headphones).
- `--monitor Redmi` / `--monitor 2` → a specific device.
- The monitor has **its own stream and buffer** (`--monitor-delay`, default
  1.2s), independent of the main output. If your headphones still crackle,
  raise it: `--monitor-delay 2.0`. (Not to be confused with `--delay`,
  which is only for the Zoom cable path.)
- ⚠️ Always use `--mic MacBook` — speak into the built-in mic, headphones
  are for listening only (otherwise Bluetooth drops to HFP quality and the
  audio breaks up).
- Flow: you play "the local speaker" (speak your language), hear the
  English reply, and reply "as the foreign participant". The `BlackHole
  2ch` cable receives the same audio, so the Zoom path is verified in
  parallel.

```bash
# older quick scenarios:
python3 selftest.py in            # speak English → read the translation in your language
python3 selftest.py out --clone   # speak your language → hear English (your voice)

# full test over real virtual cables (2 terminals):
python3 bidirectional.py --hear uk --speak en --clone --mic MacBook   # terminal 1
python3 fake_zoom.py                                                   # terminal 2
```

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `DASHSCOPE_API_KEY not found` | add the key to `.env` |
| HTTP 401 | key isn't from the international console (Singapore region) |
| `Translate Speaker/... not found` | `sudo killall coreaudiod`, then `python3 list_devices.py` |
| Audio breaks up for the OTHER PARTICIPANT | in Zoom: Original Sound = On, Echo cancellation = Off, Background noise suppression = Low/Off (section 2) |
| Audio breaks up for YOU (headphones) | speak into the MacBook mic (`--mic MacBook`), listen on headphones |
| High latency | short sentences + pause; `--vad 300 --delay 0.4`; measure with `--latency` |
| Other participant can't hear you | in Zoom, Microphone = `Translate Microphone`, Original Sound enabled |
| SSL error | run `"/Applications/Python 3.14/Install Certificates.command"` |

List devices any time: `python3 list_devices.py`
