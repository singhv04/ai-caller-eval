# Metric catalogue

All 57 metrics of the evaluation framework ([PDF](reference/voice_ai_evaluation_framework.pdf)), grouped the way the UI groups them.
`#` is the framework's priority. **Needs** lists the inputs required beyond the recording; **Target** is the default "good" threshold, which a profile can override.

Inputs: `audio` = the recording only · `transcript` = sidecar or local Whisper · `LLM` = any configured LLM provider · `data` = metadata from your voice platform (see the README) · `batch` = needs several tagged calls.

## ⏱ Responsiveness & turn-taking

How fast the AI replies, whether it talks over people, and how it handles being interrupted.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 9 | **Turnaround time (TAT)**<br>`turnaround_time` | How long the user waits after finishing before AI starts speaking. | audio | ≤ 800 ms | t(first audible AI speech) − t(user speech end). Measured per human→AI handoff from the two channels; includes endpointing, reasoning and synthesis. |
| 13 | **Latency jitter**<br>`latency_jitter` | Whether response speed feels consistent. | audio | ≤ 250 ms | SD(TAT)  (also reports p95(TAT) − p50(TAT)). SD(TAT)  (also reports p95(TAT) − p50(TAT)) |
| 17 | **Turn-taking appropriateness**<br>`turn_taking_appropriateness` | Does it know when to speak and when to listen? | audio | ≥ 90% | Appropriate handoffs / Total handoffs × 100. Automated proxy for the human rating: a human→AI handoff is appropriate when the AI waits for the user to finish and replies within the max response gap; a barge-in is appropriate when the AI yields within the max-yield window. |
| 19 | **Unwanted overlap rate (turns)**<br>`unwanted_overlap_turns` | How often AI talks over the user. | audio | ≤ 2% | Unwanted overlapping turns / Total turns × 100. AI turns that start while the human channel is mid-utterance; cooperative backchannels excluded. |
| 18 | **Unwanted overlap rate (talk time)**<br>`unwanted_overlap_time` | Does it talk over you unnecessarily? | audio | ≤ 1% | Unwanted overlapping talk time / Total conversation time × 100. Unwanted = AI starting over the user, plus AI speech continuing past the barge-in yield target after the user interrupts. Backchannels excluded. |
| 12 | **Premature endpoint rate**<br>`premature_endpoint_rate` | How often AI assumes the person is done while they are still speaking. | audio | ≤ 2% | Early endpoints / True user turns × 100. Proxy: the AI starts talking inside a short user pause and the user resumes within the resume window — i.e. the AI split an unfinished utterance. |
| 14 | **Barge-in stop latency**<br>`barge_in_stop_latency` | How quickly AI stops talking when interrupted. | audio | ≤ 500 ms | t(AI audio stops) − t(user interruption onset). Onset = human-channel speech starting while the AI channel is active (backchannels excluded); stop = end of that stretch of AI speech. |
| 16 | **Interruption handling success**<br>`interruption_handling_success` | Whether users can interrupt naturally. | audio | ≥ 95% | Correctly handled interruptions / All valid interruptions × 100. Audio proxy: AI stops within the max-yield window AND speaks again after the user finishes. Context retention after interruption is covered by `context_retention_accuracy`. |
| 10 | **Time to first audio (TTFA)**<br>`time_to_first_audio` | How quickly generated audio starts arriving. | data (event log) — Agent event log with tts_request / first_audio_packet timestamps | ≤ 500 ms | t(first audio packet) − t(request accepted). Needs client-side events `tts_request` and `first_audio_packet` in meta.events. |
| 11 | **End-of-turn detection delay**<br>`end_of_turn_detection_delay` | How long AI waits to recognize the user has stopped. | audio, data (event log) — Agent event log with endpoint timestamps | ≤ 300 ms | t(endpoint decision) − t(actual user speech end). Actual speech end from the human channel; endpoint decisions from `endpoint` events. |
| 15 | **Barge-in detection latency**<br>`barge_in_detection_latency` | How fast AI notices someone interrupting it. | audio, data (event log) — Agent event log with barge_in_detected timestamps | ≤ 300 ms | t(interruption detected) − t(user interruption onset). Onset from the human channel; detection time from `barge_in_detected` events. |

