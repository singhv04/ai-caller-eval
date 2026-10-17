# Low-level design

Module internals, data model, algorithms and control flow. For the big picture see
[HIGH_LEVEL_DESIGN.md](HIGH_LEVEL_DESIGN.md).

## 1. Source map

```text
voiceeval/
├── config.py            Settings (env / .env) · Profile loading with `extends:` deep-merge
├── db.py                SQLAlchemy models: Recording, Run, RunItem · session_scope()
├── storage.py           Storage protocol + LocalStorage (atomic writes, path-traversal guard)
├── cli.py, __main__.py  serve · worker · ingest · run · metrics
├── api/main.py          FastAPI routes; serves web/
├── audio/
│   ├── loader.py        ffprobe/ffmpeg normalisation · mono→diarize · two-file merge · load_dual_channel
│   ├── vad.py           EnergyVAD (adaptive threshold + hysteresis) · VAD_REGISTRY
│   ├── features.py      clipping · dropouts · clicks · LUFS · F0 (autocorr) · DTW · syllables
│   └── diarize.py       mono 2-speaker separation (k-means + per-window Gaussian refinement)
├── analysis/
│   ├── timeline.py      turns, responses, barge-ins, AI talk-over, overlaps, premature endpoints, dead air
│   └── context.py       CallContext: lazy + cached per-call artifacts; judge orchestration
├── transcription/
│   ├── base.py          Word / Utterance / Transcript · sidecar parser (many shapes, speaker aliases)
│   └── providers.py     faster-whisper per channel · provider registry
├── judge/
│   ├── base.py          prompt, combined schema, BaseJudge, provider registry, config resolution, settings store
│   └── providers.py     Anthropic · OpenAI · OpenAI-compatible · Gemini
├── metrics/
│   ├── base.py          Metric, MetricResult, JudgeSection, MissingInput, NA kinds, REGISTRY
│   ├── __init__.py      imports all metric modules · PACKS (7 areas) · PRESETS
│   ├── latency.py conversational.py safety.py dialogue.py asr.py
│   └── speech_output.py prosody.py robustness.py operations.py      (57 metrics)
└── engine/
    ├── ingest.py        ingest_recording / ingest_file
    ├── service.py       create_run · claim_item · process_item · finalize_run · executors · worker_loop
    ├── evaluator.py     resolve_metrics · evaluate_recording · aggregate_results
    └── readiness.py     pre-run availability per metric
web/                     index.html · styles.css · app.js (hash router, no build step)
config/profiles/         default.yaml · outbound_appointment_setter.yaml
tests/                   test_platform.py (end-to-end) · test_judges.py (providers, mocked SDKs)
```

---

## 2. Data model

```text
 recordings                         runs                               run_items
 ─────────────────────────          ───────────────────────────        ─────────────────────────────
 id            PK                   id           PK                    id            PK
 name                               name                               run_id        FK → runs (cascade)
 original_filename                  profile                            recording_id  FK → recordings
 audio_key    → blob                metric_ids   JSON [ids]            status        pending|running|done|failed
 transcript_key → blob (optional)   options      JSON                  worker        who claimed it
 duration_s, sample_rate            status       pending|running|      error
 human_channel 0|1                               aggregating|done|     results       JSON {metric_id: MetricResult}
 ai_channel    0|1                               failed                analysis      JSON {timeline, turns, judge_meta…}
 tags          JSON {accent,…}      summary      JSON {metric_id:      started_at, finished_at
 meta          JSON {events,                     aggregated result}
               annotations, cost,   created_at, finished_at
               survey, channel_source,…}
 created_at

 recordings 1 ──── * run_items * ──── 1 runs
```

**Blob keys** (`storage.py`, default root `data/blobs/`):

```text
recordings/<rid>/audio.wav            canonical 16-bit stereo, channel 0 = human, 1 = AI (after mapping)
recordings/<rid>/original.<ext>       uploaded file
recordings/<rid>/transcript.json      sidecar transcript (if uploaded)
cache/<rid>/vad-<cfg hash>.json       per-channel speech segments
cache/<rid>/transcript.json           ASR output (both channels)
cache/<rid>/judge-<fingerprint>.json  LLM verdict; fingerprint = transcript+profile+rubrics+model+call facts
```

**MetricResult** (stored per call and in the run summary):

