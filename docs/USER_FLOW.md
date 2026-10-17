# User flow

How someone uses VoiceEval end to end: add calls, choose what to check, read the report, drill into a call.

```text
 ┌──────────────┐     ┌──────────────────┐     ┌──────────────────┐     ┌──────────────────┐
 │  1. CALLS    │────▶│  2. EVALUATE     │────▶│  3. REPORT       │────▶│  4. CALL VIEW    │
 │              │     │                  │     │                  │     │                  │
 │ upload audio │     │ pick calls       │     │ scoreboard       │     │ channel timeline │
 │ see status   │     │ pick check areas │     │ key findings     │     │ audio + transcript│
 │ select calls │     │ pick profile     │     │ results by area  │     │ evidence → audio │
 └──────────────┘     └──────────────────┘     │ how to unlock    │     └──────────────────┘
        ▲                                      └────────┬─────────┘
        │              ┌──────────────────┐             │
        └──────────────│  SETTINGS        │◀────────────┘  (unlock LLM checks)
                       │ LLM provider/key │
                       └──────────────────┘
```

---

## 1. Calls — add and select recordings

```text
                 drop / browse audio file(s)
                            │
                            ▼
                ┌───────────────────────┐
                │ how many channels?    │
                └───────────┬───────────┘
          2 (stereo)        │ 1 (mono)          two separate files
      ┌─────────────────────┼─────────────────────────┐
      ▼                     ▼                         ▼
 human side = L/R    automatic speaker          merged: caller=L, AI=R
 (upload option)     separation → virtual        "Dual-channel"
 "Dual-channel"      stereo, AI voice guessed
                     "Mono · auto-split"
      └─────────────────────┬─────────────────────────┘
                            ▼
              call appears in the list (selected)
                            │
             ┌──────────────┴──────────────┐
             ▼                             ▼
     tick calls → "Evaluate →"      "Open" → preview timeline, transcript,
                                     edit name / tags / metadata,
                                     swap caller/AI side, delete
```

What the list shows for each call: length, audio type (dual-channel / mono auto-split), tags, and the
result of its latest evaluation (issues / to watch / ok) with a link to it.

---

## 2. Evaluate — three choices

```text
 Step 1  Calls ............ toggle chips for any uploaded call
            │
            ▼
 Step 2  What to check .... 7 areas (cards). Each card shows a readiness bar:
            │                 ██████████░░░  "8 of 11 checks can run · 3 need data"
            │                 Ready │ Some calls │ Needs LLM key │ Needs data │ Needs more calls
            │               "Show checks" lists the individual metrics and why any are locked.
            │               [✓] Only run checks that can produce a result (default)
            ▼
 Step 3  Profile .......... what the AI caller is supposed to do (goal, disclosures,
            │               escalation rules, knowledge base, pass/fail targets)
            ▼
         Run evaluation ──▶ report page (live progress while running)
```

The seven areas:

| Area | What it answers |
|---|---|
| Responsiveness & turn-taking | How fast does the AI reply? Does it talk over people? Does it stop when interrupted? |
| Compliance & safety | Did it make the required disclosures? Leak data? Say anything unsafe? Hand off when it should? |
| Conversation quality | Did the call achieve its goal? Accurate, understood, remembered context, recovered from mistakes? |
| Voice & audio quality | Glitches, dropouts, clipping, volume jumps, speaking pace, pauses |
| Speech recognition | Did the AI hear the caller correctly (key details, noise triggers, word errors)? |
| Listener ratings | Human listener scores (naturalness, human-likeness, prosody, pronunciation, A/B) |
| Reliability & business | Broken calls, recovery after outages, accent/noise robustness, CSAT, cost |

---

## 3. Report — read results

```text
 ┌─────────────────────────────────────────────────────────────────────────┐
 │  Issues: 7     To watch: 8     On target: 15     Not measured: 19 (why?) │
 ├─────────────────────────────────────────────────────────────────────────┤
 │  KEY FINDINGS (issues first, then watch)                                │
 │   ! Consent / disclosure compliance: 33% · target ≥ 100%                │
 │     Flagged in 2 of 2 calls. e.g. real-estate at 0:03 — ai_identity:    │
 │     MISSING                                              [Inspect]      │
 │   ~ Turnaround time: 1.21 s · target ≤ 800 ms ...                       │
 ├─────────────────────────────────────────────────────────────────────────┤
 │  RESULTS BY AREA                                                        │
 │   metric · question │ value + target │ status │ per-call values (click) │
 │   "Didn't occur" rows for situations that never happened in the calls   │
 ├─────────────────────────────────────────────────────────────────────────┤
 │  NOT MEASURED — AND HOW TO UNLOCK                                       │
 │   🔒 Needs an LLM ............ connect a provider in Settings           │
 │   📎 Needs extra data ......... event logs, ratings, survey, cost       │
 │   ➕ Needs more calls ......... e.g. clean vs noisy, ≥2 accents         │
 │   ○  Available but not selected                                         │
 └─────────────────────────────────────────────────────────────────────────┘
```