## 🛡 Compliance & safety

Required disclosures (AI identity, recording), data leaks, unsafe replies and handing off to a human.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 3 | **Consent / disclosure compliance**<br>`consent_disclosure_compliance` | Does it tell people necessary information? | transcript (+LLM) | ≥ 100% | Correct disclosures / Disclosure-required items × 100. Required disclosures (AI identity, recording consent, …) from `policies.required_disclosures`. |
| 1 | **Sensitive data leakage rate**<br>`sensitive_data_leakage_rate` | Does it reveal private information? | transcript (+LLM) | ≤ 0% | Disclosures of protected data / Privacy-relevant exchanges × 100. Protected data defined by `policies.protected_data` in the profile. Regex PII scan always runs. |
| 2 | **Unsafe response rate**<br>`unsafe_response_rate` | How often the AI gives unsafe replies. | transcript, LLM | ≤ 0% | Policy-violating AI responses / AI responses × 100. Safety policy from `policies.safety_policy` in the profile. |
| 4 | **Escalation / handoff success**<br>`escalation_handoff_success` | Does it transfer to a person when it should? | transcript, LLM | ≥ 95% | Successful human handoffs / Required handoff scenarios × 100. Triggers from `policies.escalation_triggers`; `transfer` events in meta.events confirm a handoff. |

## 💬 Conversation quality

Did the call achieve its goal? Accuracy, understanding, memory, repetition and recovery from mistakes.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 5 | **End-to-end successful call rate**<br>`end_to_end_success_rate` | Did the entire experience work well? | audio, transcript, LLM | ≥ 80% | Calls meeting task, quality, latency and safety gates / All calls × 100. Gates are configured in the profile (`gates:`). A call succeeds only if every evaluated gate passes. Unevaluable gates are skipped, fail the call (`strict`), or make the call N/A (`required`). |
| 6 | **Task completion rate**<br>`task_completion_rate` | Can users actually get things done? | transcript, LLM | ≥ 85% | Successfully completed tasks / Attempted tasks × 100. Each caller goal is a task; success must satisfy the scenario's objective (meta.expected_outcome if given). |
| 7 | **Hallucination rate**<br>`hallucination_rate` | How often it confidently says made-up things. | transcript, LLM | ≤ 2% | Unsupported factual claims / All evaluated factual claims × 100. Claims checked against the profile knowledge base (authoritative ground truth) and the call itself. |
| 8 | **Instruction adherence**<br>`instruction_adherence` | Does it follow what the user asked? | transcript, LLM | ≥ 95% | Compliant responses / Instruction-bearing turns × 100. Covers user instructions (format, language, scope, pace) and operator instructions from the profile. |
| 25 | **Intent understanding accuracy**<br>`intent_understanding_accuracy` | Does it understand what users want? | transcript, LLM | ≥ 95% | Correctly identified intents / Annotated intents × 100. LLM judge labels each user intent and compares with what the AI acted on (human labels can override via annotations.intents). |
| 26 | **Context retention accuracy**<br>`context_retention_accuracy` | Does it remember what was said earlier in the call? | transcript, LLM | ≥ 95% | Correct context-dependent answers / Context probes × 100. Probes = AI turns that require recalling earlier information (names, numbers, prior answers, state after interruption). |
| 27 | **Contextual response quality**<br>`contextual_response_quality` | Does it respond like someone following the conversation? | transcript, LLM | ≥ 4 | Mean rubric rating (1–5). LLM judge rates every AI turn for relevance, coherence, continuity and conversational fit. |
| 28 | **Clarification appropriateness**<br>`clarification_appropriateness` | Does it ask sensible follow-up questions? | transcript, LLM | ≥ 90% | Correct clarification decisions / Ambiguous or clear cases × 100. Correct = asks when the request is ambiguous, does not ask when it is already clear. |
| 29 | **Repair success rate**<br>`repair_success_rate` | Can it recover gracefully when it gets something wrong? | transcript, LLM | ≥ 90% | Successful recoveries / Misunderstanding or error events × 100. Recovery must fix the issue within `dialogue.max_repair_turns` turns (profile). |
| 31 | **Repetition / redundancy rate**<br>`repetition_rate` | Does AI keep repeating itself? | transcript (+LLM) | ≤ 5% | Unnecessary repeated AI turns / AI turns × 100. LLM judge labels redundancy; without a judge, near-duplicate AI turns (word Jaccard ≥ 0.75) are counted. |
| 30 | **Turns to resolution**<br>`turns_to_resolution` | How many exchanges it takes to solve a problem. | audio, transcript, LLM | — | Total user + AI turns in completed sessions / Completed sessions. Turns from the channel timeline; completion from task_completion_rate outcome. |
| 32 | **Conversation abandonment rate**<br>`conversation_abandonment_rate` | How often people give up before finishing. | transcript, LLM | ≤ 5% | User-abandoned sessions / Started sessions × 100. Per-call flag aggregated across selected recordings; intentional quick exits excluded. |

