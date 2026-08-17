"""
podcast.py — cutting interviews and podcasts on SPEAKER TURNS instead of the clock.

The problem this solves
-----------------------
In a two-person show the meaningful unit is a question and the answer it gets. A
selector that picks free timestamps has no idea where an answer ends, so it stops
part-way into the next question — the clip trails off into someone changing the
subject. Sentence boundaries do not fix this: a new question is a new sentence.

The fix is structural, not a better prompt. Deepgram's diarisation labels every
word with a speaker, so we can build real turns, assemble candidate clips that are
whole by construction (a question plus its complete answer), and then ask the model
to CHOOSE among those candidates rather than invent boundaries. A wrong pick costs
one mediocre clip; it can no longer cost a clip that stops mid-answer.

What it produces
----------------
  build_turns()      consecutive same-speaker segments -> turns
  identify_roles()   which speaker is the host, which is the guest
  build_units()      candidate clips: Q&A pairs and standalone guest moments
  rank_units()       the model scores the candidates; heuristics if it is unavailable
  select()           the entry point select_clips.py calls
"""

import json
import re

import providers

# A turn shorter than this is back-channel ("haan", "right", "exactly"), not a real
# turn — it must not split an answer in two.
BACKCHANNEL_MAX_SEC = 1.6
BACKCHANNEL_MAX_WORDS = 4

# How many candidates to show the model. Enough to choose well, small enough that a
# long podcast still fits one call.
MAX_CANDIDATES = 60

# Sentence-ending punctuation, Latin + Devanagari.
_ENDS = (".", "!", "?", "।", "॥", "…")
_Q_MARKS = ("?", "？")

# Hindi/English question words — a question mark is often missing from ASR output,
# so wording has to carry the signal too.
_Q_WORDS = (
    "kya", "kyu", "kyun", "kaise", "kaisa", "kaisi", "kab", "kahan", "kaun", "kitna",
    "kitne", "kyon", "batao", "bataiye", "what", "why", "how", "when", "where", "who",
    "which", "tell me", "do you", "did you", "are you", "have you", "can you",
    "क्या", "क्यों", "कैसे", "कब", "कहाँ", "कहां", "कौन", "कितना", "कितने", "बताइए", "बताओ",
)


def _ends_sentence(text):
    text = (text or "").strip()
    return bool(text) and text[-1] in _ENDS


def _is_question(text):
    """Does this turn read as a question? Punctuation first, then question words."""
    t = (text or "").strip()
    if not t:
        return False
    if t[-1] in _Q_MARKS:
        return True
    low = t.lower()
    # Only the opening clause matters: "I asked what he thought" is not a question.
    head = low[:90]
    return any(w in head for w in _Q_WORDS)


# ─────────────────────────────────────────────────────────────
# Turns
# ─────────────────────────────────────────────────────────────

def build_turns(segments, log=None):
    """Group consecutive same-speaker segments into turns.

    Short interjections are absorbed into the surrounding turn: a host saying "haan"
    while the guest talks is not a turn change, and treating it as one would chop
    every long answer into fragments.
    """
    labelled = [s for s in (segments or []) if s.get("speaker") is not None]
    if not labelled:
        return []

    turns = []
    for seg in segments:
        spk = seg.get("speaker")
        if spk is None:
            # Unlabelled segment: attach to the current turn rather than dropping it.
            if turns:
                turns[-1]["segs"].append(seg)
                turns[-1]["end"] = seg.get("end", turns[-1]["end"])
            continue
        if turns and turns[-1]["speaker"] == spk:
            turns[-1]["segs"].append(seg)
            turns[-1]["end"] = seg.get("end", turns[-1]["end"])
        else:
            turns.append({"speaker": spk, "start": seg.get("start", 0.0),
                          "end": seg.get("end", 0.0), "segs": [seg]})

    # Absorb back-channel turns into their neighbour.
    merged = []
    for t in turns:
        words = sum(len((s.get("text") or "").split()) for s in t["segs"])
        dur = t["end"] - t["start"]
        tiny = dur <= BACKCHANNEL_MAX_SEC and words <= BACKCHANNEL_MAX_WORDS
        if tiny and merged:
            merged[-1]["segs"].extend(t["segs"])
            merged[-1]["end"] = t["end"]
            continue
        merged.append(t)

    # Re-merge neighbours that are now the same speaker (a back-channel removal can
    # leave A | A adjacent), then finalise text.
    out = []
    for t in merged:
        if out and out[-1]["speaker"] == t["speaker"]:
            out[-1]["segs"].extend(t["segs"])
            out[-1]["end"] = t["end"]
        else:
            out.append(t)
    for t in out:
        t["text"] = " ".join((s.get("text") or "").strip() for s in t["segs"]).strip()
        t["dur"] = round(t["end"] - t["start"], 3)

    if log:
        log.log(f"  Speaker turns: {len(out)} from {len(segments)} segment(s)")
    return out