```text
metric_id, status (ok | not_applicable | error), value, unit, band (good | warn | bad | null),
method (measured | proxy | llm_judge | annotation | metadata | composite),
numerator, denominator, samples[], details{}, evidence[{start, end, turn?, note}],
reason, na_kind (not_occurred | needs_llm | needs_transcript | needs_data | needs_more_calls | needs_config)
```

---

## 3. Ingest

```text
 ingest_recording(stereo_path | human_path+ai_path, human_channel, transcript?, tags?, meta?)
   │
   ├─ insert Recording row (placeholder audio_key)            ← gives us an id for blob keys
   │
   ├─ stereo_path given?
   │     ├─ yes ─ ffprobe channels
   │     │        ├─ 1 ─▶ diarize_mono()  ─▶ virtual stereo [human, ai]; meta.channel_source = mono_diarized
   │     │        ├─ 2 ─▶ normalize_stereo(): ffmpeg → PCM16, ≤48 kHz; human_channel from user (L/R)
   │     │        └─ >2 ─▶ AudioError
   │     └─ no ── both mono files? ─▶ merge_mono_pair(): ffmpeg amerge → [human, ai]
   │
   ├─ transcript given? ─▶ parse_sidecar() (validate) ─▶ store JSON
   ├─ update row: audio_key, duration, sample_rate, channel mapping, meta
   └─ on any exception: delete row + blobs (no half-ingested recordings)
```

### 3.1 Mono diarization (`audio/diarize.py`)

```text
 mono signal x (resampled to 16 kHz)
   │
   ▼
 EnergyVAD(merge_gap 120 ms) ─▶ fine speech segments
   │
   ▼
 per segment features: [log median F0 ×2, spectral centroid, RMS dB ×0.5]  (z-scored)
   │
   ▼
 2-means clustering (init: lowest / highest pitch) ─▶ initial speaker label per segment
   │
   ▼
 refine_speakers():
   ├─ 200 ms windows (hop 100 ms): [log F0 (NaN if unvoiced), 24 mel bands − mean]
   ├─ fit diagonal Gaussian per speaker on windows of its labelled segments
   ├─ 2 EM-style passes: relabel windows by log-likelihood, refit
   ├─ per segment: smooth score over 5 windows; runs < 0.7 s absorbed
   └─ split segment at speaker changes; adjacent pieces share ONE exact boundary
       (padding would fabricate overlaps → fake barge-ins)
   │
   ▼
 unvoiced segments inherit the nearest labelled neighbour's speaker
   │
   ▼
 AI speaker = argmax( −loudness_sd + 4 × talk_share )        (TTS: steadier, talks more)
   │
   ▼
 mono_to_virtual_stereo(): each channel keeps only its speaker's segments (10 ms fades)
```

---

## 4. Run lifecycle

### 4.1 States

```text
 Run:      pending ──first item claimed──▶ running ──last item finished──▶ aggregating ──▶ done
                                                                                  └──▶ failed (no item done)
 RunItem:  pending ──claim (atomic UPDATE)──▶ running ──▶ done | failed
                ▲                                │
                └──── stale (> stale_minutes) ───┘   (worker died; re-queued)
```

### 4.2 Create → execute → finalize

```text
 POST /api/runs {recording_ids, metric_ids | preset, profile, options}
   │
   ├─ validate metrics (registry) + profile (file) + recordings (DB)
   ├─ INSERT run + one run_item per recording
   └─ executor.dispatch(run_id)
         ├─ LocalExecutor:  submit item ids to ThreadPoolExecutor(VOICEEVAL_LOCAL_WORKERS)
         └─ ExternalWorker: no-op (workers poll the table)

 process_item(item_id, worker)
   │
   ├─ claim:  UPDATE run_items SET status='running' WHERE id=? AND status='pending'
   │          rowcount == 1 ? continue : return            ← exactly-once across processes
   ├─ load recording, run.metric_ids, profile (+ options.profile_overrides)
   ├─ evaluate_recording() ─▶ results, analysis
   ├─ save item (done | failed + error)
   └─ maybe_finalize(run_id)
         ├─ any item pending/running? ─▶ return
         ├─ UPDATE runs SET status='aggregating' WHERE id=? AND status IN (pending, running)
         │   rowcount == 1 ? (this executor wins) : return
         └─ finalize_run(): aggregate_results() over done items ─▶ run.summary, status done/failed

 worker_loop(concurrency, poll_s)                       (python -m voiceeval worker)
   loop:
     re-queue items running longer than stale_minutes
     free slots? SELECT pending ids LIMIT 2×free ─▶ claim_item() each ─▶ pool.submit(process_item(claimed=True))
     sleep(poll_s)
```

