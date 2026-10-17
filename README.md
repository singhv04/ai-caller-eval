# VoiceEval: evaluate AI caller recordings

VoiceEval scores recorded phone calls between a **person** and an **AI voice agent** against the 57
metrics of the *Voice AI Real-World Evaluation* framework
([PDF](docs/reference/voice_ai_evaluation_framework.pdf)). Upload calls, choose what to check, and get
a report that leads with plain-English findings. Each finding links to the exact moment in the audio.

```text
 Calls ──▶ Evaluate ──▶ Report ──▶ Call view
 upload     pick calls,   issues, to watch,   two-lane channel timeline,
 recordings check areas,  on target, how to   audio, transcript, evidence
            profile       unlock the rest
```

**What it checks:** responsiveness and turn-taking, compliance and safety, conversation quality,
voice and audio quality, speech recognition, listener ratings, and reliability/business.

**How it works:**
- **Dual-channel recordings** (caller on one channel, AI on the other) give exact timing for
  latency, interruptions and talk-over.
- **Mono recordings** are split into the two speakers automatically and flagged as approximate.
- **Any LLM** can judge conversation quality: Anthropic Claude, OpenAI, Google Gemini, or any
  OpenAI-compatible API (OpenRouter, Groq, Azure, Ollama, …).
- **Pluggable and scalable:** add metrics, LLM/ASR providers and storage backends as plug-ins, and
  run any number of workers against Postgres.

---

## Quick start

Requirements: Python 3.10+, `ffmpeg` on the PATH.

```bash
git clone https://github.com/singhv04/ai-caller-eval.git && cd ai-caller-eval
python3 -m venv .venv
.venv/bin/pip install -e ".[llm,asr,dev]"     # llm = Anthropic/OpenAI/Gemini SDKs, asr = local Whisper

cp .env.example .env                           # add ONE LLM key (or do it later in the UI → Settings)
.venv/bin/python -m voiceeval serve            # open http://127.0.0.1:8000
```

In the UI:
1. **Calls:** drop recordings. They can be stereo (one person per channel), mono, or two separate
   files via *Upload options*.
2. Select calls → **Evaluate →**. Choose the areas to check; each shows how many checks can run on
   these calls. Then pick the profile describing what the AI caller should do.
3. **Report:** key findings first, then results by area. Anything that couldn't be measured is listed
   with what it needs.
4. Click any finding or value to open the **call view**. Clicking evidence plays that moment.

Try it on the included recordings:

```bash
.venv/bin/python -m voiceeval ingest samples/real-calls/
.venv/bin/python -m voiceeval run --profile outbound_appointment_setter --metrics preset:core --wait
```

---

## Connecting an LLM

Conversation-quality, compliance and safety checks need an LLM. Put a key in `.env` (restart the
server afterwards) or use **Settings** in the UI (applies immediately, with *Test connection* and
*Load models*).

| Provider | `.env` key | Model setting (default) |
|---|---|---|
| Anthropic Claude | `ANTHROPIC_API_KEY` | `VOICEEVAL_ANTHROPIC_MODEL` (`claude-opus-5-5`) |
| OpenAI (ChatGPT) | `OPENAI_API_KEY` | `VOICEEVAL_OPENAI_MODEL` (`gpt-5`) |
| Google Gemini | `GEMINI_API_KEY` | `VOICEEVAL_GEMINI_MODEL` (`gemini-2.5-pro`) |
| OpenAI-compatible | `VOICEEVAL_JUDGE_BASE_URL` (+ `VOICEEVAL_JUDGE_API_KEY`) | `VOICEEVAL_OPENAI_COMPATIBLE_MODEL` |

`VOICEEVAL_JUDGE=auto` uses the first provider that has a key. All selected LLM checks share **one
structured request per call**, and verdicts are cached.

> 🔒 `.env` and `data/` (which holds keys saved from Settings) are git-ignored. Never commit keys.

---

## Getting meaningful results

- **Prefer dual-channel recordings.** Most voice platforms can record each party on its own channel
  (e.g. Twilio `RecordingChannels=dual`).
- **Describe each agent in a profile** (`config/profiles/*.yaml`):
  - its goal and instructions
  - required disclosures
  - escalation rules
  - pass/fail gates
  - a **knowledge base** of facts it may state. Without one, every specific claim (price,
    availability) is unverifiable and the hallucination rate looks very high.
- **Attach platform data to unlock more metrics:** event logs (time to first audio, endpointing),
  the agent's own ASR transcript (word error rate), listener ratings, survey and cost. The formats
  are in [docs/LOW_LEVEL_DESIGN.md](docs/LOW_LEVEL_DESIGN.md#2-data-model). Edit them per call via
  *Open → Settings → Metadata*.

---

## Documentation

| Doc | Contents |
|---|---|
| [docs/USER_FLOW.md](docs/USER_FLOW.md) | Screen-by-screen flow: calls → evaluate → report → call view, settings |
| [docs/HIGH_LEVEL_DESIGN.md](docs/HIGH_LEVEL_DESIGN.md) | Goals, system context, components, data flow, design decisions, scaling, security |
| [docs/LOW_LEVEL_DESIGN.md](docs/LOW_LEVEL_DESIGN.md) | Source map, data model, ingest/diarization, run lifecycle, timeline algorithm, LLM judge, aggregation, API, config |
| [docs/METRICS.md](docs/METRICS.md) | All 57 metrics by area: question, inputs, target, measurement method |
| [samples/README.md](samples/README.md) | Included recordings and how to generate synthetic test calls |

---

## Repository layout

```text
voiceeval/          Python package (API, engine, audio, analysis, transcription, judge, metrics)
web/                Single-page UI (no build step)
config/profiles/    Evaluation profiles (agent goal, policies, knowledge base, thresholds, gates)
docs/               Design docs, metric catalogue, framework PDF
samples/real-calls/ Two real outbound AI-caller recordings (mono)
scripts/            Synthetic dual-channel demo-call generator (macOS `say`)
tests/              End-to-end and provider tests (no network needed)
```

## CLI

```bash
python -m voiceeval serve [--port 8000]                    # API + UI
python -m voiceeval ingest <files or folders>              # add recordings
python -m voiceeval run --recordings <ids|all> --metrics <ids|preset:NAME|all> --profile NAME --wait
python -m voiceeval metrics [--markdown]                   # list metrics and presets
python -m voiceeval worker --concurrency 4                 # extra worker (VOICEEVAL_EXECUTOR=worker)
```

## Development

```bash
.venv/bin/python -m pytest -q          # 10 tests: timeline, all metrics, judge schema, providers (mocked), API flow
.venv/bin/ruff check voiceeval tests   # lint
```

To add a metric, subclass `Metric` and decorate it with `@register`; add an LLM provider by
subclassing `BaseJudge` and decorating it with `@register_provider`. See
[LOW_LEVEL_DESIGN.md §9](docs/LOW_LEVEL_DESIGN.md#9-metric-plug-in-contract-metricsbasepy) and
[HIGH_LEVEL_DESIGN.md §8](docs/HIGH_LEVEL_DESIGN.md#8-extension-points).

> **macOS note:** if the `voiceeval` console script says `No module named 'voiceeval'`, the venv's
> `.pth` file has been flagged hidden (common in iCloud-synced folders). Use `python -m voiceeval`
> from the repo root instead.