# ─────────────────────────────────────────────────────────────
# Who is the host?
# ─────────────────────────────────────────────────────────────

def identify_roles(turns, log=None):
    """Return {"host": id, "guest": id}.

    The host asks; the guest explains. That shows up as a higher question rate and
    much shorter turns, and it is far more reliable than talk time alone — a chatty
    host would break a pure talk-time rule.
    """
    if not turns:
        return {}
    speakers = sorted({t["speaker"] for t in turns})
    if len(speakers) == 1:
        return {"host": None, "guest": speakers[0]}

    stats = {}
    for spk in speakers:
        mine = [t for t in turns if t["speaker"] == spk]
        total = sum(t["dur"] for t in mine)
        qs = sum(1 for t in mine if _is_question(t["text"]))
        stats[spk] = {
            "turns": len(mine),
            "talk": total,
            "avg": total / max(1, len(mine)),
            "q_rate": qs / max(1, len(mine)),
        }

    # Score how host-like each speaker is: asks a lot, says little per turn.
    def hostness(spk):
        s = stats[spk]
        longest_avg = max(stats[x]["avg"] for x in speakers) or 1.0
        return s["q_rate"] * 2.0 + (1.0 - s["avg"] / longest_avg)

    ranked = sorted(speakers, key=hostness, reverse=True)
    host, guest = ranked[0], ranked[1]
    # A "host" who dominates the talk time is probably the guest after all.
    if stats[host]["talk"] > stats[guest]["talk"] * 1.6 and stats[host]["q_rate"] < 0.2:
        host, guest = guest, host

    if log:
        for spk in speakers:
            s = stats[spk]
            role = "HOST" if spk == host else ("GUEST" if spk == guest else "other")
            log.log(f"    speaker {spk}: {role}  turns={s['turns']} talk={s['talk']:.0f}s "
                    f"avg={s['avg']:.1f}s questions={s['q_rate']*100:.0f}%")
    return {"host": host, "guest": guest}


# ─────────────────────────────────────────────────────────────
# Candidate clips
# ─────────────────────────────────────────────────────────────

def _trim_to_sentences(segs, budget, from_start=True):
    """Take whole segments up to `budget` seconds, never splitting one.

    Returns (start, end) or None. Preferring a sentence end is the point: the clip
    stops where a thought stops.
    """
    if not segs:
        return None
    if from_start:
        base = segs[0].get("start", 0.0)
        best = None
        for s in segs:
            if s.get("end", 0.0) - base > budget:
                break
            if _ends_sentence(s.get("text", "")):
                best = s.get("end")
        if best is None:
            # No punctuated end fits — take whole segments anyway rather than nothing.
            for s in segs:
                if s.get("end", 0.0) - base > budget:
                    break
                best = s.get("end")
        return (base, best) if best and best > base else None
    # Trim from the END backwards (used to keep the payoff when an answer is long).
    end = segs[-1].get("end", 0.0)
    best = None
    for s in reversed(segs):
        if end - s.get("start", 0.0) > budget:
            break
        best = s.get("start")
    return (best, end) if best is not None and end > best else None