`LocalExecutor.recover()` runs at API start-up: resets `running` items owned by local executors to
`pending`, re-dispatches pending runs, and finishes runs left in `aggregating`.

---

## 5. Evaluating one recording (`engine/evaluator.py`)

```text
 evaluate_recording(recording, metric_ids, profile, options)
   │
   ├─ ctx = CallContext(recording, profile, options)
   ├─ metrics = resolve_metrics(metric_ids)       DFS over metric.dependencies(profile),
   │                                              dependencies first, cycle detection
   │                                              (e.g. end_to_end_success_rate → its gate metrics)
   ├─ ctx.judge_sections = {m.id: m.judge_section(profile)  for LLM-capable metrics}
   │
   ├─ for m in metrics:                           (in dependency order)
   │     try:    r = m.with_band(m.compute(ctx), profile)
   │     except MissingInput e:  r = m.na(e.message, kind=e.kind)
   │     except Exception e:     r = error result (+ traceback in details)   ← isolated
   │     ctx.results[m.id] = r
   │
   └─ return {id: r.to_dict()}, ctx.analysis_summary()   (timeline, turns, transcript source, judge meta)
```

### 5.1 CallContext: lazy artifact graph

Each node is a `cached_property`. It is computed only when a metric touches it, and persisted where
it is expensive.

```text
                    recording.audio_key
                           │
                           ▼
                    audio (DualChannelAudio)            native-rate + 16 kHz copies per side
                           │
            ┌──────────────┼────────────────────┐
            ▼              │                    ▼
   vad {human, ai}         │             transcript                 sidecar JSON  ─┐
   [cache: vad-<cfg>]      │             [cache: transcript.json]   or ASR per     │
            │              │                    │                   channel ◀──────┘
            ▼              │                    ▼
   timeline (Timeline)     │             turn_texts = timeline turns + transcript text (±150 ms)
            │              │                    │
            └──────────────┼────────────┬───────┘
                           │            ▼
                           │      judge  [cache: judge-<fingerprint>, per-key lock]
                           │        one LLM request for all judge_sections
                           ▼            │
                   metric.compute(ctx) ◀┘   reads any of: audio · vad · timeline · transcript ·
                                            turn_texts · judge_for(id) · results(dep) · meta/tags
```

### 5.2 Timeline construction (`analysis/timeline.py`)

```text
 inputs: human segments, AI segments (from VAD), TimelineParams (profile `timeline:`)

 1. mark backchannels
      segment s (≤ backchannel_max_s) lying inside the OTHER party's continuing speech
      (other.start ≤ s.start+tol and other.end ≥ s.end+tol) ─▶ backchannel ("mm-hmm")

 2. build turns (non-backchannel segments, sorted)
      join s to the speaker's open turn if gap ≤ turn_gap_s AND the other party
      did not start a non-backchannel segment inside the gap; else start a new turn

 3. responses  (TAT samples)
      for each human turn H: first AI turn A after H, before the next human turn
      gap = A.start − H.end;  overlapped = A.start < H.end

 4. barge-ins  (per human TURN, not per segment)
      human turn starts while AI speech is active (±50 ms margin)
      stop = end of that AI speech run (gaps ≤ speech_run_gap_s merged)
      latency = stop − onset;  ignored if < min_overlap_s (VAD boundary noise)
      stopped = latency ≤ barge_in_max_yield_s;  ai_responded = AI speaks after the human ends

 5. AI talk-over
      AI turn starts while a non-backchannel human segment is active, overlap ≥ min_overlap_s

 6. overlaps  (pairwise segment intersections ≥ min_overlap_s)
      backchannel      → unwanted 0
      human-started    → unwanted = part beyond barge_in_yield_target_s
      AI-started       → unwanted = whole overlap

 7. premature endpoints
      AI turn starts after a user pause ≤ premature_max_pause_s AND the user resumes
      within premature_resume_window_s after AI onset

 8. dead air
      mutual silence ≥ dead_air_s between first and last speech
```

### 5.3 Banding

