# ✅ Pre-call connection check

Go top to bottom. Takes ~3 minutes total. If any step fails — **don't start the call**, fix it first.

---

## 0. Before launching (once a day / after a reboot)

- [ ] Headphones connected (**wired** is better — less choppy audio).
- [ ] BlackHole installed: `BlackHole 16ch` and `BlackHole 2ch` show up in **Audio MIDI Setup**.
- [ ] If audio glitched last time — restart the audio engine:
  `sudo killall coreaudiod` (sound drops for a second, then comes back).

---

## 1. Launch the app

```bash
cd "GPT real-time translator"
python3 web.py
```

- [ ] Browser opened `http://localhost:8000`.
- [ ] Status at the top reads: **"Ready. Press Start"**, grey dot.

---

## 2. Zoom setup (in Zoom itself, before the call)

Zoom → Settings → Audio:

- [ ] **Speaker → BlackHole 16ch** (the other participant's audio goes into the app).
- [ ] **Microphone → BlackHole 2ch** (your translation goes into Zoom).
- [ ] **Original Sound for Musicians: ON** (the call should show "Original Sound: On" at the top).
- [ ] Background noise suppression: **Low / Off** (otherwise it chops up the synthesized voice → "choppy").

> ⚠️ The most common cause of "choppy voice for the other participant" is Zoom's noise suppression being on. Check this one separately.

---

## 3. Start + device check

- [ ] **Engine: Gemini (single model)** — the default, primary option. The hybrid engine stays available as a fallback for comparison.
- [ ] The **"Other participant"** dropdown has the right language selected (for an English speaker — **English**, don't mix it up with another language!).
- [ ] Check the **"hear my own translation"** box (for a solo test).
- [ ] Press **Start**. Status → green pulsing dot, **"Running…"**.
- [ ] A device line appears at the bottom:
  `🎧 in: BlackHole 16ch · 🎙 mic: MacBook… · 📤 out: BlackHole 2ch`
  → **all three filled in, no "None"**. If any is None, the device wasn't found — see step 0.

---

## 4. Check DIRECTION A — "I hear the other participant in my language"

No second person needed, using the built-in test:

- [ ] In the left panel, press **🧪 Simulate English input**.
- [ ] Within 2–4s the **left panel** shows the **translated text** in your language.
  → Confirms the incoming direction works (Gemini or Qwen, per the selected engine): real English speech from the other participant will show up the same way.

---

## 5. Check DIRECTION B — "the other participant hears me in English"

- [ ] Say a short phrase into the microphone in your language, e.g.:
  *"Good afternoon, very glad to finally speak."*
- [ ] Within ~3s:
  - the **right panel** shows the English text;
  - your **headphones** (since "hear my own translation" is on) play an **English voice** — **smooth, no breakup**.
- [ ] Choppy voice? → recheck step 2 (Original Sound / noise suppression) and use wired headphones.

---

## 6. Go / No-Go

| Check | Expected |
|---|---|
| Devices at the bottom | all three filled in, no None |
| 🧪 test (direction A) | translated text on the left |
| Voice (direction B) | English voice in headphones, smooth |
| Latency | ~3s is normal for this technology |

✅ All four pass — **you're clear to call**.
❌ Any fail — fix it first, don't get on with the client yet.

---

## 7. Right before the call

- [ ] **Uncheck** "hear my own translation" if it's distracting (during the real call you already hear the other participant directly; the monitor is only for the solo test).
- [ ] Keep this window next to Zoom — read the other participant on the left, keep an eye on what they hear on the right.
- [ ] Watch the timer/cost at the top to track API spend in real time.

---

### One-line quick check
**killall coreaudiod → web.py → Zoom(16ch/2ch/Original Sound) → Start → devices with no None → 🧪 left panel shows translated text → say a phrase → right panel shows English text + smooth voice → make the call.**