def build_units(turns, roles, min_len, max_len, style="both", log=None):
    """Assemble candidate clips whose boundaries are correct by construction.

    style:
      "qa"    — host question + the guest's complete answer
      "guest" — a standalone guest moment
      "both"  — both kinds (default)

    Every candidate starts on a turn boundary and ends where a turn (or a complete
    sentence inside it) ends, so nothing can stop mid-answer.
    """
    host, guest = roles.get("host"), roles.get("guest")
    units = []

    for i, t in enumerate(turns):
        # ── Q&A: a host question, then everything the guest says in reply ──
        if style in ("qa", "both") and host is not None and t["speaker"] == host \
                and _is_question(t["text"]):
            answer_segs = []
            j = i + 1
            while j < len(turns) and turns[j]["speaker"] == guest:
                answer_segs.extend(turns[j]["segs"])
                j += 1
            if not answer_segs:
                continue
            # Start at the QUESTION, not at whatever else the host said first.
            # A host turn is usually "welcome back … so tell me, why did you quit?" —
            # opening on the greeting wastes the only three seconds that matter.
            q_segs = t["segs"]
            for k in range(len(q_segs) - 1, -1, -1):
                if _is_question(q_segs[k].get("text", "")):
                    q_segs = q_segs[k:]
                    break
            q_start = q_segs[0].get("start", t["start"])

            budget = max_len - (t["end"] - q_start)
            if budget < 4.0:
                # Even the question alone eats the clip — keep only its tail.
                q_win = _trim_to_sentences(q_segs, max_len * 0.5, from_start=False)
                if not q_win:
                    continue
                q_start = q_win[0]
                budget = max_len - (t["end"] - q_start)
            ans = _trim_to_sentences(answer_segs, budget, from_start=True)
            if not ans:
                continue
            start, end = q_start, ans[1]
            if end - start < min_len:
                continue
            units.append({
                "kind": "qa", "start": round(start, 3), "end": round(end, 3),
                "question": t["text"][:200],
                "text": " ".join((s.get("text") or "") for s in answer_segs)[:600],
            })

        # ── Standalone guest moment ──
        if style in ("guest", "both") and t["speaker"] == guest:
            win = _trim_to_sentences(t["segs"], max_len, from_start=True)
            if win and win[1] - win[0] >= min_len:
                units.append({
                    "kind": "guest", "start": round(win[0], 3), "end": round(win[1], 3),
                    "question": "", "text": t["text"][:600],
                })

    # Drop near-duplicates (a Q&A and a guest unit can cover the same answer).
    units.sort(key=lambda u: (u["start"], -(u["end"] - u["start"])))
    deduped = []
    for u in units:
        if any(abs(u["start"] - d["start"]) < 2.0 and abs(u["end"] - d["end"]) < 2.0
               for d in deduped):
            continue
        deduped.append(u)

    if log:
        qa = sum(1 for u in deduped if u["kind"] == "qa")
        log.log(f"  Candidate clips: {len(deduped)} ({qa} Q&A, {len(deduped)-qa} guest-only) "
                f"— every one starts and ends on a turn boundary")
    return deduped


# ─────────────────────────────────────────────────────────────
# Ranking
# ─────────────────────────────────────────────────────────────

_RANK_PROMPT = """You are choosing clips from a podcast/interview for Instagram Reels
and YouTube Shorts. Your only objective is VIEWS.

Below are numbered CANDIDATE clips. Their start and end points are already correct —
each one is a complete question-and-answer or a complete standalone point. You are
NOT choosing timestamps. You are only judging which candidates are worth posting.

PICK the ones that:
- open on something that stops a scroll (a bold claim, a number, conflict, a question
  a viewer needs answered)
- carry real emotion or a strong opinion, not neutral information
- make full sense to someone who has never heard of this podcast
- land a payoff: a punchline, a reveal, a lesson

REJECT: pleasantries, intros, sponsor reads, logistics, anything that only sets up a
point made later, and anything that needs the rest of the episode to make sense.

Score 1-10 for PREDICTED VIEWS (10 = would genuinely take off). Return the best {want}.

Output ONLY valid JSON:
{{"picks":[{{"id":int,"score":int,"reason":"what the hook is and what the payoff is"}}]}}

CANDIDATES:
{blocks}"""


def _fallback_rank(units, want, log=None):
    """Heuristic ranking when no model answers. Prefers Q&A pairs (a question is a
    built-in hook) and longer, more substantial answers."""
    def score(u):
        s = 6.0
        if u["kind"] == "qa":
            s += 1.5
        words = len((u.get("text") or "").split())
        s += min(1.5, words / 90.0)
        if _ends_sentence(u.get("text", "")):
            s += 0.5
        return s
    ranked = sorted(units, key=score, reverse=True)[:want]
    for u in ranked:
        u["score"] = int(round(score(u)))
        u["reason"] = ("Question and its full answer" if u["kind"] == "qa"
                       else "Standalone point from the guest")
    if log:
        log.log(f"  Ranking: model unavailable — kept {len(ranked)} by heuristic")
    return ranked