## 🔊 Voice & audio quality

How the AI sounds: glitches, dropouts, clipping, volume jumps, speaking pace and pausing.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 34 | **Synthesis artifact rate**<br>`synthesis_artifact_rate` | How often generated speech sounds broken. | audio | ≤ 1% | AI clips with audible glitches / All AI clips × 100. Clip = one AI turn. Glitch = impulsive click/pop or abrupt digital dropout detected in the AI channel. |
| 35 | **Audio dropout rate**<br>`audio_dropout_rate` | How often the voice cuts out. | audio | ≤ 0.1 /min | Missing or corrupted playback intervals / Playback minutes. Abrupt near-digital-silence gaps (15–250 ms) cutting through AI speech. |
| 36 | **Clipping rate**<br>`clipping_rate` | Does the voice distort at loud moments? | audio | ≤ 0.01% | Clipped audio samples / Total samples × 100. Clipped = consecutive samples at digital full scale, measured over AI speech. |
| 38 | **Loudness consistency**<br>`loudness_consistency` | Does volume jump unexpectedly? | audio | ≤ 2 LU | SD(integrated loudness per AI turn, LUFS). ITU-R BS.1770 integrated loudness per AI turn ≥0.4 s. |
| 39 | **Speaking-rate deviation**<br>`speaking_rate_deviation` | Does it speak too fast or too slowly? | audio, transcript | ≤ 0.5 syl/s | abs(AI syllables/sec − reference syllables/sec). Reference = profile speech.reference_rate_sps if set, otherwise the human caller's own rate in this call. |
| 45 | **Articulation rate**<br>`articulation_rate` | How fast the voice speaks while actually talking. | audio, transcript | — | Spoken syllables / Speaking time excluding silence. Syllables from the AI transcript; speaking time = voiced time on the AI channel (pauses excluded). |
| 46 | **Pause duration**<br>`pause_duration` | Are pauses comfortably long, not awkwardly short or long? | audio | — | Σ pause durations / number of pauses. Pauses = silences ≥ threshold (default 200 ms) inside AI turns, from the AI channel VAD. |
| 44 | **Pause placement F1**<br>`pause_placement_f1` | Does it pause where a person would naturally pause? | transcript | ≥ 0.85 | 2PR/(P+R); P=TP/(TP+FP); R=TP/(TP+FN). Proxy: expected pauses = punctuation (clause/sentence) boundaries in the AI text; detected pauses = inter-word gaps ≥ pause threshold from word timestamps. annotations.pause_placement={tp,fp,fn} overrides. |
| 41 | **Intonation contour similarity**<br>`intonation_contour_similarity` | Does pitch rise and fall at sensible points in the sentence? | audio, data (ratings) — Human reference readings of AI lines | ≤ 0.5 cost | DTW(z(F0_AI), z(F0_human)) / L. annotations.prosody_references = [{start, end, reference_path}] — AI clip in this call and a human reading of the same text. Best (minimum) cost across references per clip. |