Every empty result is classified, never shown as a bare "N/A":

| Kind | Meaning | Shown as |
|---|---|---|
| `not_occurred` | The situation never happened (e.g. nobody interrupted) | "Didn't occur" row |
| `needs_llm` | No LLM provider configured | Unlock section |
| `needs_transcript` | No transcript and no ASR | Unlock section |
| `needs_data` | Metadata/annotations/event logs missing | Unlock section |
| `needs_more_calls` | Batch metric needs more or differently tagged calls | Unlock section |
| `needs_config` | Profile lacks the policy/gate | Unlock section |

---

## 4. Call view — hear exactly where each issue happens

Every flagged check (Issue / Watch) contributes **moments**: exact timestamps in the call where the
problem occurs. A missing disclosure is anchored to the AI's opening turns, where it should have been
said. Problems that apply to the whole call (e.g. "never confirmed the date") are listed without a time.

```text
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ CALLER  ▮▮  ▮      ▮▮▮        ▮       ▮▮                                    │
 │ AI        ▮▮▮▮▮ ▮▮▮    ▮▮▮▮▮▮   ▮▮▮▮▮▮▮  ▮▮▮                                  │
 │ ISSUES  ▬   ▬▬     ▬      ▬▬▬    ▬   ▬     ← one marker per moment (red =    │
 │           ▬            ▬                      issue, amber = watch); stacked  │
 │                                               when they overlap. Click = play │
 │ 0:00          0:30          1:00          1:30                               │
 │ ▶ ───────●─────────────────────────────────────                              │
 │ ⏮ ↺ ⏭   Turnaround time · #2 of 5 · moment 4 of 14     Context [±4 s ▾]      │
 │          0:42–0:45 · playing 0:38–0:49 · Caller waited 2.50 s for the reply  │
 ├───────────────────────────────────────┬────────────────────────────────────┤
 │ ISSUE MOMENTS (14)  [By issue|By time]│ TRANSCRIPT                         │
 │ ● Turnaround time  1.38 s   5 moments │  CALLER 0:02  Hello.               │
 │   ▶ 0:09  #1 of 5  Caller waited 2.5 s│  AI     0:03  Hi, this is …        │
 │   ▶ 0:42  #2 of 5  …                  │  (line highlights while playing)   │
 │ ● Consent / disclosure  33%  1 moment │                                    │
 │   ▶ 0:03  ai identity: MISSING — …    │                                    │
 │ ALL RESULTS (expand for formula,      │                                    │
 │ every evidence item, details)         │                                    │
 └───────────────────────────────────────┴────────────────────────────────────┘
```

How to listen:

| Action | Result |
|---|---|
| ▶ next to a moment, or click a marker on the ISSUES lane | Plays from *context* seconds before the moment to *context* seconds after, then stops. The exact moment is highlighted on the timeline, the surrounding context is shaded |
| ⏮ / ⏭ (or ← / → keys) | Previous / next issue moment in call order, across all checks |
| ↺ (or R) | Replay the current moment |
| Context ±2 / 4 / 8 / 15 s | How much surrounding audio to include |
| *By issue* / *By time* | Moments grouped per check (#1 of 5, #2 of 5 …), or as one chronological list |
| Click a transcript line | Play from that line |

On the **report**, each key finding lists its moments across all calls as ▶ time chips (e.g.
`▶ medical 0:09`). A chip opens that call and plays that moment immediately.

---

## Settings — connect an LLM

```text
 Automatic ── first provider that has a key: Anthropic → OpenAI → Gemini → compatible
 Anthropic Claude ┐
 OpenAI (ChatGPT) ├─ paste key · choose model (Load models) · Test connection · Save & use
 Google Gemini    │
 OpenAI-compatible┘─ + base URL (OpenRouter, Groq, Azure, Ollama, vLLM …)
 Off ──────────── skip LLM-judged checks
```

Keys can also be set in `.env` (restart required). Keys saved in Settings are stored locally in
`data/llm_settings.json` (file mode 600). Neither location is committed to git.