def rank_units(units, want, log=None, prefer_model="auto", clip_prompt=""):
    """Ask the model to score the candidates. Falls back to heuristics."""
    if not units:
        return []
    pool = units[:MAX_CANDIDATES]
    blocks = []
    for i, u in enumerate(pool):
        head = f"[{i}] ({u['end'] - u['start']:.0f}s, {u['kind']})"
        if u.get("question"):
            head += f"\n  HOST ASKS: {u['question']}"
        blocks.append(f"{head}\n  ANSWER: {u['text']}")
    prompt = _RANK_PROMPT.format(want=want, blocks="\n\n".join(blocks))
    if clip_prompt:
        prompt += (f"\n\nTHE USER ONLY WANTS CLIPS MATCHING THIS BRIEF — reject anything "
                   f"that does not match, even if it would otherwise perform:\n{clip_prompt}")

    raw = providers.chat(prompt, temperature=0.2, json_mode=True, log=log,
                         prefer_model=prefer_model)
    if not raw:
        return _fallback_rank(pool, want, log)
    try:
        parsed = json.loads(raw)
        picks = parsed.get("picks", parsed) if isinstance(parsed, dict) else parsed
        if not isinstance(picks, list):
            raise ValueError("picks is not a list")
    except Exception as e:
        if log:
            log.log(f"  Ranking: could not parse model JSON ({e}) — using heuristic")
        return _fallback_rank(pool, want, log)

    out = []
    for p in picks:
        try:
            idx = int(p.get("id"))
        except (TypeError, ValueError):
            continue
        if not (0 <= idx < len(pool)):
            continue
        u = dict(pool[idx])
        try:
            u["score"] = max(1, min(10, int(p.get("score", 7))))
        except (TypeError, ValueError):
            u["score"] = 7
        u["reason"] = str(p.get("reason", "") or "Podcast moment")[:400]
        if not any(abs(u["start"] - o["start"]) < 1.0 for o in out):
            out.append(u)
    if not out:
        return _fallback_rank(pool, want, log)
    out.sort(key=lambda u: u.get("score", 0), reverse=True)
    if log:
        log.log(f"  Ranking: model returned {len(out)} usable pick(s)")
    return out[:want]


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def select(segments, num_clips, min_len, max_len, log=None,
           style="both", prefer_model="auto", clip_prompt=""):
    """Full podcast selection. Returns (highlights, roles).

    highlights match the shape the rest of the pipeline expects
    ({start, end, score, reason}), plus "speaker_roles" carried on the manifest so
    the burn stage can colour each speaker's captions.

    Returns ([], {}) when the transcript has no speaker labels — the caller then
    falls back to normal highlight selection rather than guessing.
    """
    turns = build_turns(segments, log)
    if not turns:
        if log:
            log.log("  No speaker labels in the transcript — podcast mode needs "
                    "diarisation (Deepgram). Falling back to normal selection.")
        return [], {}

    roles = identify_roles(turns, log)
    if roles.get("host") is None:
        if log:
            log.log("  Only one speaker detected — this is not an interview. "
                    "Falling back to normal selection.")
        return [], {}

    units = build_units(turns, roles, min_len, max_len, style, log)
    if not units:
        if log:
            log.log("  No candidate Q&A or guest moment fits the length bounds.")
        return [], roles

    picked = rank_units(units, num_clips, log, prefer_model, clip_prompt)
    highlights = [{
        "start": u["start"], "end": u["end"],
        "score": u.get("score", 7),
        "reason": u.get("reason", "Podcast moment"),
        "kind": u.get("kind", "qa"),
    } for u in picked]
    highlights.sort(key=lambda h: h.get("score", 0), reverse=True)

    if log and highlights:
        log.log(f"  Podcast selection: {len(highlights)} clip(s)")
        for i, h in enumerate(highlights):
            log.log(f"    Clip {i+1}: {h['start']:.1f}s -> {h['end']:.1f}s "
                    f"({h['end']-h['start']:.0f}s) [{h['kind']}] score={h['score']}")
    return highlights, roles