## 👂 Speech recognition

How well the AI heard the caller: word errors, key details (names, dates, numbers), noise triggers.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 21 | **Entity recognition accuracy**<br>`entity_recognition_accuracy` | Does AI catch important details correctly? | transcript, LLM | ≥ 98% | Correctly captured entities / Total entities × 100. Names, dates, numbers, addresses, IDs. Uses annotations.entities if present, else the LLM judge compares what the caller said with how the AI used/read it back. |
| 23 | **Speech detection recall**<br>`speech_detection_recall` | How often AI notices when someone speaks. | audio | ≥ 98% | User speech segments the AI reacted to / All user speech segments × 100. Annotation if supplied (annotations.speech_segments); otherwise proxy: human turns followed by an AI response or yield. |
| 24 | **False speech activation rate**<br>`false_speech_activation_rate` | How often background noise wakes the AI. | audio, transcript | ≤ 1 /hr | False speech triggers / Audio hours. Proxy: human-channel activity with no transcribed words that made the AI stop or start talking. |
| 20 | **Word error rate (WER)**<br>`word_error_rate` | How many words AI mishears. | data (+transcript) — What the AI's own speech recognizer heard (from the agent's logs) | ≤ 10% | (S + D + I) / N × 100. S substitutions, D deletions, I insertions, N reference words (human channel). |
| 22 | **Character error rate (CER)**<br>`character_error_rate` | How accurately AI hears names and languages without spaces. | data (+transcript) — What the AI's own speech recognizer heard (from the agent's logs) | ≤ 5% | (S_c + D_c + I_c) / N_c × 100. Character-level edits over reference characters (spaces removed). |

## ⭐ Listener ratings

Scores from human listeners: naturalness, human-likeness, prosody, pronunciation, A/B preference.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 52 | **Overall voice naturalness MOS**<br>`overall_naturalness_mos` | Does the voice feel natural overall? | data (ratings) — Listener ratings (1–5) | ≥ 4 | Σ overall naturalness ratings / Number of ratings. Blind listeners score complete clips/interactions; keep scale and listening setup consistent. |
| 40 | **Prosody naturalness MOS**<br>`prosody_naturalness_mos` | Overall, does the *way* it speaks sound human? | data (ratings) — Listener ratings (1–5) | ≥ 4 | Σ listener prosody ratings / Number of ratings. Blind listeners rate pitch, stress, rhythm and pauses together on a 1–5 scale. |
| 54 | **Human-likeness rating**<br>`human_likeness_rating` | How much does it sound like a person rather than a machine? | data (ratings) — Listener ratings (1–5) | ≥ 4 | Σ human-likeness ratings / Number of ratings. Blind 1–5 rating of perceived human-likeness, distinct from usefulness or correctness. |
| 37 | **Voice consistency**<br>`voice_consistency` | Does it keep sounding like the same speaker? | data (ratings) — Listener ratings (1–5) | ≥ 4 | Mean listener consistency rating (1–5). Same voice identity, accent and timbre across turns; blind ratings. |
| 43 | **Expressive appropriateness**<br>`expressive_appropriateness` | Does it sound cheerful, calm, serious, or empathetic at the right time? | data (ratings) — Listener ratings (1–5) | ≥ 4 | Mean listener rating (1–5). Listeners rate whether emotional delivery matches the context. |
| 33 | **Pronunciation accuracy**<br>`pronunciation_accuracy` | Does it pronounce names and words correctly? | data (ratings) — Annotator pronunciation error counts | ≥ 0.97 | 1 − (Pronunciation errors / Evaluated words). annotations.pronunciation = {errors, evaluated_words} from independent annotators. |
| 42 | **Emphasis / stress accuracy**<br>`emphasis_stress_accuracy` | Does the voice emphasize the important word? | data (ratings) — Annotated emphasis targets | ≥ 85% | Correct prominent targets / Annotated targets × 100. annotations.emphasis = {correct, total}; listener-verified prominence placement. |
| 53 | **A/B preference win rate**<br>`ab_preference_win_rate` | Which of two voices do people actually prefer? | data (ratings) — Blind A/B listening results vs. a baseline voice | ≥ 55% | AI-A preferred / Non-tied A/B judgments × 100. annotations.ab_preference = {a_wins, b_wins, ties}; this recording is system A vs a baseline. |