```text
 direction = lower:  v ≤ good → good ;  v ≥ bad → bad ;  else warn
 direction = higher: v ≥ good → good ;  v ≤ bad → bad ;  else warn
 direction = none:   no band (informational, e.g. articulation rate)
 thresholds: metric class defaults, overridden by profile.metric_overrides.<id>.{good,bad}
```

---

## 6. LLM judge (`judge/`)

### 6.1 Provider resolution

```text
 resolve_config()
   choice = data/llm_settings.json "provider"  ▸  env VOICEEVAL_JUDGE  ▸  "auto"
   │
   ├─ "none" ─▶ unavailable ("turned off")
   ├─ order = [choice]  or  auto: [anthropic, openai, gemini, openai_compatible]
   └─ for each provider:
        key      = settings api_key ▸ env (ANTHROPIC_API_KEY | OPENAI_API_KEY | GEMINI_API_KEY/GOOGLE_API_KEY
                                           | VOICEEVAL_JUDGE_API_KEY) ▸ Anthropic local login profile
        base_url = settings ▸ VOICEEVAL_JUDGE_BASE_URL            (compatible endpoints only)
        model    = settings ▸ VOICEEVAL_<PROVIDER>_MODEL ▸ VOICEEVAL_JUDGE_MODEL (if chosen) ▸ default
        effort   = settings ▸ VOICEEVAL_JUDGE_EFFORT (Anthropic)
        usable?  key │ login profile │ (key optional AND base_url) ─▶ JudgeConfig

 get_judge(): build once per config fingerprint (settings changes take effect without restart)
```

### 6.2 One request per call

```text
 sections = {metric_id: JudgeSection(instructions, schema)}       from selected LLM-capable metrics
   │
   ├─ system  = evaluator persona + evidence rules ("cite turn indexes; empty lists if N/A")
   ├─ user    = <business_context> agent role/goal/instructions, policies, knowledge base
   │            <call_metadata>    tags, scenario, expected_outcome
   │            <transcript>       [T7 41.2s-44.0s] AI: …   (timeline turns + text)
   │            <rubrics>          ## metric_id  instructions…
   └─ schema  = {type: object, properties: {metric_id: section schema}, required: all,
                 additionalProperties: false}                     (strict: every object closed)
   │
   ▼
 provider.complete_json(system, user, schema)
   ├─ Anthropic : messages.create(output_config.format = json_schema, effort), server-side refusal fallback
   ├─ OpenAI    : chat.completions.create(response_format = json_schema strict)
   │              └─ BadRequest on json_schema ─▶ retry with json_object + schema in prompt
   ├─ Compatible: same as OpenAI with base_url
   └─ Gemini    : models.generate_content(response_mime_type = application/json, response_json_schema)
   │
   ▼
 parse_json_output() (tolerates code fences) ─▶ {metric_id: section, _meta: provider/model/tokens}
   │
   ▼
 metric.compute(): ctx.judge_for(id) ─▶ count items, map turn indexes → time spans for evidence
 errors: auth / unknown model ─▶ JudgeUnavailable ─▶ MissingInput(needs_llm)
         rate limit / 5xx / truncation ─▶ RuntimeError ─▶ metric status error
```

---

## 7. Aggregation across calls

```text
 for each metric in the run:
   ok results = per-call results with status ok
   none ok ─▶ not_applicable, na_kind = most common per-call kind
   aggregation = ratio    ─▶ Σ numerator / Σ denominator × scale        (pooled, length-weighted)
               = samples  ─▶ summarize_samples(all per-call samples)    (e.g. TAT p50/p95 over all turns)
               = mean     ─▶ mean of per-call values                     (e.g. loudness SD)
               = custom   ─▶ metric.aggregate(results, contexts, profile)
                              noise_robustness_delta: pooled WER(noisy) − WER(clean) by tag
                              accent_performance_gap: max − min pooled WER per accent tag
                              cost_per_completed:     Σ cost / Σ completed (cost kept even if not completed)
                              availability_uptime:    run options ops.{available,scheduled}_minutes
   band with profile thresholds
```

---

## 8. Readiness (`engine/readiness.py`)

```text
 for each metric:
   batch metric (noise delta, accent gap, uptime)? ─▶ check tags / WER inputs ─▶ needs_more_calls | needs_data
   needs LLM and judge unavailable?               ─▶ needs_llm
   needs transcript, no ASR, not all have sidecar ─▶ needs_transcript | partial
   needs a meta path (DATA_NEEDS table)?          ─▶ count calls having it ─▶ ready | partial | needs_data
   else                                           ─▶ ready
```

