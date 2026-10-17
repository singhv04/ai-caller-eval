# High-level design

## 1. Purpose and scope

VoiceEval evaluates recorded phone calls between a **human** and an **AI voice agent**, against the 57
metrics of the *Voice AI Real-World Evaluation* framework
([docs/reference/voice_ai_evaluation_framework.pdf](reference/voice_ai_evaluation_framework.pdf)).

Design goals:

| Goal | How it is met |
|---|---|
| Exact attribution of who spoke when | Dual-channel recordings: one party per channel, analysed separately |
| Still useful with what teams actually have | Mono fallback via automatic two-speaker separation (flagged as approximate) |
| Any subset of calls × any subset of metrics | Run = recordings × metrics. Readiness check before running |
| Honest results | Every result carries a method and evidence. Empty results carry a reason category |
| Vendor-neutral | LLM judge, ASR, VAD, diarizer and storage are plug-ins |
| Scale out | Each recording is an independent work item claimed atomically from a shared DB |
| Cheap re-runs | ASR transcripts, VAD and LLM verdicts are cached per call |

Out of scope: placing calls, real-time monitoring of live calls, and collecting listener ratings
(ratings are imported as annotations).

---

## 2. System context

```text
                         ┌─────────────────────────────────────────┐
   QA / ops / eng  ────▶ │               VoiceEval                 │
   (browser or CLI)      │                                         │
                         │  Web UI ─▶ HTTP API ─▶ Engine ─▶ Metrics │
                         └───┬───────────────┬───────────────┬─────┘
                             │               │               │
          call recordings    │               │ transcripts   │ structured verdicts
          (+ optional logs,  │               │               │
          ratings, cost)     ▼               ▼               ▼
                    ┌────────────────┐ ┌──────────────┐ ┌──────────────────────────┐
                    │ Voice platform │ │ ASR          │ │ LLM provider (plug-in)   │
                    │ exports        │ │ faster-whisper│ │ Anthropic · OpenAI ·     │
                    │ (Twilio, Vapi, │ │ (local) or   │ │ Gemini · OpenAI-compatible│
                    │  Retell, …)    │ │ sidecar JSON │ │ (OpenRouter, Ollama, …)  │
                    └────────────────┘ └──────────────┘ └──────────────────────────┘
```

---

## 3. Component architecture

```text
 ┌──────────────────────────────── Presentation ────────────────────────────────┐
 │  web/ (SPA: Calls · Evaluate · Report · Call view · Settings)    voiceeval/cli│
 └───────────────────────────────────────┬──────────────────────────────────────┘
                                         │ JSON over HTTP
 ┌───────────────────────────────────────▼──────────────────────────────────────┐
 │  API layer  (voiceeval/api/main.py — FastAPI)                                │
 │  recordings · runs · readiness · metrics catalogue · profiles · LLM settings │
 └──────────┬───────────────────────┬──────────────────────────┬────────────────┘
            │                       │                          │
 ┌──────────▼─────────┐  ┌──────────▼───────────┐   ┌──────────▼──────────────┐
 │ Ingest             │  │ Run orchestration    │   │ Readiness               │
 │ engine/ingest.py   │  │ engine/service.py    │   │ engine/readiness.py     │
 │ normalise audio,   │  │ create run, claim    │   │ which metrics can run   │
 │ mono → diarize     │  │ items, finalize      │   │ on these calls, and why │
 └──────────┬─────────┘  └──────────┬───────────┘   └─────────────────────────┘
            │                       │ per recording
            │            ┌──────────▼───────────────────────────────────────────┐
            │            │ Evaluator  engine/evaluator.py                       │
            │            │ resolve metric deps → compute → band → aggregate     │
            │            └──────────┬───────────────────────────────────────────┘
            │                       │ lazy artifacts
            │            ┌──────────▼───────────────────────────────────────────┐
            │            │ CallContext  analysis/context.py                     │
            │            │ audio → VAD → timeline → transcript → LLM judge      │
            │            └───┬──────────┬──────────────┬──────────────┬─────────┘
            │                │          │              │              │
 ┌──────────▼──────┐ ┌───────▼──────┐ ┌─▼──────────┐ ┌─▼──────────┐ ┌─▼─────────────┐
 │ audio/          │ │ analysis/    │ │ transcript-│ │ judge/     │ │ metrics/      │
 │ loader, vad,    │ │ timeline     │ │ ion/       │ │ base +     │ │ 57 plug-in    │
 │ features,       │ │ (turns, gaps,│ │ sidecar,   │ │ providers  │ │ classes in 7  │
 │ diarize         │ │ barge-ins…)  │ │ whisper    │ │            │ │ evaluation    │
 └─────────────────┘ └──────────────┘ └────────────┘ └────────────┘ │ packs         │
                                                                    └───────────────┘
 ┌──────────────────────────────── Persistence ─────────────────────────────────┐
 │  db.py: SQLAlchemy (SQLite default / Postgres)   storage.py: blob store      │
 │  recordings · runs · run_items                   audio · transcripts · caches│
 └──────────────────────────────────────────────────────────────────────────────┘
```

| Component | Responsibility |
|---|---|
| Web UI | Guided flow. Never shows raw IDs or empty grids |
| API | Thin HTTP layer and input validation. Serves the UI |
| Ingest | Turns any audio layout into a canonical stereo WAV `[human, ai]` |
| Run orchestration | Run lifecycle, atomic work claiming, local or external executors |
| Evaluator | Metric dependency ordering, error isolation per metric, aggregation |
| CallContext | Computes expensive artifacts once, only if a selected metric needs them |
| Timeline | Turn-taking structure derived from two VAD tracks |
| Judge | One structured LLM request per call covering all selected LLM metrics |
| Metrics | Self-describing plug-ins: catalogue info, inputs, compute, aggregate |
| Profiles | Business context per agent: goal, policies, knowledge base, thresholds, gates |