## 📈 Reliability & business

Dropped or broken calls, recovery after outages, robustness across accents/noise, CSAT and cost.

| # | Metric | Question | Needs | Target | How it is measured |
|---|---|---|---|---|---|
| 50 | **Session failure rate**<br>`session_failure_rate` | How often calls break entirely. | audio (+data) | ≤ 1% | Crashed or unrecoverable sessions / All sessions × 100. meta.end_reason when provided (error/dropped/timeout/...); otherwise proxy: the call ends on an unanswered user turn followed by long silence (frozen agent). |
| 51 | **Recovery after network interruption**<br>`network_interruption_recovery` | Does it recover from connection problems? | audio | ≥ 95% | Successfully resumed sessions / Interrupted sessions × 100. Interruptions from `network_interruption` events, or mid-call dead air on both channels; resumed = both parties speak again afterwards. |
| 49 | **Multilingual / code-switch accuracy**<br>`multilingual_accuracy` | Can it handle users switching languages? | transcript, LLM | ≥ 90% | Correct multilingual turns / Tested multilingual turns × 100. Switches within sentences and across turns. |
| 47 | **Noise robustness delta**<br>`noise_robustness_delta` | How much background noise hurts understanding. | data, batch | ≤ 2 pp | WER(noisy) − WER(clean). Recordings tagged noise_condition=clean / noisy; pooled WER per group; percentage-point gap. |
| 48 | **Accent performance gap**<br>`accent_performance_gap` | Does it work equally well across accents? | data, batch | ≤ 3 pp | max(group error rate) − min(group error rate). Recordings tagged accent=<group>; pooled WER per group. |
| 55 | **User satisfaction (CSAT)**<br>`csat` | Did users like the interaction? | data — Post-call survey score | ≥ 80% | Positive survey responses / All valid survey responses × 100. meta.survey = {score, scale} (top-two box is positive) or {positive, total}. |
| 57 | **Cost per completed conversation**<br>`cost_per_completed_conversation` | How much does a successful call cost? | data, LLM — Per-call cost (ASR + LLM + TTS + telephony) | — | Total voice system cost / Successfully completed conversations. meta.cost (number or {total}) incl. ASR, LLM, TTS, infra, telephony; completion from task_completion_rate. |
| 56 | **Availability / uptime**<br>`availability_uptime` | Is the voice assistant reliably available? | data, batch | ≥ 99.9% | Available service minutes / Scheduled service minutes × 100. Service-level, not recording-level: pass run options ops.available_minutes / ops.scheduled_minutes. |

## Presets (CLI)

`python -m voiceeval run --metrics preset:<name>`

- `responsiveness` — Responsiveness & turn-taking
- `compliance` — Compliance & safety
- `conversation` — Conversation quality
- `voice` — Voice & audio quality
- `listening` — Speech recognition
- `human_ratings` — Listener ratings
- `reliability` — Reliability & business
- `audio_only` — Audio-only (no LLM, no transcript)
- `core` — Core scorecard
