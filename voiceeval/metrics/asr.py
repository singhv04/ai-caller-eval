"""Speech-recognition metrics (framework priorities 20-24).

These evaluate how well the *AI system* heard the human. The human channel
is the reference audio; what the AI's own ASR produced must be supplied in
`meta.ai_asr_transcript` (from the voice platform's logs). The reference text is
a human annotation (`annotations.reference_transcript`) if present, otherwise
our own transcript of the isolated human channel (reported as a proxy).
"""
from __future__ import annotations

import re

from .base import REQ_AUDIO, REQ_JUDGE, REQ_METADATA, REQ_TRANSCRIPT, JudgeSection, Metric, register
from .util import BOOL, STR, TURN, arr, ev, obj


def normalize_text(text: str) -> list[str]:
    text = text.lower().replace("’", "'")
    text = re.sub(r"[^a-z0-9' ]+", " ", text)
    return text.split()


def edit_ops(ref: list, hyp: list) -> dict:
    """Levenshtein alignment returning substitution / deletion / insertion counts."""
    n, m = len(ref), len(hyp)
    D = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        D[i][0] = i
    for j in range(m + 1):
        D[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            D[i][j] = min(D[i - 1][j] + 1, D[i][j - 1] + 1, D[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]))
    i, j, S, Dl, I = n, m, 0, 0, 0
    while i > 0 or j > 0:
        if i > 0 and j > 0 and D[i][j] == D[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            S += ref[i - 1] != hyp[j - 1]
            i, j = i - 1, j - 1
        elif i > 0 and D[i][j] == D[i - 1][j] + 1:
            Dl += 1
            i -= 1
        else:
            I += 1
            j -= 1
    return {"S": S, "D": Dl, "I": I, "N": n}


def _hyp_text(ctx) -> str | None:
    h = ctx.meta.get("ai_asr_transcript")
    if h is None:
        return None
    if isinstance(h, list):
        return " ".join(u.get("text", "") if isinstance(u, dict) else str(u) for u in h)
    return str(h)


def _ref_text(ctx) -> tuple[str | None, str]:
    ref = ctx.annotations.get("reference_transcript")
    if ref:
        return ref, "annotation"
    tr = ctx.transcript
    if tr is None:
        return None, ""
    return " ".join(u.text for u in tr.side("human")), "proxy"


class _ErrorRate(Metric):
    category = "Speech recognition"
    direction = "lower"
    requires = (REQ_METADATA,)
    optional = (REQ_TRANSCRIPT,)
    char_level = False

    def compute(self, ctx):
        hyp = _hyp_text(ctx)
        if hyp is None:
            return self.na("meta.ai_asr_transcript (what the AI's ASR heard) not provided")
        ref, method = _ref_text(ctx)
        if not ref:
            return self.na("No reference: add annotations.reference_transcript or enable transcription")
        if self.char_level:
            r, h = list("".join(normalize_text(ref))), list("".join(normalize_text(hyp)))
        else:
            r, h = normalize_text(ref), normalize_text(hyp)
        ops = edit_ops(r, h)
        return self.ratio(ops["S"] + ops["D"] + ops["I"], ops["N"], method=method, details=ops)


@register
class WordErrorRate(_ErrorRate):
    id = "word_error_rate"
    priority = 20
    name = "Word error rate (WER)"
    question = "How many words AI mishears."
    value_range = "≥0%, can exceed 100%; <10% on clean speech is a provisional target; compare accents/noise."
    formula = "(S + D + I) / N × 100"
    terminology = "S substitutions, D deletions, I insertions, N reference words (human channel)."
    good, bad = 10, 25


@register
class CharacterErrorRate(_ErrorRate):
    id = "character_error_rate"
    priority = 22
    name = "Character error rate (CER)"
    question = "How accurately AI hears names and languages without spaces."
    value_range = "≥0%, can exceed 100%; benchmark per language."
    formula = "(S_c + D_c + I_c) / N_c × 100"
    terminology = "Character-level edits over reference characters (spaces removed)."
    good, bad = 5, 15
    char_level = True


@register
class EntityRecognition(Metric):
    id = "entity_recognition_accuracy"
    priority = 21
    name = "Entity recognition accuracy"
    category = "Speech recognition"
    question = "Does AI catch important details correctly?"
    value_range = "0–100%; aim ≥98% for critical fields with confirmation."
    formula = "Correctly captured entities / Total entities × 100"
    terminology = "Names, dates, numbers, addresses, IDs. Uses annotations.entities if present, else the LLM judge compares what the caller said with how the AI used/read it back."
    direction = "higher"
    good, bad = 98, 90
    requires = (REQ_TRANSCRIPT, REQ_JUDGE)

    def judge_section(self, profile):
        return JudgeSection(
            "List every critical entity the caller provided (names, dates, times, numbers, amounts, addresses, IDs, "
            "emails). Compare what the caller said with what the AI captured — as shown by read-backs, confirmations or "
            "how it used the value later. If the AI never reflected the value back, judge from its subsequent actions.",
            obj({"entities": arr({"turn": TURN, "entity_type": STR, "caller_value": STR, "ai_value": STR, "correct": BOOL})}),
        )

    def compute(self, ctx):
        ann = ctx.annotations.get("entities")
        if ann and "correct" in ann:
            return self.ratio(ann["correct"], ann["total"], method="annotation")
        items = ctx.judge_for(self.id)["entities"]
        return self.ratio(sum(i["correct"] for i in items), len(items), method="llm_judge",
                          evidence=[{**ctx.turn_span(i["turn"]), "note": f"{i['entity_type']}: said '{i['caller_value']}' → AI '{i['ai_value']}'"}
                                    for i in items if not i["correct"]])


@register
class SpeechDetectionRecall(Metric):
    id = "speech_detection_recall"
    priority = 23
    name = "Speech detection recall"
    category = "Speech recognition"
    question = "How often AI notices when someone speaks."
    value_range = "0–100%; aim ≥98% in target conditions."
    formula = "User speech segments the AI reacted to / All user speech segments × 100"
    terminology = "Annotation if supplied (annotations.speech_segments); otherwise proxy: human turns followed by an AI response or yield."
    direction = "higher"
    good, bad = 98, 90

    def compute(self, ctx):
        ann = ctx.annotations.get("speech_segments")
        if ann and "detected" in ann:
            return self.ratio(ann["detected"], ann["total"], method="annotation")
        tl = ctx.timeline
        humans = tl.turns_of("human")
        if humans and tl.turns[-1].speaker == "human":
            humans = humans[:-1]  # the final goodbye has no chance of a reply
        answered = {r["human_turn"] for r in tl.responses}
        yielded = {b["onset"] for b in tl.barge_ins if b["stopped"]}
        missed = [t for t in humans if t.index not in answered and not any(t.start <= y <= t.end for y in yielded)]
        return self.ratio(len(humans) - len(missed), len(humans), method="proxy",
                          evidence=[ev(t.start, t.end, "no AI reaction to this user turn") for t in missed])


@register
class FalseSpeechActivation(Metric):
    id = "false_speech_activation_rate"
    scale = 1.0
    priority = 24
    name = "False speech activation rate"
    category = "Speech recognition"
    question = "How often background noise wakes the AI."
    value_range = "≥0 per hour; lower better; calibrate to environment."
    formula = "False speech triggers / Audio hours"
    terminology = "Proxy: human-channel activity with no transcribed words that made the AI stop or start talking."
    unit = "/hr"
    direction = "lower"
    good, bad = 1, 5
    requires = (REQ_AUDIO, REQ_TRANSCRIPT)

    def compute(self, ctx):
        tr = ctx.require_transcript()
        tl = ctx.timeline
        triggers = []
        for s in tl.segments["human"]:
            if tr.text_in("human", s.start, s.end):
                continue
            reacted = any(abs(b["onset"] - s.start) < 0.05 and b["stopped"] for b in tl.barge_ins) or \
                any(t.speaker == "ai" and 0 <= t.start - s.end <= 1.0 for t in tl.turns)
            if reacted:
                triggers.append(s)
        hours = tl.duration / 3600
        return self.ratio(len(triggers), hours, method="proxy",
                          evidence=[ev(s.start, s.end, "non-speech sound triggered the AI") for s in triggers])