---

## 4. Core data flow

```text
  upload ──▶ normalise ──▶ canonical stereo WAV ──▶ DB row (recording)
                                                         │
  user selects calls + checks + profile ─────────────────┤
                                                         ▼
                                              run  ─┬─ item (call 1)
                                                    ├─ item (call 2)      work queue
                                                    └─ item (call N)      (DB table)
                                                         │
                         any executor claims an item ◀───┘
                                     │
                                     ▼
   per call:  decode ─▶ VAD per channel ─▶ timeline ─▶ [transcript] ─▶ [LLM verdict]
                                     │                          (cached)     (cached)
                                     ▼
              compute selected metrics (+ dependencies) ─▶ results + evidence
                                     │
                                     ▼
              last item done ─▶ aggregate across calls ─▶ run summary ─▶ report
```

---

## 5. Key design decisions

| Decision | Rationale | Trade-off |
|---|---|---|
| Canonical stereo `[human, ai]` for every input | Downstream code has one shape. Mono and two-file uploads become the same thing | Mono overlaps can't be separated; flagged in UI |
| Timeline from per-channel VAD, not ASR | Fast, language-independent, exact on dual-channel | Energy VAD can be fooled by echo or noise; pluggable |
| One LLM call per recording | Cost and latency independent of the number of LLM metrics | One very large schema; mitigated by strict structured output |
| Metric = class with catalogue + `compute` + `aggregate` | New metric = one class. UI, API and CLI pick it up automatically | Some boilerplate per metric |
| Pooled aggregation (Σnum / Σden, pooled samples) | Correct rates and percentiles across calls of different length | Per-call means are available but not the headline |
| Result categories for "empty" | Users can tell "didn't happen" from "can't measure" | Every metric must classify its N/A reasons |
| Readiness before running | Avoids runs full of N/A | Readiness is a heuristic on metadata presence |
| DB-backed work queue | No Redis/Celery needed. Works on SQLite and Postgres | Polling (1 s) instead of push |
| Provider-neutral judge with native JSON mode | Teams use the LLM they already pay for | Verdicts can differ by model; cache key includes model |

---

## 6. Deployment and scaling

```text
 Single machine (default)                    Scaled out
 ─────────────────────────                   ─────────────────────────────────────────
 ┌───────────────────────┐                   ┌─────────┐   ┌─────────┐
 │ voiceeval serve       │                   │ API #1  │   │ API #2  │  VOICEEVAL_EXECUTOR=worker
 │  ├─ API + UI          │                   └────┬────┘   └────┬────┘
 │  └─ thread-pool       │                        └──────┬──────┘
 │     executor (N)      │                               ▼
 ├───────────────────────┤                 ┌──────────────────────────┐
 │ SQLite (WAL)  data/   │                 │ Postgres (runs, items)   │◀──┐ claim items
 │ blobs         data/   │                 └──────────────────────────┘   │ atomically
 └───────────────────────┘                 ┌──────────────────────────┐   │
                                           │ Shared blob storage      │◀──┤
                                           │ (NFS / S3 via Storage)   │   │
                                           └──────────────────────────┘   │
                                           ┌────────┐┌────────┐┌────────┐ │
                                           │worker 1││worker 2││worker N│─┘
                                           └────────┘└────────┘└────────┘
                                            python -m voiceeval worker --concurrency 4
```

- **Throughput:** scales linearly with workers. Per-call cost is dominated by ASR (about 1× real-time
  on CPU with `base.en`) and one LLM request.
- **Fault tolerance:** a failed metric yields an `error` result without failing the call. A failed
  call yields a failed item without failing the run. Items held by a dead worker are re-queued after
  `stale_minutes`.
- **Idempotency:** caches are keyed by content fingerprints (VAD config, transcript + profile +
  rubric + model), so retries don't re-pay for ASR or the LLM.

---

## 7. Security and privacy

| Concern | Handling |
|---|---|
| API keys | `.env` (git-ignored) or `data/llm_settings.json` (git-ignored, mode 600). The API only returns masked keys |
| Call audio and transcripts | Stored locally under `data/` by default. Sent to an LLM provider only when LLM checks run |
| Network exposure | Server binds to `127.0.0.1` by default. There is no auth layer; put it behind SSO/a proxy before exposing it |
| Data deletion | Deleting a recording removes its audio, caches and evaluation items; runs left empty are deleted |

---

## 8. Extension points

| Extend | Where | Contract |
|---|---|---|
| Metric | any module + `VOICEEVAL_PLUGINS` | subclass `Metric`, `@register`, implement `compute(ctx)` |
| LLM provider | `judge/providers.py` or plug-in | subclass `BaseJudge`, `@register_provider`, implement `complete_json` |
| ASR provider | `transcription/providers.py` | `transcribe_channel(x, sr, speaker) -> [Utterance]` |
| VAD | `audio/vad.py` `VAD_REGISTRY` | `detect(x, sr) -> [Segment]` |
| Diarizer (mono) | `audio/diarize.py` | `diarize_two_speakers(x, sr) -> {speakers, ai, stats}` |
| Blob storage | `storage.py` | `put_bytes / get_bytes / exists / local_path / delete_prefix` |
| Business rules | `config/profiles/*.yaml` | YAML; `extends:` another profile |

See [LOW_LEVEL_DESIGN.md](LOW_LEVEL_DESIGN.md) for module internals and algorithms.