The Evaluate page shows the result per area. With "only run checks that can produce a result" on,
locked metrics are left out of the run.

---

## 9. Metric plug-in contract (`metrics/base.py`)

```text
 class MyMetric(Metric):
   id, priority, name, category, question, value_range, formula, terminology   ← catalogue
   unit, scale (100 for %), direction, good, bad                               ← scoring
   requires / optional  (audio, transcript, llm_judge, annotations, event_log, metadata, multiple_recordings)
   scope (call | batch), aggregation (ratio | samples | mean | custom), depends_on
   judge_section(profile) -> JudgeSection | None                               ← optional LLM rubric
   compute(ctx) -> MetricResult                                                 ← required
   aggregate(results, contexts, profile) -> MetricResult                       ← optional

 helpers: self.ratio(num, den) · self.ok(value) · self.na(reason, kind) · raise MissingInput(msg, kind)
 register: @register (module imported in metrics/__init__.py or listed in VOICEEVAL_PLUGINS)
 every metric must belong to exactly one PACK (asserted at import)
```

---

## 10. HTTP API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | executor, judge status, ASR provider |
| GET | `/api/metrics` | catalogue (+ targets, warnings), presets, packs |
| POST | `/api/readiness` | `{recording_ids}` → per-metric status/detail/calls |
| GET | `/api/profiles`, `/api/profiles/{name}` | list / resolved profile |
| GET / PUT | `/api/settings/llm` | providers (masked keys, key source), selected provider; save |
| POST | `/api/settings/llm/test` | ping provider with structured output |
| POST | `/api/settings/llm/models` | list models available to a key |
| POST | `/api/recordings` | multipart: `file` (stereo/mono) or `human_file`+`ai_file`, `transcript`, `tags`, `meta`, `human_channel` |
| GET | `/api/recordings` | list + latest evaluation summary per call |
| GET / PATCH / DELETE | `/api/recordings/{id}` | read / edit (name, channel side, tags, meta) / delete |
| GET | `/api/recordings/{id}/audio` | canonical WAV |
| GET | `/api/recordings/{id}/preview` | timeline (+ transcript if available) without a run |
| POST | `/api/runs` | create run (`metric_ids` and/or `preset`, `profile`, `options`) |
| GET / DELETE | `/api/runs`, `/api/runs/{id}` | list / detail with items / delete |
| GET | `/api/runs/{id}/export.csv`, `/export.json` | exports |

---

## 11. Configuration reference

| Variable | Default | Meaning |
|---|---|---|
| `VOICEEVAL_DATA_DIR` | `./data` | DB, blobs, caches, saved LLM settings |
| `DATABASE_URL` | SQLite in data dir | e.g. `postgresql+psycopg://…` |
| `VOICEEVAL_EXECUTOR` | `local` | `local` thread pool, or `worker` for external workers |
| `VOICEEVAL_LOCAL_WORKERS` | `4` | threads for local executor |
| `VOICEEVAL_TRANSCRIBER` | `auto` | `auto` · `sidecar` · `faster_whisper` · `none` |
| `VOICEEVAL_WHISPER_MODEL` | `base.en` | faster-whisper model |
| `VOICEEVAL_JUDGE` | `auto` | `auto` · `anthropic` · `openai` · `gemini` · `openai_compatible` · `none` |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `GEMINI_API_KEY` | – | provider keys |
| `VOICEEVAL_<PROVIDER>_MODEL` | provider default | model per provider |
| `VOICEEVAL_JUDGE_BASE_URL`, `VOICEEVAL_JUDGE_API_KEY` | – | OpenAI-compatible endpoint |
| `VOICEEVAL_JUDGE_EFFORT` | `medium` | Anthropic effort |
| `VOICEEVAL_PLUGINS` | – | comma-separated modules to import (extra metrics/providers) |

Profile keys (`config/profiles/*.yaml`): `agent`, `knowledge_base`, `policies`
(`required_disclosures`, `escalation_triggers`, `protected_data`, `safety_policy`), `dialogue`,
`timeline`, `audio.vad`, `speech`, `gates`, `metric_overrides`, `extends`.
