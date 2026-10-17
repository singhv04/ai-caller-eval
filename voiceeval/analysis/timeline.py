"""Conversation timeline built from per-channel speech segments.

With one channel per party, turn-taking structure is derived exactly from two
VAD tracks: who held the floor, response gaps, barge-ins, talk-over, dead air.
All timing metrics read from this one structure.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..audio.vad import Segment

OTHER = {"human": "ai", "ai": "human"}


@dataclass
class TLSegment:
    speaker: str
    start: float
    end: float
    backchannel: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class Turn:
    index: int
    speaker: str
    start: float
    end: float
    segments: list[TLSegment] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class TimelineParams:
    turn_gap_s: float = 1.0           # same-speaker silence that still belongs to one turn
    backchannel_max_s: float = 0.8    # short utterance inside the other party's speech
    backchannel_tolerance_s: float = 0.25
    speech_run_gap_s: float = 0.35    # gaps inside one stretch of AI speech
    barge_in_yield_target_s: float = 0.5   # provisional target from the framework (<500 ms)
    barge_in_max_yield_s: float = 2.0      # beyond this the AI "did not stop"
    max_response_gap_s: float = 2.0        # human->AI handoff slower than this is inappropriate
    premature_resume_window_s: float = 1.0
    premature_max_pause_s: float = 1.2
    dead_air_s: float = 6.0
    min_overlap_s: float = 0.12       # overlaps shorter than this are VAD-boundary noise, not interruptions

    @staticmethod
    def from_profile(cfg: dict | None) -> "TimelineParams":
        p = TimelineParams()
        for k, v in (cfg or {}).items():
            if hasattr(p, k):
                setattr(p, k, float(v))
        return p


def _active_at(segs: list[TLSegment], t: float, margin: float = 0.0) -> TLSegment | None:
    for s in segs:
        if s.start + margin < t < s.end - margin:
            return s
    return None


class Timeline:
    def __init__(self, human: list[Segment], ai: list[Segment], duration: float, params: TimelineParams):
        self.p = params
        self.duration = duration
        self.segments = {
            "human": [TLSegment("human", s.start, s.end) for s in human],
            "ai": [TLSegment("ai", s.start, s.end) for s in ai],
        }
        self._mark_backchannels()
        self.turns = self._build_turns()
        self.responses = self._responses()
        self.barge_ins = self._barge_ins()
        self.ai_interruptions = self._ai_interruptions()
        self.overlaps = self._overlaps()
        self.premature_endpoints = self._premature_endpoints()
        self.dead_air = self._dead_air()

    # ---- construction ----------------------------------------------------
    def _mark_backchannels(self) -> None:
        tol = self.p.backchannel_tolerance_s
        for spk in ("human", "ai"):
            for s in self.segments[spk]:
                if s.duration > self.p.backchannel_max_s:
                    continue
                # fully inside the other party's continuing speech => acknowledgement, not a floor grab
                for o in self.segments[OTHER[spk]]:
                    if o.start <= s.start + tol and o.end >= s.end + tol:
                        s.backchannel = True
                        break

    def _build_turns(self) -> list[Turn]:
        main = sorted([s for spk in ("human", "ai") for s in self.segments[spk] if not s.backchannel],
                      key=lambda s: s.start)
        turns: list[Turn] = []
        open_turn: dict[str, Turn | None] = {"human": None, "ai": None}
        for s in main:
            cur = open_turn[s.speaker]
            other_cut = any(
                o.start > (cur.end if cur else 0) and o.start < s.start
                for o in self.segments[OTHER[s.speaker]] if not o.backchannel
            ) if cur else True
            if cur and not other_cut and s.start - cur.end <= self.p.turn_gap_s:
                cur.end = max(cur.end, s.end)
                cur.segments.append(s)
            else:
                t = Turn(0, s.speaker, s.start, s.end, [s])
                turns.append(t)
                open_turn[s.speaker] = t
        turns.sort(key=lambda t: t.start)
        for i, t in enumerate(turns):
            t.index = i
        return turns

    def speech_run_end(self, speaker: str, t: float) -> float:
        """End of the continuous stretch of `speaker` speech that contains time t."""
        segs = [s for s in self.segments[speaker] if s.end > t]
        end = None
        for s in segs:
            if end is None:
                if s.start > t:
                    return t
                end = s.end
            elif s.start - end <= self.p.speech_run_gap_s:
                end = max(end, s.end)
            else:
                break
        return end if end is not None else t

    def _responses(self) -> list[dict]:
        """Human turn end -> AI turn start (the TAT samples)."""
        out = []
        for i, h in enumerate(self.turns):
            if h.speaker != "human":
                continue
            nxt_h = next((t for t in self.turns[i + 1:] if t.speaker == "human"), None)
            limit = nxt_h.start if nxt_h else float("inf")
            a = next((t for t in self.turns[i + 1:] if t.speaker == "ai" and t.start < limit), None)
            if a is None:
                continue
            out.append({
                "human_turn": h.index, "ai_turn": a.index, "human_end": round(h.end, 3),
                "ai_start": round(a.start, 3), "gap_ms": round((a.start - h.end) * 1000, 1),
                "overlapped": a.start < h.end,
            })
        return out

    def _barge_ins(self) -> list[dict]:
        out = []
        for s in self.turns_of("human"):  # one interruption per human turn, however many pauses it has
            a = _active_at(self.segments["ai"], s.start, margin=0.05)
            if a is None:
                continue
            stop = self.speech_run_end("ai", s.start)
            latency = stop - s.start
            if latency < self.p.min_overlap_s:  # back-to-back handoff, not an interruption
                continue
            out.append({
                "onset": round(s.start, 3), "ai_stop": round(stop, 3), "stop_latency_ms": round(latency * 1000, 1),
                "stopped": latency <= self.p.barge_in_max_yield_s,
                "within_target": latency <= self.p.barge_in_yield_target_s,
                "human_end": round(s.end, 3),
            })
        # AI resumes after the human finishes?
        for b in out:
            b["ai_responded"] = any(t.speaker == "ai" and t.start >= b["human_end"] - 0.2 for t in self.turns)
        return out

    def _ai_interruptions(self) -> list[dict]:
        out = []
        for t in self.turns:
            if t.speaker != "ai":
                continue
            h = _active_at(self.segments["human"], t.start, margin=0.05)
            if h is not None and not h.backchannel and h.end - t.start >= self.p.min_overlap_s:
                out.append({"ai_turn": t.index, "onset": round(t.start, 3), "human_segment_end": round(h.end, 3),
                            "overlap_ms": round((min(h.end, t.end) - t.start) * 1000, 1)})
        return out

    def _overlaps(self) -> list[dict]:
        out = []
        for h in self.segments["human"]:
            for a in self.segments["ai"]:
                s, e = max(h.start, a.start), min(h.end, a.end)
                if e - s < self.p.min_overlap_s:
                    continue
                if h.backchannel or a.backchannel:
                    kind, unwanted = "backchannel", 0.0
                elif h.start >= a.start:  # human barged in on AI
                    kind = "barge_in"
                    unwanted = max(0.0, e - max(s, h.start + self.p.barge_in_yield_target_s))
                else:  # AI started while human was talking
                    kind, unwanted = "ai_talk_over", e - s
                out.append({"start": round(s, 3), "end": round(e, 3), "kind": kind,
                            "unwanted_s": round(unwanted, 3)})
        return sorted(out, key=lambda o: o["start"])

    def _premature_endpoints(self) -> list[dict]:
        """AI took the floor inside a short user pause and the user carried on talking."""
        out = []
        for t in self.turns:
            if t.speaker != "ai":
                continue
            prev_h = [s for s in self.segments["human"] if not s.backchannel and s.end <= t.start + 0.05]
            if not prev_h:
                continue
            pause = t.start - prev_h[-1].end
            if pause > self.p.premature_max_pause_s:
                continue
            resumed = [s for s in self.segments["human"] if not s.backchannel
                       and t.start - 0.05 <= s.start <= t.start + self.p.premature_resume_window_s]
            if resumed:
                out.append({"ai_turn": t.index, "ai_onset": round(t.start, 3), "user_pause_ms": round(pause * 1000, 1),
                            "user_resumed_at": round(resumed[0].start, 3)})
        return out

    def _dead_air(self) -> list[dict]:
        allsegs = sorted(self.segments["human"] + self.segments["ai"], key=lambda s: s.start)
        out, cursor = [], None
        for s in allsegs:
            if cursor is not None and s.start - cursor >= self.p.dead_air_s:
                out.append({"start": round(cursor, 3), "end": round(s.start, 3), "duration_s": round(s.start - cursor, 2)})
            cursor = s.end if cursor is None else max(cursor, s.end)
        return out

    # ---- helpers ---------------------------------------------------------
    def turns_of(self, speaker: str) -> list[Turn]:
        return [t for t in self.turns if t.speaker == speaker]

    @property
    def conversation_span(self) -> tuple[float, float]:
        allsegs = self.segments["human"] + self.segments["ai"]
        if not allsegs:
            return 0.0, 0.0
        return min(s.start for s in allsegs), max(s.end for s in allsegs)

    def speech_time(self, speaker: str) -> float:
        return sum(s.duration for s in self.segments[speaker])

    def to_dict(self) -> dict:
        return {
            "duration": round(self.duration, 3),
            "segments": {k: [asdict(s) for s in v] for k, v in self.segments.items()},
            "turns": [{"index": t.index, "speaker": t.speaker, "start": round(t.start, 3), "end": round(t.end, 3)}
                      for t in self.turns],
            "responses": self.responses,
            "barge_ins": self.barge_ins,
            "ai_interruptions": self.ai_interruptions,
            "overlaps": self.overlaps,
            "premature_endpoints": self.premature_endpoints,
            "dead_air": self.dead_air,
        }
