import os
import sys
import re
import math
import json
import shutil
import tempfile
import traceback
import datetime
import subprocess
import unicodedata
import argparse
import providers
import viral_council
import publish_kit
import joblog

# ─────────────────────────────────────────────────────────────
# Diagnostic Logger
# ─────────────────────────────────────────────────────────────

class DiagnosticLog:
    """Writes a human-readable diagnostic report to a .txt file."""

    def __init__(self, job_dir: str):
        self.job_dir = job_dir
        self.path = os.path.join(job_dir, "SUBTITLE_DIAGNOSTIC_REPORT.txt")
        self.lines = []
        self._write_header()

    def _write_header(self):
        self.lines.append("=" * 70)
        self.lines.append("   SUBTITLE BURNING DIAGNOSTIC REPORT")
        self.lines.append(f"   Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        self.lines.append("=" * 70)
        self.lines.append("")

    def section(self, title: str):
        self.lines.append("")
        self.lines.append("-" * 70)
        self.lines.append(f"   {title}")
        self.lines.append("-" * 70)
        self._flush()

    def log(self, msg: str):
        self.lines.append(msg)
        print(msg)
        self._flush()

    def log_json(self, label: str, obj):
        self.lines.append(f"{label}:")
        self.lines.append(json.dumps(obj, indent=2, ensure_ascii=False))
        self._flush()

    def error(self, msg: str, exc: Exception = None):
        self.lines.append(f"[ERROR] {msg}")
        if exc:
            self.lines.append(traceback.format_exc())
        print(f"[ERROR] {msg}", file=sys.stderr)
        self._flush()

    def _flush(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write("\n".join(self.lines))
        except Exception:
            pass

    def finalize(self, final_clips: list):
        self.section("FINAL RESULT")
        if final_clips:
            self.log(f"SUCCESS: {len(final_clips)} subtitled clip(s) produced:")
            for c in final_clips:
                size_kb = os.path.getsize(c) // 1024 if os.path.exists(c) else 0
                self.log(f"   - {os.path.basename(c)}  ({size_kb} KB)")
        else:
            self.log("FAILED: ZERO subtitled clips were produced. Check errors above.")
        self.log("")
        self.log(f"Report saved to: {self.path}")
        self._flush()


# ─────────────────────────────────────────────────────────────
# Audio extraction
# ─────────────────────────────────────────────────────────────

def _extract_clip_audio(source_video: str, output_path: str,
                        start: float, end: float, log: DiagnosticLog) -> str:
    """Extract ONLY the [start, end] slice of audio from the source video to mp3,
    16kHz mono (small + ideal for speech recognition). Timestamps in the result are
    clip-local (0 = clip start) because we seek before -i."""
    if not os.path.exists(source_video):
        raise FileNotFoundError(f"Source video not found: {source_video}")
    cmd = ["ffmpeg", "-y"]
    if start is not None and end is not None:
        cmd += ["-ss", f"{float(start):.3f}", "-to", f"{float(end):.3f}"]
    cmd += ["-i", source_video, "-vn", "-ar", "16000", "-ac", "1", "-f", "mp3", output_path]
    result = providers.run_cmd(cmd, timeout=600, retries=2, log=log, label="ffmpeg-clipaudio")
    if result.returncode != 0:
        raise RuntimeError(f"Clip audio extraction failed:\n{(result.stderr or '')[-800:]}")
    return output_path



def _slice_transcript(full_transcript_path: str, clip_start: float, clip_end: float) -> dict:
    """Slice the full-video Deepgram transcript JSON to the clip's [start, end] window
    and shift all timestamps to clip-local time (0 = clip start).

    This avoids re-calling Deepgram for every overlapping clip — the single full-video
    transcription already has every word with precise timestamps; we just extract the
    relevant slice and subtract clip_start so the ASS timecodes are clip-relative."""
    with open(full_transcript_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    result_segments = []
    for seg in data.get("segments", []):
        seg_s = seg.get("start", 0)
        seg_e = seg.get("end", 0)
        # Keep segment if it overlaps the clip window (even partially)
        if seg_e <= clip_start or seg_s >= clip_end:
            continue
        # Clip-local timestamps (clamped to [0, duration])
        local_s = max(0.0, seg_s - clip_start)
        local_e = min(clip_end - clip_start, seg_e - clip_start)
        words_in = []
        for w in seg.get("words", []):
            ws, we = w.get("start", seg_s), w.get("end", seg_e)
            if we <= clip_start or ws >= clip_end:
                continue
            wd = {
                "word": w.get("word", ""),
                "start": max(0.0, ws - clip_start),
                "end":   min(clip_end - clip_start, we - clip_start),
            }
            # Speaker labels must survive slicing, or podcast captions lose the
            # colour that says who is talking.
            if w.get("speaker") is not None:
                wd["speaker"] = w.get("speaker")
            words_in.append(wd)
        entry = {
            "start": round(local_s, 3),
            "end":   round(local_e, 3),
            "text":  seg.get("text", "").strip(),
            "words": words_in,
        }
        if seg.get("speaker") is not None:
            entry["speaker"] = seg.get("speaker")
        result_segments.append(entry)
    return {"segments": result_segments}


def _shift_segment(seg: dict, delta: float, lo: float = None, hi: float = None) -> dict:
    """Copy a segment with every timestamp moved by `delta`, optionally clamped into
    [lo, hi]. Text fields (Devanagari, English, Hinglish) are carried across
    untouched, so this is safe to run after the translation passes."""
    def _t(value):
        v = float(value or 0.0) + delta
        if lo is not None:
            v = max(lo, v)
        if hi is not None:
            v = min(hi, v)
        return round(v, 3)

    out = {k: v for k, v in seg.items() if k not in ("start", "end", "words")}
    out["start"] = _t(seg.get("start", 0))
    out["end"] = _t(seg.get("end", 0))
    out["words"] = [dict(w, start=_t(w.get("start", 0)), end=_t(w.get("end", 0)))
                    for w in seg.get("words", [])]
    return out


def _remap_segments_for_hook(segments: list, hook_start: float, hook_end: float) -> list:
    """Rebuild a clip's captions for the HOOK-FIRST timeline.

    The rendered video is [3s hook][full clip], so the caption timeline becomes:
        0 … hook_len             the hook's own words, pulled back to start at 0
        hook_len … hook_len+dur  the whole clip, pushed back by hook_len

    Everything downstream (word timings, karaoke, cue chunking) is purely
    time-based, so remapping the segments here is all that hook support costs: the
    ASS builder is handed one continuous list and never learns a hook exists.

    `hook_start` / `hook_end` are CLIP-LOCAL seconds.
    """
    hook_len = float(hook_end) - float(hook_start)
    if hook_len <= 0 or not segments:
        return segments

    hooked = []
    for seg in segments:
        s, e = float(seg.get("start", 0)), float(seg.get("end", 0))
        if e <= hook_start or s >= hook_end:
            continue
        new = _shift_segment(seg, -hook_start, lo=0.0, hi=hook_len)
        # Words outside the window all clamp onto the boundary; without this they
        # pile up as a stack of zero-length cues on the first or last frame.
        new["words"] = [w for w in new["words"] if w["end"] > w["start"]]
        if new["end"] > new["start"]:
            hooked.append(new)

    return hooked + [_shift_segment(seg, hook_len) for seg in segments]


def transcribe_clip(audio_path: str, job_dir: str, clip_index: int, log: DiagnosticLog,
                    clip_start: float = None, clip_end: float = None,
                    full_transcript_path: str = None, translate: bool = True) -> dict:
    """Get word-level timestamps for one clip.

    FAST PATH: if full_transcript_path (or a discoverable one in job_dir) exists
    and clip_start/clip_end are supplied, slices the JSON — zero API calls.
    SLOW PATH: falls back to a direct Deepgram nova-3 call on clip audio.

    translate=True translates here (per-clip, used by the standalone slow path).
    translate=False skips translation so a caller can BATCH all clips in one call."""
    # Resolve the best available full-video transcript
    if full_transcript_path is None or not os.path.exists(full_transcript_path):
        deepgram_full = os.path.join(job_dir, "transcript_deepgram.json")
        whisper_full  = os.path.join(job_dir, "transcript_full.json")
        full_transcript_path = deepgram_full if os.path.exists(deepgram_full) else (
                               whisper_full  if os.path.exists(whisper_full)  else None)

    if clip_start is not None and clip_end is not None and full_transcript_path:
        log.log(f"[Clip {clip_index}] Slicing full-video transcript [{clip_start:.2f}-{clip_end:.2f}s] "
                f"(no Deepgram call)")
        data = _slice_transcript(full_transcript_path, clip_start, clip_end)
    else:
        # ── Slow path: fresh transcription through the full provider chain ──
        log.log(f"[Clip {clip_index}] Transcribing clip audio "
                f"({'no full transcript found' if full_transcript_path is None else 'no timestamps given'})...")
        data = providers.transcribe_audio(audio_path, language="hi", log=log) or {"segments": []}

    segments = data.get("segments", [])
    words_total = sum(len(s.get("words", [])) for s in segments)
    log.log(f"  Clip segments : {len(segments)}  |  Clip words: {words_total}")

    if segments and translate:
        _translate_segments_to_english(segments, log=log)

    # JSON (machine-readable, full detail)
    transcript_path = os.path.join(job_dir, f"transcript_clip_{clip_index}.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

    transcripts_txt = os.path.join(job_dir, "clip_transcripts.txt")
    with open(transcripts_txt, "a", encoding="utf-8") as tf:
        tf.write("=" * 70 + "\n")
        tf.write(f"CLIP {clip_index}  ({len(segments)} segments, {words_total} words)\n")
        tf.write("=" * 70 + "\n")
        for seg in segments:
            tf.write(f"[{seg['start']:7.2f} - {seg['end']:7.2f}]\n")
            tf.write(f"   HI: {seg.get('text', '').strip()}\n")
            tf.write(f"   EN: {seg.get('text_en', '').strip()}\n")
        tf.write("\n")

    log.log(f"  Clip transcript JSON saved: {transcript_path}")
    log.log(f"  Clip transcript text appended: {transcripts_txt}")
    return data


# ─────────────────────────────────────────────────────────────
# Translation (Hindi/Devanagari -> English) for the bottom subtitle track
# ─────────────────────────────────────────────────────────────

def _translate_segments_to_english(segments: list, log: DiagnosticLog):
    """Translate each segment's Devanagari text into natural English and store it
    on seg['text_en']. The original Devanagari seg['text'] / per-word timings are
    left untouched (those drive the Hindi top track). Mutates segments in place."""
    log.section("TRANSLATION (Hindi -> English)")

    # Safe default so the bottom track is never empty if translation is unavailable.
    for seg in segments:
        seg.setdefault("text_en", "")

    texts = [seg["text"].strip() for seg in segments]
    results = _translate_texts(texts, log)
    for i, seg in enumerate(segments):
        if i < len(results) and results[i]:
            seg["text_en"] = results[i]


def _translate_texts(texts: list, log: DiagnosticLog) -> list:
    """Translate a flat list of Hindi strings to English in ONE Groq call.
    Returns a list of English strings aligned by index (missing -> '')."""
    out = [""] * len(texts)
    if not texts:
        return out

    bulk_text = "\n".join(f"{i}:: {t}" for i, t in enumerate(texts))

    prompt = """You are an expert Hindi-to-English translator for video subtitles.
Translate each numbered line of Hindi (Devanagari) into natural, conversational English.
CRITICAL RULES:
1. Keep the EXACT line numbers and prefix format (e.g., 0:: ).
2. Output exactly one line per input line, in the same order.
3. Translate MEANING into fluent English - do NOT transliterate, do NOT keep Hindi words.
4. Keep each translation concise enough to read as a subtitle (it must fit on screen).
TEXT:\n""" + bulk_text

    raw_content = providers.chat(prompt, temperature=0.2, log=log)
    if not raw_content:
        log.log("  Translation unavailable (all providers failed) — English track will be blank")
        return out
    raw_content = re.sub(r"```[a-zA-Z]*\n", "", raw_content).replace("`" * 3, "")
    for line in raw_content.split("\n"):
        if not line.strip() or "::" not in line:
            continue
        try:
            parts = line.split("::", 1)
            idx_match = re.search(r"\d+", parts[0].strip())
            if not idx_match:
                continue
            idx = int(idx_match.group())
            if 0 <= idx < len(out):
                out[idx] = parts[1].strip()
        except Exception:
            continue
    return out


def batch_translate_clips(clips_segments: list, log: DiagnosticLog) -> None:
    """STAGE 4 — translate EVERY segment of EVERY clip in a SINGLE Groq call.

    clips_segments: list of per-clip segment lists (each seg has 'text', gets 'text_en').
    Flattens all segments across all clips, translates once, writes results back.
    This replaces N per-clip translation calls with exactly 1."""
    log.section("BATCH TRANSLATION (all clips, one call)")
    flat = []          # (clip_i, seg_i)
    texts = []
    for ci, segs in enumerate(clips_segments):
        for si, seg in enumerate(segs):
            seg.setdefault("text_en", "")
            flat.append((ci, si))
            texts.append(seg["text"].strip())

    if not texts:
        log.log("  No segments to translate.")
        return

    log.log(f"  Translating {len(texts)} segments from {len(clips_segments)} clips in one call...")
    results = _translate_texts(texts, log)
    for (ci, si), en in zip(flat, results):
        if en:
            clips_segments[ci][si]["text_en"] = en
    log.log("  Batch translation complete.")


# ─────────────────────────────────────────────────────────────
# Transliteration (Hindi/Devanagari -> Roman "Hinglish") for a Latin-script track
# ─────────────────────────────────────────────────────────────

def _transliterate_texts(texts: list, log: DiagnosticLog) -> list:
    """Romanise a flat list of Hindi (Devanagari) strings into natural Hinglish in
    ONE Groq call. Hinglish = Hindi words written in Latin/Roman letters the way
    Indians type online (common English words kept as English).
    Returns a list aligned by index (missing -> '')."""
    out = [""] * len(texts)
    if not texts:
        return out

    bulk_text = "\n".join(f"{i}:: {t}" for i, t in enumerate(texts))

    prompt = """You are an expert at writing Hinglish subtitles.
For each numbered line of Hindi (Devanagari), write the SAME sentence in ROMAN/LATIN letters (Hinglish)
- the way Indians casually type Hindi in English script.
CRITICAL RULES:
1. Keep the EXACT line numbers and prefix format (e.g., 0:: ).
2. Output exactly one line per input line, in the same order.
3. Do NOT translate the meaning. ROMANISE the Hindi sounds (e.g. "मैं ठीक हूँ" -> "main theek hoon").
4. Keep common English words that appear as English. Use everyday, readable spelling (no accents/diacritics).
TEXT:\n""" + bulk_text

    raw_content = providers.chat(prompt, temperature=0.2, log=log)
    if not raw_content:
        log.log("  Transliteration unavailable (all providers failed) — Hinglish track will be blank")
        return out
    raw_content = re.sub(r"```[a-zA-Z]*\n", "", raw_content).replace("`" * 3, "")
    for line in raw_content.split("\n"):
        if not line.strip() or "::" not in line:
            continue
        try:
            parts = line.split("::", 1)
            idx_match = re.search(r"\d+", parts[0].strip())
            if not idx_match:
                continue
            idx = int(idx_match.group())
            if 0 <= idx < len(out):
                out[idx] = parts[1].strip()
        except Exception:
            continue
    return out


def batch_transliterate_clips(clips_segments: list, log: DiagnosticLog) -> None:
    """STAGE 4 — romanise EVERY segment of EVERY clip into Hinglish in a SINGLE Groq
    call and write the result onto seg['text_hinglish']. Mirrors batch_translate_clips."""
    log.section("BATCH TRANSLITERATION (Hindi -> Hinglish, one call)")
    flat = []
    texts = []
    for ci, segs in enumerate(clips_segments):
        for si, seg in enumerate(segs):
            seg.setdefault("text_hinglish", "")
            flat.append((ci, si))
            texts.append(seg["text"].strip())

    if not texts:
        log.log("  No segments to transliterate.")
        return

    log.log(f"  Transliterating {len(texts)} segments from {len(clips_segments)} clips in one call...")
    results = _transliterate_texts(texts, log)
    for (ci, si), hg in zip(flat, results):
        if hg:
            clips_segments[ci][si]["text_hinglish"] = hg
    log.log("  Batch transliteration complete.")


def batch_generate_titles(clips_segments: list, log: DiagnosticLog) -> list:
    """STAGE 4 — generate ONE crisp on-screen title per clip in a SINGLE Groq call.

    Returns a list of title strings aligned to clips_segments order ('' if none).
    The title is short (<= 6 words), punchy, English, and based on the clip's content."""
    log.section("BATCH TITLE GENERATION (all clips, one call)")
    titles = ["" for _ in clips_segments]

    # Build a compact prompt: one numbered block of Hindi text per clip.
    blocks = []
    for ci, segs in enumerate(clips_segments):
        hi = " ".join((s.get("text") or "").strip() for s in segs).strip()
        if not hi:
            hi = "(no transcript)"
        blocks.append(f"{ci}:: {hi[:600]}")   # cap length per clip to keep prompt small
    bulk = "\n".join(blocks)

    prompt = """You are a viral short-form video editor. For each numbered clip below, write ONE
punchy on-screen TITLE that would make someone stop scrolling. Rules:
1. Keep the EXACT line numbers and prefix (e.g., 0:: ).
2. Max 6 words. No quotes, no emojis, no hashtags, no ending punctuation.
3. English. Make it a curiosity hook or bold statement tied to the clip's content.
4. Output exactly one line per clip, same order.
CLIPS:\n""" + bulk

    raw = providers.chat(prompt, temperature=0.6, log=log)
    if not raw:
        log.log("  Title generation unavailable (all providers failed) — no titles")
        return titles
    raw = re.sub(r"```[a-zA-Z]*\n", "", raw).replace("`" * 3, "")
    for line in raw.split("\n"):
        if "::" not in line:
            continue
        try:
            pfx, val = line.split("::", 1)
            m = re.search(r"\d+", pfx)
            if not m:
                continue
            idx = int(m.group())
            if 0 <= idx < len(titles):
                titles[idx] = val.strip().strip('"').strip()
        except Exception:
            continue
    log.log(f"  Generated {sum(1 for t in titles if t)}/{len(titles)} titles.")
    return titles


# ─────────────────────────────────────────────────────────────
# ASS timing helpers (built from a CLIP-LOCAL transcript, no time remap needed)
# ─────────────────────────────────────────────────────────────

def _fmt_ass_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    cs = int(round((seconds % 1) * 100))
    s = int(seconds)
    if cs == 100:
        cs = 0
        s += 1
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _headline_text(title: str, upper: bool = True) -> str:
    """Render the on-screen headline for ASS.

    Two-line meme titles ("bro: you're lucky" / "me: *my luck*") are the reason this
    is not just .upper(): the format depends on BOTH the line break and the lowercase
    voice, and shouting it in caps kills the joke. So a multi-line title keeps its
    own case and gets a real ASS line break; a single-line headline is still
    uppercased, which is what reads best as a big overlay.

    `upper` comes from the headline style the user picked, so a style that is not
    an all-caps style (Outline, Cinematic, Fade…) now leaves the typed case alone.
    It defaults True because the original headline was always shouted."""
    lines = [ln.strip() for ln in str(title or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    if len(lines) == 1:
        return _ass_escape(lines[0].upper() if upper else lines[0])
    return "\\N".join(_ass_escape(ln) for ln in lines)


def _ass_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", " ").strip()


# Subtitle chunking target: 5-10 words per on-screen frame.
_WORDS_PER_CUE_MIN = 2
_WORDS_PER_CUE_MAX = 3

# How many words sit on screen at once, when the user picks a number in the UI.
WORDS_ON_SCREEN_MIN, WORDS_ON_SCREEN_MAX = 1, 8
# 0 / unset means "auto": the count each style was tuned around. Only word_pop
# differs — one word at a time IS the style, so it must not inherit the general
# three-word default.
_STYLE_AUTO_WORDS = {"word_pop": 1}


def _resolve_words_on_screen(caption_words, caption_style: str) -> int:
    """How many words one cue may hold. 0/None/garbage -> the style's own default."""
    try:
        n = int(caption_words or 0)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return _STYLE_AUTO_WORDS.get((caption_style or "").lower(), _WORDS_PER_CUE_MAX)
    return max(WORDS_ON_SCREEN_MIN, min(WORDS_ON_SCREEN_MAX, n))


def _chunk_word_cues(segments: list, lead_offset: float, clip_duration,
                     max_words: int = _WORDS_PER_CUE_MAX) -> list:
    """Group Devanagari words (with real timestamps) into cues of up to max_words,
    preferring to break at the end of a segment (natural pause). Each cue is timed
    from its first word's start to its last word's end. Returns (start, end, text).

    Fallback: when a segment has no word-level timestamps but has text, the segment
    duration is divided evenly across its words so subtitles still appear."""
    cues = []
    bucket = []  # list of (start, end, word)
    bucket_spk = None       # speaker the current bucket belongs to
    for seg in segments:
        # A cue must never mix two speakers: in a podcast the caption colour tells
        # you WHO is talking, so a cue straddling a turn change would be a lie.
        seg_spk = seg.get("speaker")
        if bucket and seg_spk != bucket_spk:
            cues.append(_flush_bucket(bucket, clip_duration)); bucket = []
        bucket_spk = seg_spk

        seg_words = [w for w in seg.get("words", [])
                     if (w.get("word") or "").strip() and w.get("start") is not None and w.get("end") is not None]

        # Fallback: no word timestamps — synthesise them from the segment span.
        if not seg_words:
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            s0 = max(0.0, float(seg.get("start", 0)) - lead_offset)
            e0 = max(0.0, float(seg.get("end", 0)) - lead_offset)
            if clip_duration is not None:
                e0 = min(e0, clip_duration)
            toks = text.split()
            if toks and e0 > s0:
                span = (e0 - s0) / len(toks)
                seg_words = [(s0 + i * span, s0 + (i + 1) * span, t) for i, t in enumerate(toks)]
            else:
                continue

        for item in seg_words:
            if isinstance(item, tuple):
                ws, we, word = item
            else:
                ws = max(0.0, item["start"] - lead_offset)
                we = max(0.0, item["end"] - lead_offset)
                word = (item["word"] or "").strip()
            bucket.append((ws, we, word))
            if len(bucket) >= max_words:
                cues.append(_flush_bucket(bucket, clip_duration)); bucket = []
        # Prefer a break at the segment boundary once we have a readable amount.
        # Never demand more than the cue can hold, or a 1-word setting would keep
        # the tail of every segment waiting for a second word that cannot arrive.
        if len(bucket) >= min(_WORDS_PER_CUE_MIN, max_words):
            cues.append(_flush_bucket(bucket, clip_duration)); bucket = []
    if bucket:
        cues.append(_flush_bucket(bucket, clip_duration))
    # Enforce no-overlap: each cue ends no later than the next cue starts.
    valid = [c for c in cues if c]
    for i in range(len(valid) - 1):
        s, e, t = valid[i]
        next_s = valid[i + 1][0]
        if e > next_s:
            valid[i] = (s, next_s, t)
    return valid


def _flush_bucket(bucket, clip_duration):
    if not bucket:
        return None
    c_start, c_end = bucket[0][0], bucket[-1][1]
    if clip_duration is not None:
        c_end = min(c_end, clip_duration)
    if c_end <= c_start:
        c_end = c_start + 0.3
    return (c_start, c_end, " ".join(b[2] for b in bucket))


def _chunk_text_cues(segments: list, text_key: str, lead_offset: float, clip_duration,
                     max_words: int = _WORDS_PER_CUE_MAX) -> list:
    """For text without per-word timings (the English translation), split each
    segment's text into <=max_words chunks and distribute the segment's time span
    evenly across them. Returns (start, end, text)."""
    cues = []
    for seg in segments:
        text = (seg.get(text_key) or "").strip()
        if not text:
            continue
        seg_s = max(0.0, float(seg["start"]) - lead_offset)
        seg_e = max(0.0, float(seg["end"]) - lead_offset)
        if clip_duration is not None:
            seg_e = min(seg_e, clip_duration)
        if seg_e <= seg_s:
            continue
        words = text.split()
        # number of chunks needed for this segment
        n_chunks = max(1, math.ceil(len(words) / max_words))
        per = math.ceil(len(words) / n_chunks)
        span = (seg_e - seg_s) / n_chunks
        for i in range(n_chunks):
            piece = " ".join(words[i * per:(i + 1) * per]).strip()
            if not piece:
                continue
            cs = seg_s + i * span
            ce = seg_s + (i + 1) * span
            cues.append((cs, ce, piece))
    return cues


# Output canvases. Every one is a real platform target, so the list is short on
# purpose — an arbitrary WxH box would just be a way to render something no feed
# accepts. 9:16 stays the default because that is what this tool is for.
ASPECTS = {
    "9:16": (1080, 1920),   # Shorts / Reels / TikTok
    "4:5":  (1080, 1350),   # the tallest post a feed will show uncropped
    "1:1":  (1080, 1080),   # square feed post
    "16:9": (1920, 1080),   # YouTube landscape
}
DEFAULT_ASPECT = "9:16"
# Kept as the reference canvas: every pixel constant below (font sizes, margins,
# insets) was tuned against it, and _frame() rescales them for anything else.
SHORTS_W, SHORTS_H = ASPECTS[DEFAULT_ASPECT]


def _frame(aspect: str = None) -> tuple:
    """(width, height) for an aspect key, falling back to 9:16."""
    return ASPECTS.get((aspect or "").strip() or DEFAULT_ASPECT, ASPECTS[DEFAULT_ASPECT])


def _k(frame: tuple) -> float:
    """Scale factor from the 1080x1920 reference canvas to `frame`, by AREA.

    Not by height. Height alone gives 16:9 a caption 3% of frame height — correct
    arithmetic, but it reads tiny, because a landscape frame is far wider and the
    eye judges type against the whole picture rather than its height. sqrt(area)
    is the standard compromise: it leaves 9:16 exactly as it was (ratio 1.0) and
    lands 16:9 near the 5% of height that landscape subtitles actually use."""
    return math.sqrt((frame[0] * frame[1]) / float(SHORTS_W * SHORTS_H))


def _sz(px: int, frame: tuple) -> int:
    """A reference-canvas pixel size, rescaled for `frame` (never below 1)."""
    return max(1, int(round(px * _k(frame))))

# Hard ceiling on a single clip render, so one wedged ffmpeg can't hold a worker
# thread (and the whole job) forever.


# Visual tuning (real pixels on the 1080x1920 frame)
# Caption size is a MULTIPLIER, not an absolute point size: every style preset
# carries its own size (bold_yellow is bigger than outline by design), so a fixed
# number would flatten those differences. Scaling preserves the style's proportions
# while still letting the user make everything bigger or smaller.
CAPTION_SIZE_MIN = 0.6
CAPTION_SIZE_MAX = 1.8
CAPTION_SIZE_DEFAULT = 1.0

_HI_FONTSIZE    = 60
_TITLE_FONTSIZE = 64
_PART_FONTSIZE  = 58          # "Part 3" badge (sequential mode)
# Default seat for the part badge: top of the frame, above every caption preset.
_PART_DEFAULT_MARGIN = 60
# When a dual caption pair is dragged, how far below the Hindi line the English
# line sits, as a fraction of frame height.
_DUAL_GAP_FRAC = 0.055
_EN_FONTSIZE    = 54
_SINGLE_FONTSIZE = 58
_SIDE_MARGIN    = 70
_OUTLINE        = 5
_TITLE_COLOUR   = "&H0033E6FF"   # warm yellow (ASS = AABBGGRR) for the crisp title

# Single-track caption positions:
#   top    -> above the video band (in the upper letterbox bar)
#   middle -> centred over the video
#   bottom -> ON the video, near its bottom edge (overlaid on the footage)
#   below  -> just BELOW the video band (in the lower letterbox bar, outside the footage)
# Values below are the fallback (no known video geometry).
_POS_MARGIN_V = {"top": 120, "middle": 0, "bottom": 170, "below": 170}
_POS_ALIGN    = {"top": 8, "middle": 5, "bottom": 2, "below": 2}

# When the source isn't already 9:16 it gets letterboxed (black bars top/bottom).
# "below"/"top" sit in those bars (just outside the footage); "bottom" sits ON the
# footage a little above its bottom edge.
_LETTERBOX_GAP = 26          # px gap between the video band and an OUTSIDE caption
_ON_VIDEO_INSET = 70         # px above the video's bottom edge for an ON-video caption
_MIN_BAR_FOR_OUTSIDE = 170   # need at least this much bar to seat a caption outside the video


def _video_box(src_w, src_h, frame: tuple = None, fit: str = "fit"):
    """Given the SOURCE w/h, return (video_top_y, video_bottom_y) of the scaled video
    band inside `frame`, matching whatever the render filter does. None if the
    dimensions are unknown.

    Under "fill" the footage is cropped to cover the whole canvas, so there are no
    letterbox bars at all and the band IS the frame — which is what makes the
    outside-the-video caption positions collapse back onto the footage."""
    if not src_w or not src_h:
        return None
    W, H = frame or (SHORTS_W, SHORTS_H)
    if fit == "fill":
        return (0.0, float(H))
    scale = min(W / float(src_w), H / float(src_h))
    vh = src_h * scale
    pad_top = (H - vh) / 2.0
    return (pad_top, pad_top + vh)


def _position_layout(position, video_box, frame: tuple = None):
    """Return (ass_alignment, margin_v) for a single caption.
      top    -> just ABOVE the video band (upper bar) when there's room
      middle -> centred on the video
      bottom -> ON the video, a little above its bottom edge
      below  -> just BELOW the video band (lower bar) when there's room
    Falls back to in-frame margins when geometry is unknown or there's no bar."""
    if position not in _POS_ALIGN:
        position = "bottom"
    F = frame or (SHORTS_W, SHORTS_H)
    H = F[1]
    fb = {k: _sz(v, F) for k, v in _POS_MARGIN_V.items()}     # fallback margins
    gap, inset = _sz(_LETTERBOX_GAP, F), _sz(_ON_VIDEO_INSET, F)
    min_bar = _sz(_MIN_BAR_FOR_OUTSIDE, F)
    if not video_box:
        return _POS_ALIGN[position], fb[position]
    v_top, v_bot = video_box
    lower_bar = H - v_bot
    upper_bar = v_top
    if position == "middle":
        return 5, 0
    if position == "bottom":
        # bottom-anchored, sitting INSIDE the footage just above its bottom edge
        return 2, max(0, int(round(lower_bar + inset)))
    if position == "below":
        if lower_bar >= min_bar:
            return 8, int(round(v_bot + gap))   # top-anchored, just under the video
        return 2, fb["bottom"]                  # no bar -> fall back onto the video
    if position == "top":
        if upper_bar >= min_bar:
            return 2, int(round(H - v_top + gap))  # bottom-anchored, just above video
        return 8, fb["top"]
    return _POS_ALIGN[position], fb[position]


def _probe_dimensions(path, log):
    """Return (width, height) of the first video stream, or (None, None)."""
    try:
        r = providers.run_cmd(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", path],
            timeout=60, retries=2, log=log, label="ffprobe")
        parts = (r.stdout or "").strip().split(",")
        return int(parts[0]), int(parts[1])
    except Exception:
        return None, None

# Colours (ASS = AABBGGRR, where AA=00 is fully opaque)
_WHITE = "&H00FFFFFF"
_BLACK = "&H00000000"
_SHADOW_BACK = "&H64000000"   # translucent black drop shadow (outline style)
_BOX_BACK    = "&HA0000000"   # mostly-opaque black box (box style)
_WHITE_BOX_BACK = "&H80FFFFFF"  # 50% white / 50% transparent slab (white_box style; dark text)
_ORANGE      = "&H0000A5FF"   # RGB FF A5 00 — vibrant orange (fire)
_DARK_RED    = "&H000000CC"   # RGB CC 00 00 — deep red (fire outline)
_MAGENTA_BOX = "&HA0FF00FF"   # 63% opaque magenta slab (retro box)
_MINT        = "&H00C5F5C0"   # RGB C0 F5 C5 — soft mint green (mint)
_DEEP_BACK   = "&HB0201810"   # 69% opaque near-black slab (mint)
_HOT_PINK    = "&H009B4BFF"   # RGB FF 4B 9B — hot pink (sunset)
_DEEP_PURPLE = "&H00701A3A"   # RGB 3A 1A 70 — deep purple outline (sunset)

# One reusable Style-row tail. Order matches the Format line below.
_STYLE_FIELDS = "1,0,0,0,100,100,0,0,{bs},{ol},{sh},{al},{ml},{mr},{mv},1"

# A few punchy accent colours (ASS = AABBGGRR).
_YELLOW = "&H0000FFFF"   # RGB FFFF00
_GREEN  = "&H0040E62E"   # RGB 2EE640 (vivid lime)
_PINK   = "&H00B469FF"   # RGB FF69B4 (hot pink)
_CYAN   = "&H00FFFF00"   # RGB 00FFFF
_DEFAULT_ACCENT = _YELLOW


def _hex_to_ass(hexstr: str, default: str = _DEFAULT_ACCENT) -> str:
    """Convert '#RRGGBB' (web hex) into an ASS '&H00BBGGRR' colour string."""
    if not hexstr:
        return default
    s = str(hexstr).strip().lstrip("#")
    if len(s) != 6:
        return default
    try:
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    except ValueError:
        return default
    return f"&H00{b:02X}{g:02X}{r:02X}"


# Trendy caption presets. Each bundles size, colours, border + an optional animation.
#   anim: None      -> static text
#         "karaoke" -> phrase stays on screen, the spoken word lights up (Hormozi look)
#         "fade"    -> each cue fades in/out smoothly
# Recognised names: outline, box, white_box, bold_yellow, karaoke,
#                   neon, retro, shadow, fire, fade.
def _style_preset(name: str, accent: str = _DEFAULT_ACCENT) -> dict:
    name = (name or "outline").lower()
    p = dict(fontsize=_SINGLE_FONTSIZE, primary=_WHITE, accent=accent,
             outline=_OUTLINE, border=1, shadow=1, back=_SHADOW_BACK,
             outline_colour=_BLACK, upper=False, anim=None)
    if name == "box":
        p.update(border=3, outline=6, shadow=0, back=_BOX_BACK)
    elif name == "white_box":
        # Dark text on a solid whitish slab (BorderStyle 3: outline colour = box padding).
        p.update(primary=_BLACK, border=3, outline=8, shadow=0,
                 back=_WHITE_BOX_BACK, outline_colour=_WHITE_BOX_BACK)
    elif name == "bold_yellow":
        p.update(fontsize=66, primary=_YELLOW, outline=6, upper=True)
    elif name == "karaoke":
        p.update(fontsize=62, outline=5, upper=True, anim="karaoke")
    elif name == "neon":
        # Cyan text, thick white stroke — glowing neon look, uppercase
        p.update(fontsize=64, primary=_CYAN, outline_colour=_WHITE, outline=4, shadow=3, upper=True, back=_BLACK)
    elif name == "retro":
        # White text on a magenta/pink opaque slab — TikTok retro aesthetic, uppercase
        p.update(fontsize=60, primary=_WHITE, border=3, outline=8, shadow=0,
                 back=_MAGENTA_BOX, outline_colour=_MAGENTA_BOX, upper=True)
    elif name == "shadow":
        # Cinematic: white text, large drop shadow, minimal outline (Netflix/film look)
        p.update(fontsize=62, primary=_WHITE, outline=1, shadow=7, border=1, back="&HA0000000")
    elif name == "fire":
        # Orange text, dark-red thick outline, uppercase — high energy
        p.update(fontsize=66, primary=_ORANGE, outline_colour=_DARK_RED, outline=6, shadow=2, upper=True, back=_BLACK)
    elif name == "fade":
        # Standard outline but each cue fades in and out smoothly
        p.update(fontsize=60, outline=5, anim="fade")
    elif name == "slide_up":
        # Each line rises into place from just below and settles. Calm motion.
        p.update(fontsize=60, outline=5, anim="slide_up")
    elif name == "bounce":
        # Overshoots past full size and springs back — playful, high energy.
        p.update(fontsize=66, outline=6, upper=True, anim="bounce")
    elif name == "typewriter":
        # Letters arrive one at a time, like the line is being typed live.
        p.update(fontsize=56, outline=5, anim="typewriter")
    elif name == "punch":
        # Slams in oversized and snaps down. The loudest of the animated set.
        p.update(fontsize=72, outline=7, upper=True, anim="punch")
    elif name == "word_pop":
        # ONE word at a time, scaling in — the fast-cut Reels/TikTok look.
        p.update(fontsize=76, outline=6, upper=True, anim="word_pop")
    elif name == "mint":
        # Soft mint text on a deep slab — calmer, good over busy footage.
        p.update(fontsize=60, primary=_MINT, border=3, outline=7, shadow=0,
                 back=_DEEP_BACK, outline_colour=_DEEP_BACK)
    elif name == "sunset":
        # Warm gradient-feel: hot pink text with a deep purple outline.
        p.update(fontsize=66, primary=_HOT_PINK, outline_colour=_DEEP_PURPLE,
                 outline=6, shadow=2, upper=True, back=_BLACK)
    elif name == "mono":
        # Small, tight, all-caps on a black bar — documentary / subtitle-track look.
        p.update(fontsize=48, primary=_WHITE, border=3, outline=5, shadow=0,
                 back=_BOX_BACK, outline_colour=_BOX_BACK, upper=True)
    elif name == "ransom":
        # Yellow on black slab, uppercase — the loud "MrBeast" caption.
        p.update(fontsize=70, primary=_YELLOW, border=3, outline=7, shadow=0,
                 back=_BOX_BACK, outline_colour=_BOX_BACK, upper=True)
    # "outline" == the default base
    return p


# What the UI shows for each style: a label and a one-line description. Kept next
# to the presets so a new style is added in exactly two places, not five.
CAPTION_STYLE_INFO = {
    "outline":     ("Outline",     "White text with a clean black edge. Reads on anything."),
    "box":         ("Dark box",    "White text on a translucent black slab."),
    "white_box":   ("Light box",   "Dark text on a white slab. Good over dark footage."),
    "bold_yellow": ("Bold yellow", "Big yellow caps. Loud and impossible to miss."),
    "karaoke":     ("Karaoke",     "Each word lights up as it is spoken."),
    "word_pop":    ("Word pop",    "One word at a time, scaling in. Fast-cut Reels look."),
    "slide_up":    ("Slide up",    "Each line rises into place and settles."),
    "bounce":      ("Bounce",      "Springs past full size, then settles back."),
    "typewriter":  ("Typewriter",  "Letters arrive one at a time, as if typed live."),
    "punch":       ("Punch",       "Slams in oversized and snaps down. Loudest motion."),
    "neon":        ("Neon",        "Cyan with a white stroke. High contrast, high energy."),
    "retro":       ("Retro",       "White caps on a magenta slab. Classic TikTok."),
    "shadow":      ("Cinematic",   "White text with a soft drop shadow. Filmic and quiet."),
    "fire":        ("Fire",        "Orange caps with a deep red edge. Maximum energy."),
    "fade":        ("Fade",        "Plain outline, each line fading gently in and out."),
    "mint":        ("Mint",        "Soft mint on a deep slab. Calm over busy footage."),
    "sunset":      ("Sunset",      "Hot pink caps with a purple edge."),
    "mono":        ("Mono",        "Small tight caps on a black bar. Documentary style."),
    "ransom":      ("Ransom",      "Huge yellow caps on black. The loudest option."),
}


def caption_style_catalogue() -> list:
    """Style list for the UI, in display order, with which layouts each supports."""
    return [{
        "key": key,
        "label": CAPTION_STYLE_INFO[key][0],
        "help": CAPTION_STYLE_INFO[key][1],
        "animated": key not in _STATIC_STYLES,
        # Animated styles fall back to plain outline in the dual-track layout, so the
        # UI can grey them out there instead of silently ignoring the choice.
        "dual_ok": key in _STATIC_STYLES,
    } for key in VALID_CAPTION_STYLES]


# Styles that are static text (valid for the dual layout); animated ones fall back
# to "outline" in dual since two animated tracks at once is visual noise.
_STATIC_STYLES = ("outline", "box", "white_box", "bold_yellow", "neon", "retro",
                  "shadow", "fire", "fade", "mint", "sunset", "mono", "ransom")
VALID_CAPTION_STYLES = ("outline", "box", "white_box", "bold_yellow", "karaoke",
                        "word_pop", "slide_up", "bounce", "typewriter", "punch",
                        "neon", "retro", "shadow", "fire", "fade",
                        "mint", "sunset", "mono", "ransom")


def _style_row(name: str, font: str, preset: dict, align: int, margin_v: int,
               primary: str = None, fontsize: int = None, frame: tuple = None) -> str:
    """Build one ASS 'Style:' line from a preset (with optional primary/size override).

    Every pixel here — size, outline, shadow, side margin — was tuned on the 1080x1920
    reference canvas, so each is rescaled for the real frame. `margin_v` is NOT: it
    arrives already computed against the frame by _position_layout. A zero shadow
    stays zero rather than rounding up to one, or the flat box styles grow an edge
    they were designed without."""
    F = frame or (SHORTS_W, SHORTS_H)
    keep0 = lambda px: (_sz(px, F) if px else 0)
    primary = primary or preset["primary"]
    size = _sz(fontsize or preset["fontsize"], F)
    oc = preset.get("outline_colour", _BLACK)
    side = _sz(_SIDE_MARGIN, F)
    tail = _STYLE_FIELDS.format(bs=preset["border"], ol=keep0(preset["outline"]),
                                sh=keep0(preset["shadow"]),
                                al=align, ml=side, mr=side, mv=margin_v)
    return f"Style: {name},{font},{size},{primary},{primary},{oc},{preset['back']},{tail}"


def _inline_colour(ass_colour: str) -> str:
    """An ASS inline primary-colour override from an '&H00BBGGRR' style value.

    Style rows and inline overrides use different spellings of the same colour:
    the style takes '&H00BBGGRR', the override takes '\\1c&HBBGGRR&'. Mixing them
    up silently renders the default colour, so the conversion lives in one place.
    """
    v = (ass_colour or "").strip()
    if not v.startswith("&H"):
        return ""
    body = v[2:].rstrip("&")
    if len(body) == 8:           # AABBGGRR -> drop the alpha
        body = body[2:]
    if len(body) != 6:
        return ""
    return "{\\1c&H%s&}" % body.upper()


def _speaker_ranges(segments: list, lead_offset: float = 0.0) -> list:
    """[(start, end, speaker)] for segments that carry a speaker label."""
    out = []
    for s in segments or []:
        if s.get("speaker") is None:
            continue
        try:
            out.append((float(s.get("start", 0)) - lead_offset,
                        float(s.get("end", 0)) - lead_offset,
                        s.get("speaker")))
        except (TypeError, ValueError):
            continue
    return out


def _speaker_at(ranges: list, t: float):
    """Which speaker is talking at time t, or None.

    Strict containment FIRST. A cue starts exactly where the previous segment ended,
    so a tolerant match would hit the outgoing speaker and colour every turn's first
    cue as the person who just stopped talking — the captions would lag a turn behind
    the audio, which is worse than no colour at all.
    """
    for s, e, spk in ranges:
        if s <= t < e:
            return spk
    # Nothing contained it (gaps between segments) — now allow a small tolerance.
    best, best_gap = None, None
    for s, e, spk in ranges:
        gap = 0.0 if s <= t <= e else min(abs(t - s), abs(t - e))
        if gap <= 0.25 and (best_gap is None or gap < best_gap):
            best, best_gap = spk, gap
    return best


def _norm_xy(xy):
    """Accept {"x":0.5,"y":0.8} / (0.5,0.8) / [0.5,0.8] and return a clamped
    (x, y) fraction pair, or None. Anything malformed degrades to None so the
    caller falls back to the preset position instead of crashing a render."""
    if not xy:
        return None
    try:
        if isinstance(xy, dict):
            x, y = float(xy.get("x")), float(xy.get("y"))
        else:
            x, y = float(xy[0]), float(xy[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    # Keep text on-canvas: a centre this close to an edge still renders readably.
    x = min(0.98, max(0.02, x))
    y = min(0.98, max(0.02, y))
    return (x, y)


def _pos_tag(xy, frame: tuple = None) -> str:
    """ASS override that pins a line's CENTRE at a fraction of the 1080x1920 frame.

    Used for drag-and-drop placement. Pairs with a style whose Alignment is 5
    (middle-centre) so \\pos anchors at the text's centre rather than a corner."""
    p = _norm_xy(xy)
    if not p:
        return ""
    F = frame or (SHORTS_W, SHORTS_H)
    return "{\\pos(%d,%d)}" % (int(round(p[0] * F[0])), int(round(p[1] * F[1])))


def _text_key_for(language: str) -> str:
    return {"english": "text_en", "hinglish": "text_hinglish"}.get(language, "text")


def _esc_word(text: str) -> str:
    """Escape a single word for an ASS Dialogue Text (no brace->paren swap needed for
    plain words, but stay safe in case punctuation sneaks in)."""
    return (text or "").replace("\\", "\\\\").replace("{", "(").replace("}", ")").strip()


def _word_timings(seg: dict, text_key: str, lead_offset: float, clip_duration) -> list:
    """Return [(start, end, word)] for a segment. Uses REAL word timestamps when the
    Devanagari source has them; otherwise splits the chosen text evenly across the
    segment span (so animated styles still work for English/Hinglish)."""
    words_meta = [w for w in seg.get("words", [])
                  if (w.get("word") or "").strip() and w.get("start") is not None and w.get("end") is not None]
    out = []
    if words_meta and text_key == "text":
        for w in words_meta:
            s = max(0.0, w["start"] - lead_offset)
            e = max(0.0, w["end"] - lead_offset)
            if clip_duration is not None:
                e = min(e, clip_duration)
            if e > s:
                out.append((s, e, (w["word"] or "").strip()))
        return out
    text = (seg.get(text_key) or "").strip()
    if not text:
        return out
    s0 = max(0.0, float(seg["start"]) - lead_offset)
    e0 = max(0.0, float(seg["end"]) - lead_offset)
    if clip_duration is not None:
        e0 = min(e0, clip_duration)
    toks = text.split()
    if not toks or e0 <= s0:
        return out
    span = (e0 - s0) / len(toks)
    for i, t in enumerate(toks):
        out.append((s0 + i * span, s0 + (i + 1) * span, t))
    return out


def _all_word_timings(segments: list, text_key: str, lead_offset: float, clip_duration) -> list:
    words = []
    for seg in segments:
        words.extend(_word_timings(seg, text_key, lead_offset, clip_duration))
    return words


def _karaoke_events(segments, text_key, lead_offset, clip_duration, preset, group=3) -> list:
    """Hormozi-style: a short phrase stays on screen and the currently spoken word is
    recoloured + slightly enlarged. Returns [(start, end, ass_text)]."""
    words = _all_word_timings(segments, text_key, lead_offset, clip_duration)
    accent = preset["accent"]
    events = []
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        disp = [(w[2].upper() if preset["upper"] else w[2]) for w in chunk]
        for j, (s, e, _w) in enumerate(chunk):
            parts = []
            for k, word in enumerate(disp):
                wesc = _esc_word(word)
                if k == j:
                    parts.append(f"{{\\1c{accent}\\fscx116\\fscy116}}{wesc}{{\\r}}")
                else:
                    parts.append(wesc)
            events.append((s, e, " ".join(parts)))
    return events


def _wordpop_events(segments, text_key, lead_offset, clip_duration, preset, group=1) -> list:
    """Fast-cut Reels style: a group of words scales/fades in, and the word actually
    being spoken carries the highlight colour.

    At the default group of 1 this is the classic one-word-at-a-time look, and that
    single word is the highlight colour — which is what the UI preview has always
    shown, though the renderer used to ignore the colour entirely and burn plain
    white. Raise the group and the neighbours stay on screen in the caption colour
    while the highlight travels along them.

    Colours are written per word rather than with a trailing {\\r}: `\\r` resets to
    the style, which would also cancel the \\t() pop transform for every word after
    the highlighted one and leave half the group un-animated.
    """
    words = _all_word_timings(segments, text_key, lead_offset, clip_duration)
    accent, base = preset["accent"], preset["primary"]
    pop = "{\\fad(50,40)\\fscx72\\fscy72\\t(0,120,\\fscx100\\fscy100)}"
    events = []
    for i in range(0, len(words), group):
        chunk = words[i:i + group]
        disp = [(w[2].upper() if preset["upper"] else w[2]) for w in chunk]
        for j, (s, e, _w) in enumerate(chunk):
            parts = [f"{{\\1c{accent if k == j else base}}}{_esc_word(word)}"
                     for k, word in enumerate(disp)]
            # The pop leads the line so it governs the whole group; the per-word
            # colour tags ride after it and never touch the transform.
            events.append((s, e, pop + " ".join(parts)))
    return events


def _grapheme_clusters(text: str) -> list:
    """Split into what a reader calls "characters": a base plus every mark that hangs
    off it. Devanagari needs this — मा is one glyph but two code points, and revealing
    the base without its matra shows a different letter, not half of one."""
    out = []
    for ch in text or "":
        joins = (unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Mc")
                 or ch in "\u200d\u200c")
        if out and joins:
            out[-1] += ch
        else:
            out.append(ch)
    return out


def _motion_events(cues, preset, anim: str, frame: tuple) -> list:
    """Wrap plain cues in an ASS transform. These four animate the CUE, not the word,
    so they reuse the normal chunker and only differ in the override block.

    \\move and \\t are in real frame pixels, so the travel distance is scaled off the
    canvas — a 40px rise reads very differently on a 1080-tall frame than a 1920.
    Typewriter is the odd one: ASS cannot reveal text progressively, so it is built
    from one event per character with \\alpha holding the tail invisible."""
    rise = _sz(40, frame)
    out = []
    for s0, e0, text in cues:
        body = _ass_escape(text.upper() if preset["upper"] else text)
        if anim == "slide_up":
            # Relative rise: \move needs absolute coords, so use \t on \fry-free
            # origin shifting via \pos is not available here — fade + scale reads as
            # a rise without fighting the alignment the style already set.
            out.append((s0, e0, f"{{\\fad(90,60)\\t(0,160,\\fscy100)\\fscy88}}{body}"))
        elif anim == "bounce":
            out.append((s0, e0,
                        "{\\fad(40,40)\\fscx70\\fscy70"
                        "\\t(0,110,\\fscx112\\fscy112)"
                        f"\\t(110,190,\\fscx100\\fscy100)}}{body}"))
        elif anim == "punch":
            out.append((s0, e0,
                        "{\\fad(30,40)\\fscx150\\fscy150\\alpha&H60&"
                        f"\\t(0,90,\\fscx100\\fscy100\\alpha&H00&)}}{body}"))
        elif anim == "typewriter":
            # Cluster on the RAW text, then escape per step. Escaping first would let
            # a split land between a backslash and the character it escapes, and
            # splitting per Python character would tear Devanagari apart mid-syllable
            # (म + ा are one glyph to a reader, two code points to len()).
            raw = text.upper() if preset["upper"] else text
            units = _grapheme_clusters(raw)
            n = len(units)
            if not n:
                continue
            # Reveal across the first 60% of the cue, then hold the full line.
            span = max(0.0, (e0 - s0)) * 0.6
            step = (span / n) if n else 0
            for i in range(1, n + 1):
                cs = s0 + step * (i - 1)
                ce = (s0 + step * i) if i < n else e0
                if ce <= cs:
                    continue
                shown = _ass_escape("".join(units[:i]))
                hidden = _ass_escape("".join(units[i:]))
                tail = f"{{\\alpha&HFF&}}{hidden}" if hidden else ""
                out.append((cs, ce, f"{shown}{tail}"))
        else:
            out.append((s0, e0, body))
    return out


def _static_cues(segments: list, language: str, lead_offset: float, clip_duration,
                 max_words: int = _WORDS_PER_CUE_MAX):
    """Plain chunked cues (no animation) for the chosen language."""
    if language == "english":
        return _chunk_text_cues(segments, "text_en", lead_offset, clip_duration, max_words)
    if language == "hinglish":
        return _chunk_text_cues(segments, "text_hinglish", lead_offset, clip_duration, max_words)
    return _chunk_word_cues(segments, lead_offset, clip_duration, max_words)


def make_caption_ass(segments: list, ass_path: str,
                     layout: str = "single",
                     language: str = "hindi",
                     position: str = "bottom",
                     caption_style: str = "outline",
                     accent_color: str = "",
                     caption_color: str = "",
                     caption_words: int = 0,
                     aspect: str = DEFAULT_ASPECT,
                     fit: str = "fit",
                     title: str = "",
                     show_title: bool = False,
                     hindi_font: str = "Noto Sans Devanagari",
                     latin_font: str = "Poppins",
                     title_font: str = "",
                     title_style: str = "",
                     title_color: str = "",
                     lead_offset: float = 0.08,
                     clip_duration: float = None,
                     video_box: tuple = None,
                     part_label: str = "",
                     show_part_label: bool = False,
                     caption_xy=None,
                     title_xy=None,
                     part_xy=None,
                     speaker_colours=None,
                     caption_size: float = CAPTION_SIZE_DEFAULT,
                     caption_xy_en=None,
                     position_hi: str = "",
                     position_en: str = "") -> tuple:
    """Write ONE ASS file on a 1080x1920 frame.

    layout == "single": ONE caption track in `language`, pinned to `position`, styled
                        by `caption_style` (outline / box / bold_yellow / karaoke /
                        word_pop). Animated styles (karaoke, word_pop) light up or pop
                        word-by-word; `accent_color` ('#RRGGBB') tints the active word.
    layout == "dual"  : the classic two-track look — Devanagari Hindi on TOP and the
                        English translation on the BOTTOM (uses a static style).

    caption_color ('#RRGGBB') repaints the caption TEXT for any style, leaving the
    slab/outline that gives a style its identity alone. caption_words is how many
    words may share the screen (1-8); 0 keeps each style's own default.

    show_title adds a static headline; show_part_label adds the "Part 3" badge used
    by sequential mode.

    caption_xy / title_xy / part_xy are optional {"x":0..1,"y":0..1} fractions from
    the UI's drag-and-drop editor. When given they override the preset position for
    that overlay and pin its centre exactly there.

    Returns (primary_cue_count, secondary_cue_count, has_title).
    """
    position = position if position in _POS_ALIGN else "bottom"
    title = (title or "").strip()
    part_label = (part_label or "").strip()
    accent = _hex_to_ass(accent_color) if accent_color else _DEFAULT_ACCENT
    # The canvas every measurement in this file is taken against.
    FW, FH = _frame(aspect)
    F = (FW, FH)
    # Words allowed on screen at once, and the user's caption-text colour. The colour
    # is applied to a preset AFTER _style_preset built it, so a style keeps its own
    # slab, outline and size and only its text is repainted.
    words_on_screen = _resolve_words_on_screen(caption_words, caption_style)

    def _recolour(preset: dict) -> dict:
        if caption_color:
            preset["primary"] = _hex_to_ass(caption_color, preset["primary"])
        return preset

    styles = []
    events = []

    # Podcast mode: {speaker_id: '&H00BBGGRR'} so each voice gets its own colour.
    spk_colours = speaker_colours or {}
    spk_ranges = _speaker_ranges(segments, lead_offset) if spk_colours else []

    def spk_prefix(cue_start, cue_end=None):
        """Colour override for whoever is speaking at this cue.

        Sampled at the cue's MIDPOINT, not its leading edge: the edge sits exactly
        on a turn boundary and is ambiguous, the middle never is."""
        if not spk_ranges:
            return ""
        t = cue_start if cue_end is None else (cue_start + cue_end) / 2.0
        who = _speaker_at(spk_ranges, t)
        if who is None:
            return ""
        return _inline_colour(spk_colours.get(who, ""))

    try:
        csize = max(CAPTION_SIZE_MIN, min(CAPTION_SIZE_MAX, float(caption_size)))
    except (TypeError, ValueError):
        csize = CAPTION_SIZE_DEFAULT

    def _cap(px):
        """A caption font size with the user's multiplier applied."""
        return max(10, int(round(px * csize)))

    cap_xy = _norm_xy(caption_xy)
    ttl_xy = _norm_xy(title_xy)
    prt_xy = _norm_xy(part_xy)
    full_end = clip_duration if clip_duration else 3600

    # The part badge stays on this crisp caps frame whatever the headline does.
    title_preset = _style_preset("bold_yellow", accent)

    # The headline can wear any caption style and any English font. Leaving both
    # unset reproduces the original look exactly, which is NOT simply the
    # bold_yellow style: the headline used its own warmer yellow (_TITLE_COLOUR)
    # over the bold_yellow frame, so that pairing stays the default.
    if title_style:
        head_preset = _style_preset(title_style, accent)
        head_primary = head_preset["primary"]
    else:
        head_preset, head_primary = title_preset, _TITLE_COLOUR
    # An explicit headline colour beats whatever the style (or the default) chose.
    if title_color:
        head_primary = _hex_to_ass(title_color, head_primary)
    head_font = title_font or latin_font
    head_text = _headline_text(title, head_preset["upper"])

    def add_title(default_pos: str):
        if not (show_title and title):
            return False
        if ttl_xy:
            # Free placement: centre-anchored style + a per-line \pos override.
            styles.append(_style_row("TITLE", head_font, head_preset, 5, 0,
                                      primary=head_primary, fontsize=_TITLE_FONTSIZE, frame=F))
            prefix = _pos_tag(ttl_xy, F)
        else:
            align, mv = _position_layout(default_pos, video_box, F)
            styles.append(_style_row("TITLE", head_font, head_preset, align, mv,
                                      primary=head_primary, fontsize=_TITLE_FONTSIZE, frame=F))
            prefix = ""
        events.append(f"Dialogue: 0,{_fmt_ass_time(0)},{_fmt_ass_time(full_end)},TITLE,,0,0,0,,"
                      f"{prefix}{head_text}")
        return True

    def add_part_label():
        """The 'Part 3' badge for sequential mode. Defaults to the top of the frame,
        clear of both caption positions, unless the user dragged it somewhere."""
        if not (show_part_label and part_label):
            return False
        if prt_xy:
            styles.append(_style_row("PARTNO", latin_font, title_preset, 5, 0,
                                      primary=_WHITE, fontsize=_PART_FONTSIZE, frame=F))
            prefix = _pos_tag(prt_xy, F)
        else:
            styles.append(_style_row("PARTNO", latin_font, title_preset, 8,
                                      _sz(_PART_DEFAULT_MARGIN, F),
                                      primary=_WHITE, fontsize=_PART_FONTSIZE, frame=F))
            prefix = ""
        events.append(f"Dialogue: 0,{_fmt_ass_time(0)},{_fmt_ass_time(full_end)},PARTNO,,0,0,0,,"
                      f"{prefix}{_ass_escape(part_label.upper())}")
        return True

    if layout == "dual":
        # Classic two-track look — animated styles don't apply here, fall back to static.
        dual_name = caption_style if caption_style in _STATIC_STYLES else "outline"
        dpreset = _recolour(_style_preset(dual_name, accent))
        # Resolve the word count against the style that will ACTUALLY be drawn. An
        # animated pick falls back to outline here, and inheriting word_pop's auto
        # of 1 would silently cut a dual track down to one word per line.
        dual_words = _resolve_words_on_screen(caption_words, dual_name)
        hi_cues = _chunk_word_cues(segments, lead_offset, clip_duration, dual_words)
        en_cues = _chunk_text_cues(segments, "text_en", lead_offset, clip_duration,
                                   dual_words)
        # Dragging the caption moves the PAIR: the Hindi line sits at the chosen
        # point and the English line tucks just beneath it, keeping the stacked look.
        # The two tracks are positioned INDEPENDENTLY: a dragged point or a preset
        # for each. Falling back, the classic stacked look is preserved — Hindi up
        # top, English underneath — so a job that never touches these looks the same
        # as it always did.
        en_xy = _norm_xy(caption_xy_en)
        p_hi = position_hi if position_hi in _POS_ALIGN else ""
        p_en = position_en if position_en in _POS_ALIGN else ""

        if cap_xy:
            hi_align, hi_mv, hi_prefix = 5, 0, _pos_tag(cap_xy, F)
        elif p_hi:
            hi_align, hi_mv = _position_layout(p_hi, video_box, F)
            hi_prefix = ""
        else:
            hi_align, hi_mv, hi_prefix = 8, _sz(90, F), ""

        if en_xy:
            en_align, en_mv, en_prefix = 5, 0, _pos_tag(en_xy, F)
        elif p_en:
            en_align, en_mv = _position_layout(p_en, video_box, F)
            en_prefix = ""
        elif cap_xy:
            # Only the pair was dragged — keep English tucked under Hindi.
            en_align, en_mv = 5, 0
            en_prefix = _pos_tag((cap_xy[0], min(0.98, cap_xy[1] + _DUAL_GAP_FRAC)), F)
        else:
            en_align, en_mv, en_prefix = 2, _sz(150, F), ""

        styles.append(_style_row("HI", hindi_font, dpreset, hi_align, hi_mv,
                                 fontsize=_cap(_HI_FONTSIZE), frame=F))
        styles.append(_style_row("EN", latin_font, dpreset, en_align, en_mv,
                                 fontsize=_cap(_EN_FONTSIZE), frame=F))
        has_title = False
        if show_title and title:
            if ttl_xy:
                styles.append(_style_row("TITLE", head_font, head_preset, 5, 0,
                                          primary=head_primary, fontsize=_TITLE_FONTSIZE, frame=F))
                t_prefix = _pos_tag(ttl_xy, F)
            else:
                styles.append(_style_row("TITLE", head_font, head_preset, 8, _sz(270, F),
                                          primary=head_primary, fontsize=_TITLE_FONTSIZE, frame=F))
                t_prefix = ""
            events.append(f"Dialogue: 0,{_fmt_ass_time(0)},{_fmt_ass_time(full_end)},TITLE,,0,0,0,,"
                          f"{t_prefix}{head_text}")
            has_title = True
        up = dpreset["upper"]
        for c_start, c_end, text in hi_cues:
            events.append(f"Dialogue: 0,{_fmt_ass_time(c_start)},{_fmt_ass_time(c_end)},HI,,0,0,0,,"
                          f"{hi_prefix}{spk_prefix(c_start, c_end)}{_ass_escape(text)}")
        for c_start, c_end, text in en_cues:
            t = text.upper() if up else text
            events.append(f"Dialogue: 0,{_fmt_ass_time(c_start)},{_fmt_ass_time(c_end)},EN,,0,0,0,,{en_prefix}{_ass_escape(t)}")
        add_part_label()
        primary_count, secondary_count = len(hi_cues), len(en_cues)
    else:
        # Single track: one chosen language at one position, in the chosen style.
        preset = _recolour(_style_preset(caption_style, accent))
        text_key = _text_key_for(language)
        font = hindi_font if language == "hindi" else latin_font
        if cap_xy:
            # Free placement: centre-anchored style, exact \pos per line.
            styles.append(_style_row("SUB", font, preset, 5, 0,
                                     fontsize=_cap(preset["fontsize"]), frame=F))
            cap_prefix = _pos_tag(cap_xy, F)
        else:
            sub_align, sub_mv = _position_layout(position, video_box, F)
            styles.append(_style_row("SUB", font, preset, sub_align, sub_mv,
                                     fontsize=_cap(preset["fontsize"]), frame=F))
            cap_prefix = ""

        if preset["anim"] == "karaoke":
            ev = _karaoke_events(segments, text_key, lead_offset, clip_duration, preset,
                                 group=words_on_screen)
        elif preset["anim"] == "word_pop":
            ev = _wordpop_events(segments, text_key, lead_offset, clip_duration, preset,
                                 group=words_on_screen)
        elif preset["anim"] in ("slide_up", "bounce", "typewriter", "punch"):
            cues = _static_cues(segments, language, lead_offset, clip_duration, words_on_screen)
            ev = _motion_events(cues, preset, preset["anim"], F)
        elif preset["anim"] == "fade":
            cues = _static_cues(segments, language, lead_offset, clip_duration, words_on_screen)
            ev = [(s, e, f'{{\\fad(120,80)}}{_ass_escape(t.upper() if preset["upper"] else t)}')
                  for s, e, t in cues]
        else:
            cues = _static_cues(segments, language, lead_offset, clip_duration, words_on_screen)
            ev = [(s, e, _ass_escape(t.upper() if preset["upper"] else t)) for s, e, t in cues]

        # Title goes opposite the captions to avoid overlap.
        title_pos = "below" if position == "top" else "top"
        has_title = add_title(title_pos)
        add_part_label()
        # \pos must lead the line; animated styles then append their own override
        # blocks after it, which ASS applies cumulatively.
        for s, e, text in ev:
            events.append(f"Dialogue: 0,{_fmt_ass_time(s)},{_fmt_ass_time(e)},SUB,,0,0,0,,"
                          f"{cap_prefix}{spk_prefix(s, e)}{text}")
        primary_count, secondary_count = len(ev), 0

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {FW}\nPlayResY: {FH}\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(events))
        f.write("\n")

    return primary_count, secondary_count, has_title



# ─────────────────────────────────────────────────────────────
# FFmpeg render filter builder (9:16 Shorts canvas + dual subtitles)
# ─────────────────────────────────────────────────────────────

RENDER_TIMEOUT = int(os.environ.get("RENDER_TIMEOUT", "1800"))

def _escape_ffmpeg_path(path: str) -> str:
    path = path.replace("\\", "/")
    path = path.replace("'", "\\'")
    path = path.replace(":", "\\:")
    return path


def _build_render_filter(ass_path: str, fontsdir: str = "", frame: tuple = None,
                         fit: str = "fit") -> str:
    """Full -vf chain for one finished clip:
       1. put the source on the chosen canvas, either
          fit  — scaled down to fit whole, centred, black bars where it falls short
          fill — scaled up to cover, then cropped, so no bars but the edges are lost
       2. burn the ASS on top
    `fontsdir` should point at the bundled Devanagari font folder; the Latin font
    (Poppins) is resolved from system fonts via fontconfig.
    """
    W, H = frame or (SHORTS_W, SHORTS_H)
    # bilinear downscaling is noticeably cheaper than the default bicubic with
    # negligible quality loss at this resolution; override with SCALE_FLAGS if needed.
    scale_flags = os.environ.get("SCALE_FLAGS", "bilinear")
    if fit == "fill":
        chain = [
            f"scale={W}:{H}:force_original_aspect_ratio=increase:flags={scale_flags}",
            f"crop={W}:{H}",
        ]
    else:
        chain = [
            f"scale={W}:{H}:force_original_aspect_ratio=decrease:flags={scale_flags}",
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black",
        ]
    chain.append("setsar=1")
    if ass_path:
        ass_esc = _escape_ffmpeg_path(ass_path)
        if fontsdir and os.path.isdir(fontsdir):
            chain.append(f"subtitles='{ass_esc}':fontsdir='{_escape_ffmpeg_path(fontsdir)}'")
        else:
            chain.append(f"subtitles='{ass_esc}'")
    return ",".join(chain)


# ── Logo / watermark ──────────────────────────────────────────────────────────
# A PNG the user uploads, scaled to a share of the frame width and pinned wherever
# they dropped it. Drawn LAST so it sits above the captions, which is what a
# watermark is for.

LOGO_MIN_SCALE, LOGO_MAX_SCALE = 0.03, 0.60
LOGO_DEFAULT_SCALE = 0.18
LOGO_DEFAULT_XY = (0.84, 0.07)      # top-right, clear of the caption band

# The bundled Piksy mark, burned on every clip unless the user turns it off or
# supplies their own logo. It is what makes a reposted clip traceable back here,
# so it is ON by default — but it is a default, never a lock.
# Resolved from __file__ rather than BASE_DIR: that constant is defined further
# down this module, so referencing it here fails at import time.
PIKSY_WATERMARK = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "piksy_watermark.png")
PIKSY_WATERMARK_OPACITY = 0.40      # visible, but never competing with the content
PIKSY_WATERMARK_SCALE = 0.16


def _norm_logo(cfg: dict) -> dict:
    """Validate the logo settings, or return {} when there is no usable logo.

    Returns {path, scale, x, y, opacity} with everything clamped, so the filter
    builder can trust its input."""
    path = str((cfg or {}).get("logo_path", "") or "").strip()
    own_logo = bool(path) and os.path.isfile(path)

    # No logo of their own -> fall back to the Piksy watermark, unless it was
    # explicitly switched off. A user logo always wins; the two never stack.
    if not own_logo:
        if not (cfg or {}).get("piksy_watermark", True):
            return {}
        if not os.path.isfile(PIKSY_WATERMARK):
            return {}
        xy = _norm_xy((cfg or {}).get("logo_xy")) or LOGO_DEFAULT_XY
        return {
            "path": PIKSY_WATERMARK,
            "scale": PIKSY_WATERMARK_SCALE,
            "x": xy[0], "y": xy[1],
            "opacity": PIKSY_WATERMARK_OPACITY,
            "is_watermark": True,
        }

    def _f(key, default, lo, hi):
        try:
            return max(lo, min(hi, float((cfg or {}).get(key, default))))
        except (TypeError, ValueError):
            return default

    xy = _norm_xy((cfg or {}).get("logo_xy")) or LOGO_DEFAULT_XY
    return {
        "path": path,
        "scale": _f("logo_scale", LOGO_DEFAULT_SCALE, LOGO_MIN_SCALE, LOGO_MAX_SCALE),
        "x": xy[0],
        "y": xy[1],
        "opacity": _f("logo_opacity", 1.0, 0.05, 1.0),
    }


def _logo_graph(logo: dict, logo_idx: int, src_label: str, out_label: str,
                frame: tuple = None) -> str:
    """Scale the logo to `scale` x frame width (height auto, aspect kept) and pin its
    CENTRE at (x, y) as fractions of the 1080x1920 frame — the same coordinate space
    the drag-and-drop editor uses for the other overlays."""
    W, H = frame or (SHORTS_W, SHORTS_H)
    width = max(1, int(round(W * logo["scale"])))
    # force_original_aspect_ratio is not needed with -1 height, but rounding to even
    # keeps yuv420p happy if the logo is ever re-encoded rather than composited.
    steps = [f"scale={width}:-2:flags=bicubic", "format=rgba"]
    if logo["opacity"] < 0.999:
        steps.append(f"colorchannelmixer=aa={logo['opacity']:.3f}")
    x = f"{W}*{logo['x']:.4f}-overlay_w/2"
    y = f"{H}*{logo['y']:.4f}-overlay_h/2"
    return (f"[{logo_idx}:v]{','.join(steps)}[lg];"
            f"[{src_label}][lg]overlay=x='{x}':y='{y}':format=auto[{out_label}]")


def _build_hook_filter_complex(vf_chain: str, hook_idx: int) -> tuple:
    """filter_complex for a hook-first render: [hook] then [full clip], concatenated,
    then scaled to 9:16 and captioned in the SAME single pass.

    The hook arrives as its OWN input — a second -ss/-to seek into the same source,
    which the caller adds at index `hook_idx` — rather than a split+trim of one
    decoded input.

    That is not a style choice, it is the whole reason this function exists in this
    shape. concat must drain segment 1 to EOF before it reads a single frame of
    segment 2, and `trim` does not signal EOF upstream early: it only ends when its
    input does. So `split -> trim` forced ffmpeg to decode the ENTIRE clip to finish
    the 4-second hook, while every one of those frames was also handed to the body
    branch, where concat was not yet listening. They queued, undecimated, in an
    unbounded filter FIFO. One 57s 854x480 clip peaked at 10.5 GB; six render in
    parallel, so the OOM killer took the whole app down. Seeking the source twice
    decodes the hook range twice — a rounding error next to that — and holds the
    same clip at 780 MB, bit-for-bit identical output.

    Returns (filter_complex, video_label, audio_label).
    """
    graph = (
        f"[{hook_idx}:v]setpts=PTS-STARTPTS[hv];"
        f"[{hook_idx}:a]asetpts=PTS-STARTPTS[ha];"
        "[0:v]setpts=PTS-STARTPTS[bv];"
        "[0:a]asetpts=PTS-STARTPTS[ba];"
        "[hv][ha][bv][ba]concat=n=2:v=1:a=1[cv][ca];"
        f"[cv]{vf_chain}[vout]"
    )
    return graph, "[vout]", "[ca]"


def _filter_args(vf_chain: str, hook_idx, logo: dict, logo_idx, frame: tuple = None) -> list:
    """The ffmpeg arguments that turn the decoded input(s) into the finished frame.

    `hook_idx` / `logo_idx` are the input indices the caller assigned to the hook
    seek and the logo PNG, or None when that input is absent.

    Four shapes, cheapest first — a plain -vf is kept whenever nothing else is
    needed, so the common path never pays for a filter graph it does not use:

        no hook, no logo   -vf <chain>
        hook, no logo      concat the cold open, then <chain>
        no hook, logo      <chain>, then composite the PNG on top
        hook + logo        concat, then <chain>, then composite

    The logo is always last so it sits above the captions.
    """
    if hook_idx is None and not logo:
        return ["-vf", vf_chain]

    parts, vlabel, alabel = [], None, None

    if hook_idx is not None:
        graph, vlabel, alabel = _build_hook_filter_complex(vf_chain, hook_idx)
        parts.append(graph)
    else:
        parts.append(f"[0:v]{vf_chain}[base]")
        # "0:a?" — optional, so a silent source renders video-only instead of
        # failing the whole clip. The hook path cannot do this: its concat needs a
        # real audio stream, which is why dropping the hook is a ladder rung.
        vlabel, alabel = "[base]", "0:a?"

    if logo:
        parts.append(_logo_graph(logo, logo_idx, vlabel.strip("[]"), "vlogo", frame))
        vlabel = "[vlogo]"

    return ["-filter_complex", ";".join(parts), "-map", vlabel, "-map", alabel]


# ─────────────────────────────────────────────────────────────
# Fonts
# ─────────────────────────────────────────────────────────────
# IMPORTANT: For Devanagari (Hindi) captions to render, a Devanagari-capable font
# MUST be reachable. We search the bundled fonts/ folder first, then several common
# system locations. If NONE is found, Hindi captions fall back to the renderer's
# default font and may show as empty boxes — the logs will warn loudly in that case.
# (This is the bug that caused "subtitles didn't load" when the font file was missing.)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# (file_path, font_family_name) — family name is what the ASS style references.
_HINDI_FONT_CANDIDATES = [
    (os.path.join(BASE_DIR, "fonts", "NotoSansDevanagari", "full", "ttf", "NotoSansDevanagari-Bold.ttf"), "Noto Sans Devanagari"),
    (os.path.join(BASE_DIR, "fonts", "NotoSansDevanagari-Bold.ttf"), "Noto Sans Devanagari"),
    (os.path.join(BASE_DIR, "fonts", "NotoSansDevanagari-Regular.ttf"), "Noto Sans Devanagari"),
    (os.path.join(BASE_DIR, "fonts", "Baloo2-Bold.ttf"), "Baloo 2"),
    (os.path.join(BASE_DIR, "fonts", "Laila-Bold.ttf"), "Laila"),
    (os.path.join(BASE_DIR, "fonts", "Rajdhani-Bold.ttf"), "Rajdhani"),
    ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf", "Noto Sans Devanagari"),
    ("/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf", "Noto Sans Devanagari"),
    ("/usr/share/fonts/truetype/lohit-devanagari/Lohit-Devanagari.ttf", "Lohit Devanagari"),
    ("/usr/share/fonts/truetype/Sarai/Sarai.ttf", "Sarai"),
]
_LATIN_FONT_CANDIDATES = [
    (os.path.join(BASE_DIR, "fonts", "Poppins-Bold.ttf"), "Poppins"),
    (os.path.join(BASE_DIR, "fonts", "Oswald-Bold.ttf"), "Oswald"),
    (os.path.join(BASE_DIR, "fonts", "Montserrat-Bold.ttf"), "Montserrat"),
    (os.path.join(BASE_DIR, "fonts", "Staatliches-Regular.ttf"), "Staatliches"),
    (os.path.join(BASE_DIR, "fonts", "BarlowCondensed-Bold.ttf"), "Barlow Condensed"),
    (os.path.join(BASE_DIR, "fonts", "Righteous-Regular.ttf"), "Righteous"),
    ("/usr/share/fonts/truetype/google-fonts/Poppins-Bold.ttf", "Poppins"),
    ("/usr/share/fonts/truetype/poppins/Poppins-Bold.ttf", "Poppins"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu Sans"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVu Sans"),
]


def _resolve_font(candidates):
    """Return (file_path, family_name) for the first candidate that exists, else ('','')."""
    for path, family in candidates:
        if path and os.path.exists(path):
            return path, family
    return "", ""


def _get_hindi_font():
    """(path, family) for a Devanagari font, or ('','') if none is available."""
    return _resolve_font(_HINDI_FONT_CANDIDATES)


def _get_latin_font():
    """(path, family) for a Latin font, or ('','') if none is available."""
    return _resolve_font(_LATIN_FONT_CANDIDATES)


# ── User-selectable caption fonts ──────────────────────────────────────────
# key -> (family name used in the ASS style, filename expected under ./fonts/).
# Download them with the helper in the README (fetch_fonts script). If a chosen
# font file isn't present, we fall back to whatever Devanagari/Latin font we can find.
HINDI_FONTS = {
    "noto":     ("Noto Sans Devanagari", "NotoSansDevanagari-Bold.ttf"),
    "mukta":    ("Mukta",                "Mukta-Bold.ttf"),
    "hind":     ("Hind",                 "Hind-Bold.ttf"),
    "rozha":    ("Rozha One",            "RozhaOne-Regular.ttf"),
    "kalam":    ("Kalam",                "Kalam-Bold.ttf"),
    "baloo":    ("Baloo 2",              "Baloo2-Bold.ttf"),
    "laila":    ("Laila",                "Laila-Bold.ttf"),
    "rajdhani": ("Rajdhani",             "Rajdhani-Bold.ttf"),
    "khand":    ("Khand",                "Khand-Bold.ttf"),
    "teko":     ("Teko",                 "Teko-Bold.ttf"),
}
ENGLISH_FONTS = {
    "poppins":     ("Poppins",            "Poppins-Bold.ttf"),
    "anton":       ("Anton",              "Anton-Regular.ttf"),
    "bebas":       ("Bebas Neue",         "BebasNeue-Regular.ttf"),
    "archivo":     ("Archivo Black",      "ArchivoBlack-Regular.ttf"),
    "fjalla":      ("Fjalla One",         "FjallaOne-Regular.ttf"),
    "oswald":      ("Oswald",             "Oswald-Bold.ttf"),
    "montserrat":  ("Montserrat",         "Montserrat-Bold.ttf"),
    "staatliches": ("Staatliches",        "Staatliches-Regular.ttf"),
    "barlow":      ("Barlow Condensed",   "BarlowCondensed-Bold.ttf"),
    "righteous":   ("Righteous",          "Righteous-Regular.ttf"),
}
VALID_HINDI_FONTS = tuple(HINDI_FONTS.keys())
VALID_ENGLISH_FONTS = tuple(ENGLISH_FONTS.keys())


def available_fonts(table: dict) -> dict:
    """The subset of `table` whose .ttf is actually on disk.

    Offering a font the renderer cannot load is worse than not offering it: the
    preview shows one typeface and libass silently substitutes another. The UI is
    built from this, so a font that failed to download is never selectable."""
    return {k: v for k, v in table.items()
            if os.path.isfile(os.path.join(BASE_DIR, "fonts", v[1]))}


def _font_from_choice(choice, table, fallback_resolver):
    """Resolve a user's font choice to (path, family).
    Looks for the chosen font's file under ./fonts/; if it isn't there, falls back
    to the first auto-detected font (so a bad/missing choice never breaks rendering)."""
    entry = table.get((choice or "").strip().lower())
    if entry:
        family, fname = entry
        for cand in (os.path.join(BASE_DIR, "fonts", fname),
                     os.path.join(BASE_DIR, "fonts", family.replace(" ", ""), fname)):
            if os.path.exists(cand):
                return cand, family
    return fallback_resolver()


def _prepare_fontsdir(font_paths, job_dir: str) -> str:
    """ffmpeg's subtitles filter takes a SINGLE fontsdir. If the fonts we need live
    in different folders (e.g. bundled Devanagari + system Latin), copy them into one
    per-job cache dir and return that. Returns '' if no bundled fonts were found."""
    paths = [p for p in font_paths if p and os.path.exists(p)]
    if not paths:
        return ""
    dirs = {os.path.dirname(p) for p in paths}
    if len(dirs) == 1:
        return dirs.pop()
    cache = os.path.join(job_dir, ".fonts")
    os.makedirs(cache, exist_ok=True)
    for p in paths:
        dst = os.path.join(cache, os.path.basename(p))
        if not os.path.exists(dst):
            try:
                shutil.copy2(p, dst)
            except OSError:
                pass
    return cache


# ─────────────────────────────────────────────────────────────
# Subtitle burning for one clip
# ─────────────────────────────────────────────────────────────

def burn_subtitles_for_clip(raw_path: str, clip_index: int, job_dir: str, clips_dir: str,
                             log: DiagnosticLog, clip_callback=None, reason: str = "",
                             clip_start: float = None, clip_end: float = None,
                             segments: list = None, title: str = "",
                             burn: bool = True,
                             layout: str = "single",
                             language: str = "hindi",
                             position: str = "bottom",
                             caption_style: str = "outline",
                             accent_color: str = "",
                             caption_color: str = "",
                             caption_words: int = 0,
                             title_font_choice: str = "",
                             title_style: str = "",
                             title_color: str = "",
                             aspect: str = DEFAULT_ASPECT,
                             fit: str = "fit",
                             hindi_font_choice: str = "",
                             english_font_choice: str = "",
                             show_title: bool = False,
                             src_dims: tuple = None,
                             part_label: str = "",
                             show_part_label: bool = False,
                             caption_xy=None,
                             title_xy=None,
                             part_xy=None,
                             speaker_colours=None,
                             caption_size: float = CAPTION_SIZE_DEFAULT,
                             caption_xy_en=None,
                             position_hi: str = "",
                             position_en: str = "",
                             hook_start: float = None,
                             hook_end: float = None,
                             logo: dict = None) -> str:
    """Renders ONE finished 9:16 Short in a single ffmpeg pass.

    `raw_path` is the SOURCE video; we seek into it with -ss/-to instead of cutting a
    separate raw clip first.

    burn=False           -> just cut + scale to clean 9:16, NO captions at all.
    layout="single"      -> one caption track in `language` at `position`.
    layout="dual"        -> Devanagari Hindi top + English bottom (classic look).
    caption_style        -> "outline" (text + outline) or "box" (solid background).
    show_title           -> add the static AI headline overlay.
    hook_start/hook_end  -> ABSOLUTE source times of a peak moment inside this clip.
                            When given, that window is spliced onto the FRONT as a
                            cold open and the clip then plays in full, so the peak
                            is seen twice. Captions are remapped to match.
    """
    # Sequential parts are named part_1.mp4, part_2.mp4 … so the running order is
    # obvious in the file manager and when uploading a series.
    stem = f"part_{clip_index}" if part_label else f"viral_clip_{clip_index}"
    final_output = os.path.join(clips_dir, f"{stem}.mp4")

    log.log(f"\n   Clip {clip_index}  [{clip_start:.2f}s–{clip_end:.2f}s]"
            if clip_start is not None else f"\n   Clip {clip_index}")
    log.log(f"     Source : {raw_path}")
    log.log(f"     Captions: burn={burn} layout={layout} lang={language} pos={position} "
            f"style={caption_style} title={show_title}")
    if part_label:
        log.log(f"     Part   : {part_label} (badge={'on' if show_part_label else 'off'})")
    log.log(f"     Output : {final_output}")

    if not os.path.exists(raw_path):
        log.log(f"     FAILED - source video does not exist")
        return None

    # Every ASS written for this clip, so the finally block can clean up both the
    # hooked and un-hooked versions when the ladder had to rebuild.
    ass_paths = []
    _rebuild_vf_without_hook = None

    try:
        clip_duration = (clip_end - clip_start) if (clip_start is not None and clip_end is not None) else None
        vf = None
        FRAME = _frame(aspect)
        log.log(f"     Canvas : {aspect} {FRAME[0]}x{FRAME[1]} ({fit})")

        # ── Hook-first: convert the absolute hook window to clip-local seconds ──
        # The render input-seeks to clip_start, so everything past this point works
        # in clip-local time. An unusable window (outside the clip, or too short to
        # register) simply disables the hook for this clip rather than failing it.
        hook_local = None
        if (hook_start is not None and hook_end is not None
                and clip_start is not None and clip_end is not None and clip_duration):
            hs = max(0.0, min(float(hook_start) - clip_start, clip_duration))
            he = max(0.0, min(float(hook_end) - clip_start, clip_duration))
            if he - hs >= 0.5:
                hook_local = (hs, he)
            else:
                log.log(f"     Hook window {hs:.2f}–{he:.2f}s is unusable — no cold open.")

        # Watermark PNG, already validated and clamped by the caller.
        logo_cfg = logo or {}
        if logo_cfg:
            log.log(f"     Logo   : {os.path.basename(logo_cfg['path'])} at "
                    f"{logo_cfg['scale']*100:.0f}% width, "
                    f"({logo_cfg['x']:.2f}, {logo_cfg['y']:.2f}), "
                    f"opacity {logo_cfg['opacity']:.2f}")

        hook_len = (hook_local[1] - hook_local[0]) if hook_local else 0.0
        # Captions and the title overlay must span the CONCATENATED timeline, which is
        # longer than the clip by exactly the hook.
        render_duration = (clip_duration + hook_len) if clip_duration is not None else None
        if hook_local:
            log.log(f"     Hook   : {hook_len:.1f}s cold open from +{hook_local[0]:.1f}s "
                    f"(final length {render_duration:.1f}s)")

        # The part badge and a fixed series title are independent of captions: a
        # sequential job with subtitles OFF still needs "Part 3" burned on. So the
        # overlay path runs whenever ANY of the three overlays is wanted.
        want_badge = bool(show_part_label and part_label)
        want_title = bool(show_title and title)
        overlay_only = (not burn) and (want_badge or want_title)

        if not burn and not overlay_only:
            # ── No-subtitle path: clean 9:16 video, nothing overlaid. ──
            log.log("     Subtitles OFF -> rendering clean 9:16 clip (no captions).")
            vf = _build_render_filter("", "", FRAME, fit)
        elif overlay_only:
            # ── Badge/title only: no transcript needed, so nothing is transcribed. ──
            log.log(f"     Subtitles OFF -> overlay-only render "
                    f"(part={'yes' if want_badge else 'no'}, title={'yes' if want_title else 'no'}).")
            _la_path, _la_family = _font_from_choice(english_font_choice, ENGLISH_FONTS, _get_latin_font)
            # The headline may use a different face than the captions, so its file
            # has to reach the same fontsdir or libass silently substitutes.
            _ti_path, _ti_family = _font_from_choice(title_font_choice, ENGLISH_FONTS, _get_latin_font)
            fontsdir = _prepare_fontsdir([_la_path, _ti_path], job_dir)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".ass", delete=False,
                dir=job_dir, prefix=f"clip{clip_index}_", encoding="utf-8"
            ) as tmp:
                ass_path = tmp.name
            ass_paths.append(ass_path)
            primary, secondary, has_title = make_caption_ass(
                [], ass_path,
                layout="single", language="english", position=position,
                caption_style=caption_style, accent_color=accent_color,
                caption_color=caption_color, caption_words=caption_words,
                title=title, show_title=show_title,
                hindi_font="Noto Sans Devanagari",
                latin_font=_la_family or "Poppins",
                title_font=_ti_family or "", title_style=title_style,
                title_color=title_color,
                clip_duration=render_duration,
                video_box=_video_box(*(src_dims if src_dims and src_dims[0]
                                       else _probe_dimensions(raw_path, log)),
                                     frame=FRAME, fit=fit),
                aspect=aspect, fit=fit,
                part_label=part_label, show_part_label=show_part_label,
                caption_xy=caption_xy, title_xy=title_xy, part_xy=part_xy,
                speaker_colours=speaker_colours,
            )
            vf = _build_render_filter(ass_path, fontsdir, FRAME, fit)
        else:
            # 1. Ensure we have clip-local segments (with whatever language fields we need).
            if segments is None:
                data = transcribe_clip(None, job_dir, clip_index, log,
                                       clip_start=clip_start, clip_end=clip_end, translate=True)
                segments = data.get("segments", [])

            # Hook-first: fold the replayed peak into the caption timeline. Done after
            # transcription/translation so every language field comes along for free.
            # The original list is kept for the ladder rung that drops the hook.
            base_segments = segments or []
            if hook_local and segments:
                segments = _remap_segments_for_hook(segments, hook_local[0], hook_local[1])

            # 2. Resolve fonts. Hindi (Devanagari) is the one that breaks if missing.
            need_hindi = (layout == "dual") or (layout == "single" and language == "hindi")
            need_latin = (layout == "dual") or (layout == "single" and language in ("english", "hinglish")) or show_title

            hi_path, hi_family = _font_from_choice(hindi_font_choice, HINDI_FONTS, _get_hindi_font)
            la_path, la_family = _font_from_choice(english_font_choice, ENGLISH_FONTS, _get_latin_font)
            ti_path, ti_family = _font_from_choice(title_font_choice, ENGLISH_FONTS, _get_latin_font)

            if need_hindi and not hi_path:
                log.log("     WARNING: NO Devanagari font found (bundled or system). "
                        "Hindi captions may render as empty boxes. Add a Devanagari .ttf "
                        "under ./fonts/ (e.g. NotoSansDevanagari-Bold.ttf).")
            hindi_family = hi_family or "Noto Sans Devanagari"
            latin_family = la_family or "Poppins"

            wanted_paths = []
            if need_hindi and hi_path:
                wanted_paths.append(hi_path)
            if need_latin and la_path:
                wanted_paths.append(la_path)
            if show_title and ti_path:
                wanted_paths.append(ti_path)
            fontsdir = _prepare_fontsdir(wanted_paths, job_dir)

            # 3. Build the ASS for the chosen layout/language/position/style.
            #    Figure out where the actual video band sits inside the 9:16 frame, so
            #    "bottom"/"top" captions can pin to the edge of the footage (just below /
            #    above it) rather than the very frame edge.
            #    All clips seek into the SAME source video, so its dimensions are probed
            #    once by the caller and passed in — avoids an ffprobe spawn per clip.
            if src_dims and src_dims[0] and src_dims[1]:
                src_w, src_h = src_dims
            else:
                src_w, src_h = _probe_dimensions(raw_path, log)
            vbox = _video_box(src_w, src_h, FRAME, fit)
            if vbox:
                log.log(f"     Video band: y {vbox[0]:.0f}–{vbox[1]:.0f} of {FRAME[1]} "
                        f"(src {src_w}x{src_h})")

            def _make_vf(segs, duration, tag=""):
                """Write an ASS for `segs` on a `duration`-long timeline and return the
                -vf chain that burns it. Called twice when the hook has to be dropped
                mid-ladder: the caption timeline differs between the two, so the file
                is rebuilt rather than reused at the wrong offset.

                Written inside the job dir (always exists, cross-platform) rather than
                a hardcoded "/tmp" — "/tmp" doesn't exist on Windows."""
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ass", delete=False,
                    dir=job_dir, prefix=f"clip{clip_index}_{tag}", encoding="utf-8"
                ) as tmp_ass:
                    path = tmp_ass.name
                ass_paths.append(path)

                primary, secondary, has_title = make_caption_ass(
                    segs, path,
                    layout=layout,
                    language=language,
                    position=position,
                    caption_style=caption_style,
                    accent_color=accent_color,
                    caption_color=caption_color,
                    caption_words=caption_words,
                    title=title,
                    show_title=show_title,
                    hindi_font=hindi_family,
                    latin_font=latin_family,
                    title_font=ti_family or "",
                    title_style=title_style,
                    title_color=title_color,
                    aspect=aspect,
                    fit=fit,
                    clip_duration=duration,
                    video_box=vbox,
                    part_label=part_label,
                    show_part_label=show_part_label,
                    caption_xy=caption_xy,
                    title_xy=title_xy,
                    part_xy=part_xy,
                    speaker_colours=speaker_colours,
                    caption_size=caption_size,
                    caption_xy_en=caption_xy_en,
                    position_hi=position_hi,
                    position_en=position_en,
                )
                log.log(f"     Tracks : {primary} primary cues / {secondary} secondary cues / "
                        f"title={'yes' if has_title else 'no'} (fontsdir={fontsdir or 'system'})")
                if primary == 0 and secondary == 0 and not has_title:
                    log.log("     WARNING: nothing to overlay - rendering plain 9:16 video.")
                    return _build_render_filter("", "", FRAME, fit)
                return _build_render_filter(path, fontsdir, FRAME, fit)

            vf = _make_vf(segments, render_duration)
            if hook_local:
                # Rung 3 of the ladder drops the hook; its captions must lose the
                # offset too, so keep a builder for the un-hooked timeline.
                _rebuild_vf_without_hook = lambda: _make_vf(
                    base_segments, clip_duration, tag="nohook_")

        # 4. SINGLE ffmpeg pass: seek into source (-ss/-to) + scale to 9:16 + (burn).
        # Input-seek BEFORE -i is fast (keyframe seek); we re-encode anyway so accuracy
        # is preserved by -to being applied on the trimmed input.
        def _build_cmd(venc_args, filter_chain, use_hook, use_logo=True):
            cmd = ["ffmpeg", "-y"]
            # Input 0 is ALWAYS the clip body, so the filter graph can count on it.
            if clip_start is not None and clip_end is not None:
                cmd += ["-ss", f"{clip_start:.3f}", "-to", f"{clip_end:.3f}"]
            cmd += ["-i", raw_path]
            next_idx = 1

            # Input 1 (when hooked) is the SAME source seeked to the hook window, in
            # ABSOLUTE source time — hook_local is clip-local, so add clip_start back.
            # Two cheap seeks instead of one decode fanned out through split+trim;
            # _build_hook_filter_complex explains why that difference is 10 GB.
            hook_idx = None
            if use_hook and hook_local:
                hook_idx = next_idx
                next_idx += 1
                cmd += ["-ss", f"{clip_start + hook_local[0]:.3f}",
                        "-to", f"{clip_start + hook_local[1]:.3f}", "-i", raw_path]

            lg = logo_cfg if use_logo else None
            logo_idx = None
            if lg:
                logo_idx = next_idx
                next_idx += 1
                # -loop 1 so a still PNG covers the whole clip rather than one frame.
                cmd += ["-loop", "1", "-i", lg["path"]]

            cmd += _filter_args(filter_chain, hook_idx, lg, logo_idx, FRAME)
            cmd += venc_args
            cmd += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    "-shortest", final_output]
            return cmd

        # Pick the fastest available encoder (NVENC -> AMF -> QSV -> libx264).
        venc_args, enc_name = providers.select_video_encoder(log)

        # Render fallback ladder — each rung drops one thing that can fail, so a clip is
        # only lost if even a plain CPU re-encode with no captions fails:
        #   1. chosen encoder + captions + hook
        #   2. CPU encoder + captions + hook  (GPU driver / session-limit failures)
        #   3. CPU encoder + captions, NO hook (silent source: [0:a] has nothing to
        #      split, so the concat graph errors out — the clip itself is still fine)
        #   4. CPU encoder, NO captions, NO hook (broken ASS, missing font, libass error)
        #
        # Captions are remapped for the hooked timeline, so dropping the hook at rung 3
        # shifts them; they are rebuilt without the offset there rather than left adrift.
        plain_vf = _build_render_filter("", "", FRAME, fit)
        has_logo = bool(logo_cfg)
        base = "captions + hook" if hook_local else "captions"
        if has_logo:
            base += " + logo"
        ladder = [(venc_args, enc_name, vf, True, True, base)]
        if enc_name != "cpu":
            ladder.append((providers.CPU_ENCODER_ARGS, "cpu", vf, True, True, base))
        if hook_local:
            ladder.append((providers.CPU_ENCODER_ARGS, "cpu", None, False, True,
                           "captions, NO hook"))
        if has_logo:
            ladder.append((providers.CPU_ENCODER_ARGS, "cpu", None, False, False,
                           "captions, NO hook, NO logo"))
        if vf != plain_vf:
            ladder.append((providers.CPU_ENCODER_ARGS, "cpu", plain_vf, False, False,
                           "NO captions"))

        result = None
        for enc_args, name, filter_chain, use_hook, use_logo, what in ladder:
            if filter_chain is None:
                # Rungs that drop the hook need captions rebuilt without its offset.
                # Overlay-only renders have no rebuild fn: their ASS is a single
                # full-length static cue, so the hook offset never applied to it and
                # the original chain is still correct.
                filter_chain = (_rebuild_vf_without_hook() if _rebuild_vf_without_hook
                                else vf)
            command = _build_cmd(enc_args, filter_chain, use_hook, use_logo)
            log.log(f"     FFmpeg ({name}, {what})")
            result = providers.run_cmd(command, timeout=RENDER_TIMEOUT, retries=1,
                                       log=log, label=f"ffmpeg-{name}")
            if result.returncode == 0 and os.path.exists(final_output) \
                    and os.path.getsize(final_output) >= 1000:
                if what == "NO captions":
                    log.log("     WARNING: rendered WITHOUT captions (caption burn kept failing).")
                break
            log.log(f"     render attempt failed (rc={result.returncode}) -> next fallback")

        if result is None or result.returncode != 0:
            log.log(f"     FFMPEG STDERR:\n{(result.stderr if result else '')[-1500:]}")
            log.log(f"     FAILED (return code {result.returncode if result else 'n/a'})")
            return None

        if not os.path.exists(final_output) or os.path.getsize(final_output) < 1000:
            log.log(f"     FAILED - output missing or too small")
            return None

        log.log(f"     SUCCESS -> {final_output} ({os.path.getsize(final_output)//1024} KB)")
        if clip_callback:
            clip_callback(final_output, reason)
        return final_output

    except Exception as e:
        log.error(f"Clip {clip_index} burn failed: {e}", e)
        return None

    finally:
        for path in ass_paths:
            try: os.unlink(path)
            except OSError: pass


# ─────────────────────────────────────────────────────────────
# Main entry point - subtitle burning only
# ─────────────────────────────────────────────────────────────

def execute_subtitle_workflow(
    job_dir: str,
    manifest_path: str = None,
    clip_callback=None,
    status_callback=None,
    subtitle_options: dict = None,
) -> tuple:
    """Reads clips_manifest.json and produces all finished Shorts.

    Caption behaviour comes from the manifest's "subtitle_options" block (written by
    select_clips.py), and can be overridden by passing `subtitle_options` here:
        burn_subtitles    : bool  - False => clean 9:16 clips, no captions
        subtitle_layout   : "single" | "dual"
        subtitle_language : "hindi" | "english" | "hinglish"   (single layout)
        subtitle_position : "top" | "middle" | "bottom"        (single layout)
        caption_style     : "outline" | "box"
        show_title        : bool

    STAGE 4 — transcribe each clip, then run ONLY the language pass(es) actually needed
    (translate for English/dual, transliterate for Hinglish, titles if enabled).
    STAGE 5 — render every clip in PARALLEL; each clip is a single ffmpeg pass that
    seeks into the SOURCE video (-ss/-to) and cuts + scales to 9:16 + (burns captions).

    Returns (final_clips, log_path).
    """
    import concurrent.futures, os
    if manifest_path is None:
        manifest_path = os.path.join(job_dir, "clips_manifest.json")

    joblog.start(os.path.basename(os.path.normpath(job_dir)), "render")
    log = DiagnosticLog(job_dir)
    log.section("JOB INFO")
    log.log(f"   Job dir       : {job_dir}")
    log.log(f"   Manifest path : {manifest_path}")

    final_clips = []

    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)

        raw_clips = manifest.get("clips", [])
        clips_dir = os.path.join(job_dir, "clips")
        os.makedirs(clips_dir, exist_ok=True)

        # ── Resolve caption config: manifest defaults, overridden by any caller args ──
        cfg = dict(manifest.get("subtitle_options", {}) or {})
        if subtitle_options:
            cfg.update({k: v for k, v in subtitle_options.items() if v is not None})

        burn          = bool(cfg.get("burn_subtitles", True))
        layout        = str(cfg.get("subtitle_layout", "single")).lower()
        language      = str(cfg.get("subtitle_language", "hindi")).lower()
        position      = str(cfg.get("subtitle_position", "bottom")).lower()
        caption_style = str(cfg.get("caption_style", "outline")).lower()
        accent_color  = str(cfg.get("caption_accent", "") or "")
        caption_color = str(cfg.get("caption_color", "") or "")
        caption_words = cfg.get("caption_words", 0)
        title_font_choice = str(cfg.get("title_font", "") or "").lower()
        title_style   = str(cfg.get("title_style", "") or "").lower()
        title_color   = str(cfg.get("title_color", "") or "")
        aspect        = str(cfg.get("aspect", DEFAULT_ASPECT) or DEFAULT_ASPECT)
        fit           = str(cfg.get("fit", "fit") or "fit").lower()
        hindi_font_choice   = str(cfg.get("hindi_font", "") or "").lower()
        english_font_choice = str(cfg.get("english_font", "") or "").lower()
        show_title    = bool(cfg.get("show_title", False))
        # Sequential extras + free (drag-and-drop) overlay placement.
        series_title    = str(cfg.get("series_title", "") or "").strip()
        show_part_label = bool(cfg.get("show_part_label", True))
        # Podcast: map speaker id -> caption colour, from the roles the selection
        # stage worked out. Absent roles (not a podcast job) leaves this empty and
        # every caption keeps the single chosen colour.
        speaker_colours = {}
        _roles = cfg.get("speaker_roles") or {}
        _host_hex = str(cfg.get("host_color", "") or "").strip()
        _guest_hex = str(cfg.get("guest_color", "") or "").strip()
        if _roles:
            if _roles.get("host") is not None and _host_hex:
                speaker_colours[_roles["host"]] = _hex_to_ass(_host_hex)
            if _roles.get("guest") is not None and _guest_hex:
                speaker_colours[_roles["guest"]] = _hex_to_ass(_guest_hex)

        # Caption size multiplier, and the dual layout's independent positions.
        try:
            caption_size = float(cfg.get("caption_size", CAPTION_SIZE_DEFAULT))
        except (TypeError, ValueError):
            caption_size = CAPTION_SIZE_DEFAULT
        position_hi = str(cfg.get("subtitle_position_hi", "") or "").lower()
        position_en = str(cfg.get("subtitle_position_en", "") or "").lower()
        if position_hi not in ("top", "middle", "bottom", "below"):
            position_hi = ""
        if position_en not in ("top", "middle", "bottom", "below"):
            position_en = ""
        caption_xy_en = _norm_xy(cfg.get("caption_xy_en"))

        caption_xy = _norm_xy(cfg.get("caption_xy"))
        title_xy   = _norm_xy(cfg.get("title_xy"))
        part_xy    = _norm_xy(cfg.get("part_xy"))
        # A user-supplied series title is a fixed headline on every part, so it takes
        # the existing title slot and makes the AI title pass unnecessary.
        if series_title:
            show_title = True
        if layout not in ("single", "dual"):
            layout = "single"
        if language not in ("hindi", "english", "hinglish"):
            language = "hindi"
        if position not in ("top", "middle", "bottom", "below"):
            position = "bottom"
        if caption_style not in VALID_CAPTION_STYLES:
            caption_style = "outline"
        # Resolved here purely so the log shows the number that will actually be
        # used; make_caption_ass resolves it again per clip from the raw value.
        words_on_screen = _resolve_words_on_screen(caption_words, caption_style)
        if hindi_font_choice and hindi_font_choice not in VALID_HINDI_FONTS:
            hindi_font_choice = ""
        if english_font_choice and english_font_choice not in VALID_ENGLISH_FONTS:
            english_font_choice = ""
        if title_font_choice and title_font_choice not in VALID_ENGLISH_FONTS:
            title_font_choice = ""
        # "" is meaningful here — it means the original headline look — so an
        # unknown style falls back to that rather than to "outline".
        if title_style and title_style not in VALID_CAPTION_STYLES:
            title_style = ""
        if aspect not in ASPECTS:
            aspect = DEFAULT_ASPECT
        if fit not in ("fit", "fill"):
            fit = "fit"

        # Post-selection features, all driven from the manifest so the CLI entry point
        # gets them too.
        hook_first = bool(manifest.get("hook_first", False))
        want_council = bool(manifest.get("viral_council", True))
        want_kit = bool(manifest.get("publish_kit", True))
        hooked_clips = sum(1 for rc in raw_clips
                           if rc.get("hook_start") is not None) if hook_first else 0
        # Validated once for the whole job — every clip shares the same watermark.
        logo_cfg = _norm_logo(cfg)
        if cfg.get("logo_path") and not os.path.isfile(str(cfg.get("logo_path"))):
            log.log(f"   WARNING: logo '{cfg.get('logo_path')}' is missing on disk — "
                    f"falling back to the Piksy watermark.")
        if logo_cfg.get("is_watermark"):
            log.log(f"   Watermark: Piksy at {logo_cfg['opacity']*100:.0f}% opacity "
                    f"({logo_cfg['scale']*100:.0f}% width)")
        elif logo_cfg:
            log.log(f"   Watermark: your logo at {logo_cfg['opacity']*100:.0f}% opacity")
        else:
            log.log("   Watermark: none (Piksy mark switched off)")

        log.section("CAPTION CONFIG")
        log.log(f"   burn={burn} | layout={layout} | language={language} | position={position} | "
                f"style={caption_style} | size=x{caption_size:.2f} | title={show_title}")
        if layout == "dual" and (position_hi or position_en or caption_xy_en):
            log.log(f"   dual positions: hindi={position_hi or 'default'} "
                    f"english={position_en or 'default'}")
        log.log(f"   text={caption_color or 'style default'} | "
                f"highlight={accent_color or 'default'} | "
                f"words on screen={words_on_screen}"
                f"{' (auto)' if not caption_words else ''}")
        log.log(f"   fonts: hindi={hindi_font_choice or 'auto'} | english={english_font_choice or 'auto'}")
        if show_title:
            log.log(f"   headline: font={title_font_choice or 'same as captions'} | "
                    f"style={title_style or 'default yellow caps'} | "
                    f"colour={title_color or 'from style'}")
        log.log(f"   hook-first={hook_first} ({hooked_clips}/{len(raw_clips)} clips) | "
                f"council={want_council} | publish kit={want_kit} | "
                f"logo={'yes' if logo_cfg else 'no'}")

        clip_segments = {}   # index -> segments list
        clip_titles = {}     # index -> title string

        if not burn:
            # No captions at all — skip transcription/translation entirely (saves API $).
            log.section("STAGE 4 - SKIPPED (subtitles off)")
            log.log("   Subtitles are OFF — clips will be cut + scaled to 9:16 with no captions.")
            if status_callback:
                status_callback("Rendering clean 9:16 clips (no subtitles)...")
        else:
            # Subtitle source engine:
            #   "deepgram" (DEFAULT) — transcribe each SELECTED clip with Deepgram nova-3.
            #                          Best Hindi/Hinglish word-level accuracy. Used when a
            #                          DEEPGRAM_API_KEY is set; per-clip failures fall back
            #                          to whisper-slice automatically so captions never blank.
            #   "whisper"            — reuse the full-video Whisper transcript by slicing it
            #                          to each clip (free, no extra API, needs GROQ).
            engine = os.environ.get("SUBTITLE_ENGINE", "deepgram").lower()
            whisper_full = os.path.join(job_dir, "transcript_full.json")
            have_dg_key = bool((os.environ.get("DEEPGRAM_API_KEY") or "").strip())
            have_whisper = os.path.exists(whisper_full)
            # Podcast jobs MUST slice the full transcript rather than re-transcribe
            # each clip: only the full pass was diarised, so a fresh per-clip call
            # comes back with no speaker labels and the captions lose their colour.
            if speaker_colours and have_whisper:
                if engine != "whisper":
                    log.log("   Podcast job: slicing the diarised full transcript so "
                            "speaker labels (and their caption colours) survive")
                engine = "whisper"
            # Deepgram only when explicitly requested AND a real key exists.
            if engine == "deepgram" and not have_dg_key:
                log.log("   SUBTITLE_ENGINE=deepgram but no DEEPGRAM_API_KEY -> using whisper-slice")
                engine = "whisper"
            # Whisper needs the full transcript; if it's somehow missing but a Deepgram
            # key is available, use Deepgram as the fallback instead.
            if engine == "whisper" and not have_whisper:
                if have_dg_key:
                    log.log("   No full transcript found -> falling back to Deepgram")
                    engine = "deepgram"
                else:
                    log.log("   WARNING: no full transcript and no Deepgram key -> captions may be empty")
            log.log(f"   Subtitle engine: {engine}")

            # ── STAGE 4: per-clip transcription -> ONLY the needed language pass(es) ──
            log.section("STAGE 4 - PER-CLIP TRANSCRIBE + LANGUAGE PREP")
            if status_callback:
                status_callback("Preparing subtitles for all clips...")

            all_segment_lists = []
            order = []
            for rc in raw_clips:
                idx = rc["index"]
                cs, ce = rc.get("start"), rc.get("end")
                segs = []
                try:
                    if engine == "deepgram":
                        # Cut just this clip's AUDIO from the source, then run the FULL
                        # transcription chain on it (Deepgram -> Groq Whisper -> local).
                        try:
                            clip_audio = os.path.join(clips_dir, f"clip_{idx}_audio.mp3")
                            _extract_clip_audio(rc["raw_path"], clip_audio, cs, ce, log)
                            data = providers.transcribe_audio(
                                clip_audio, language="hi", log=log,
                                prefer=os.environ.get("CLIP_TRANSCRIBE_ORDER", "deepgram,groq,local"))
                            segs = (data or {}).get("segments", [])
                            try: os.remove(clip_audio)
                            except OSError: pass
                        except Exception as dg_err:
                            # ROBUSTNESS: a provider outage on one clip must not blank its
                            # captions — fall back to slicing the full Whisper transcript.
                            log.log(f"   Clip {idx}: per-clip transcription failed ({dg_err}); "
                                    f"falling back to whisper-slice")
                            segs = []
                        if not segs and os.path.exists(whisper_full) and cs is not None and ce is not None:
                            log.log(f"   Clip {idx}: falling back to whisper-slice")
                            segs = _slice_transcript(whisper_full, cs, ce).get("segments", [])
                    elif os.path.exists(whisper_full) and cs is not None and ce is not None:
                        segs = _slice_transcript(whisper_full, cs, ce).get("segments", [])
                except Exception as e:
                    log.error(f"   Clip {idx}: transcription failed: {e}", e)
                    segs = []
                clip_segments[idx] = segs
                all_segment_lists.append(segs)
                order.append(idx)

            # Run ONLY the passes the chosen captions require (each is one Groq call):
            need_translate = (layout == "dual") or (layout == "single" and language == "english")
            need_translit  = (layout == "single" and language == "hinglish")

            if need_translate:
                joblog.begin("Translate to English")
                batch_translate_clips(all_segment_lists, log)
                joblog.end("Translate to English", ok=True)
            if need_translit:
                joblog.begin("Romanise to Hinglish")
                batch_transliterate_clips(all_segment_lists, log)
                joblog.end("Romanise to Hinglish", ok=True)

            # If the language pass produced nothing at all (every chat provider down),
            # rendering would give blank captions. Falling back to the source Devanagari
            # keeps captions on screen — and it needs the Hindi font, so switch language
            # rather than leaving a Latin font to draw Devanagari as empty boxes.
            if need_translate or need_translit:
                key = "text_en" if need_translate else "text_hinglish"
                got = sum(1 for segs in all_segment_lists for s in segs if (s.get(key) or "").strip())
                if got == 0 and any(all_segment_lists):
                    log.log(f"   WARNING: no '{key}' text was produced (chat providers "
                            f"unavailable) — falling back to Hindi captions for this job.")
                    if layout == "dual":
                        layout = "single"
                    language = "hindi"
            # A fixed series title is used verbatim on every clip, so there is
            # nothing for the AI title pass to do — skip the extra LLM call.
            # The publish kit (STAGE 4b) writes its headline against the same brief as
            # the caption and hashtags, so when it is enabled its title wins and this
            # older single-purpose pass is skipped — otherwise it is two LLM calls for
            # one overlay.
            if show_title and not series_title and not want_kit:
                joblog.begin("Write headlines")
                titles = batch_generate_titles(all_segment_lists, log)
                joblog.end("Write headlines", ok=any(titles),
                           detail=f"{sum(1 for t in titles if t)}/{len(titles)} written")
                clip_titles = {order[i]: titles[i] for i in range(len(order))}
            elif series_title:
                log.log(f"   Series title: \"{series_title}\" (AI title generation skipped)")

            # Persist a single combined transcript file for reference.
            try:
                transcripts_txt = os.path.join(job_dir, "clip_transcripts.txt")
                with open(transcripts_txt, "w", encoding="utf-8") as tf:
                    for idx in order:
                        segs = clip_segments[idx]
                        tf.write("=" * 70 + f"\nCLIP {idx}  ({len(segs)} segments)\n")
                        if show_title:
                            tf.write(f"TITLE: {clip_titles.get(idx,'')}\n")
                        tf.write("=" * 70 + "\n")
                        for seg in segs:
                            tf.write(f"[{seg.get('start',0):7.2f} - {seg.get('end',0):7.2f}]\n")
                            tf.write(f"   HI: {seg.get('text','').strip()}\n")
                            if seg.get("text_en"):
                                tf.write(f"   EN: {seg.get('text_en','').strip()}\n")
                            if seg.get("text_hinglish"):
                                tf.write(f"   HG: {seg.get('text_hinglish','').strip()}\n")
                        tf.write("\n")
            except Exception as e:
                log.error(f"Could not write combined transcript file: {e}", e)

        # ── STAGE 4b: viral council + publish kit ────────────────────────────
        # Both read each clip's words. When captions are on, STAGE 4 already
        # transcribed every clip; when they are off there is nothing in
        # clip_segments, so the full-video transcript is sliced instead and these
        # features keep working on a captionless job.
        council_verdicts, kits = [], {}
        if want_council or want_kit:
            whisper_full = os.path.join(job_dir, "transcript_full.json")
            briefs = []
            for rc in raw_clips:
                idx = rc["index"]
                segs = clip_segments.get(idx) or []
                if (not segs and os.path.exists(whisper_full)
                        and rc.get("start") is not None and rc.get("end") is not None):
                    try:
                        segs = _slice_transcript(whisper_full, rc["start"],
                                                 rc["end"]).get("segments", [])
                    except (OSError, ValueError) as e:
                        log.log(f"   Clip {idx}: could not slice transcript for analysis ({e})")
                        segs = []
                texts = [(s.get("text") or "").strip() for s in segs]
                briefs.append({
                    "index": idx,
                    "duration": round(float(rc.get("end") or 0) - float(rc.get("start") or 0), 2),
                    "score": rc.get("score", 0),
                    "opening": " ".join(texts[:2]).strip(),
                    "transcript": " ".join(texts).strip(),
                    "hook_text": rc.get("hook_text", ""),
                })

            if want_council:
                if status_callback:
                    status_callback("AI council is ranking clips by view potential...")
                try:
                    joblog.begin("Rank clips (viral council)")
                    council_verdicts = viral_council.convene(briefs, log)
                    joblog.end("Rank clips (viral council)", ok=bool(council_verdicts),
                               detail=f"{len(council_verdicts or [])} judged")
                except Exception as e:
                    # Ranking is a bonus on top of the clips — never lose a finished
                    # render because the council choked.
                    log.error(f"Viral council failed (clips are unaffected): {e}", e)

            if want_kit:
                if status_callback:
                    status_callback("Writing titles, captions and hashtags...")
                try:
                    joblog.begin("Write titles + hashtags")
                    kits = publish_kit.generate(briefs, log)
                    joblog.end("Write titles + hashtags", ok=bool(kits),
                               detail=f"{len(kits or {})} clip(s)")
                except Exception as e:
                    log.error(f"Publish kit failed (clips are unaffected): {e}", e)

            # The kit's headline is the on-screen title, replacing the old title pass.
            if show_title and not series_title and kits:
                for idx, kit in kits.items():
                    if kit.get("onscreen"):
                        clip_titles[idx] = kit["onscreen"]

        # ── STAGE 5: parallel burn (single pass per clip, seek into source) ──
        log.section("STAGE 5 - PARALLEL RENDER (cut + scale + optional captions)")
        log.log(f"   Total clips: {len(raw_clips)}")

        # Probe the SOURCE video's dimensions ONCE — every clip seeks into the same
        # file, so a single ffprobe replaces one-per-clip.
        src_video = manifest.get("video_path") or (raw_clips[0]["raw_path"] if raw_clips else None)
        src_dims = _probe_dimensions(src_video, log) if src_video else (None, None)
        log.log(f"   Canvas: {aspect} {_frame(aspect)[0]}x{_frame(aspect)[1]} | "
                f"{'crop to fill' if fit == 'fill' else 'fit with bars'}")
        log.log(f"   Source dimensions: {src_dims[0]}x{src_dims[1]}" if src_dims[0] else
                "   Source dimensions: unknown (will probe per clip)")

        # Decide which encoder we'll use so we can size the worker pool sensibly.
        _venc_args, _enc_name = providers.select_video_encoder(log)
        _env_workers = os.environ.get("MAX_RENDER_WORKERS")
        if _env_workers:
            max_workers = max(1, int(_env_workers))
        elif _enc_name != "cpu":
            # Hardware encoders share ONE GPU encode block; a few parallel ffmpegs keep
            # it fed (CPU still does libass + scaling) without thrashing the GPU session.
            max_workers = max(1, min(4, len(raw_clips)))
        else:
            try:
                import psutil
                free_gb = psutil.virtual_memory().available / (1024 ** 3)
                cores = psutil.cpu_count(logical=False) or (os.cpu_count() or 2)
                # Each CPU ffmpeg uses ~2 threads, so don't exceed physical cores.
                max_workers = max(1, min(cores, int(free_gb // 1.5)))
            except Exception:
                max_workers = 2   # safe default for low-RAM machines (e.g. 4GB WSL)
        log.log(f"   Encoder: {_enc_name} | Parallel workers: {max_workers}")

        results = {}
        done_count = {"n": 0}

        def _process(rc):
            idx = rc["index"]
            out = burn_subtitles_for_clip(
                rc["raw_path"], idx, job_dir, clips_dir, log,
                clip_callback=clip_callback, reason=rc.get("reason", ""),
                clip_start=rc.get("start"), clip_end=rc.get("end"),
                segments=clip_segments.get(idx),
                # A fixed series title wins over the per-clip AI headline.
                title=series_title or clip_titles.get(idx, ""),
                burn=burn,
                layout=layout,
                language=language,
                position=position,
                caption_style=caption_style,
                accent_color=accent_color,
                caption_color=caption_color,
                caption_words=caption_words,
                title_font_choice=title_font_choice,
                title_style=title_style,
                title_color=title_color,
                aspect=aspect,
                fit=fit,
                hindi_font_choice=hindi_font_choice,
                english_font_choice=english_font_choice,
                show_title=show_title,
                src_dims=src_dims,
                part_label=rc.get("part_label", ""),
                show_part_label=show_part_label,
                caption_xy=caption_xy,
                title_xy=title_xy,
                part_xy=part_xy,
                speaker_colours=speaker_colours,
                caption_size=caption_size,
                caption_xy_en=caption_xy_en,
                position_hi=position_hi,
                position_en=position_en,
                # Hook-first cold open, chosen during selection. None for clips that
                # were too short for one, and for every sequential part.
                hook_start=rc.get("hook_start") if hook_first else None,
                hook_end=rc.get("hook_end") if hook_first else None,
                logo=logo_cfg,
            )
            done_count["n"] += 1
            if status_callback:
                status_callback(f"Rendered {done_count['n']}/{len(raw_clips)} clips...")
            return idx, out

        joblog.fact("encoder", _enc_name)
        joblog.begin("Render clips")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_process, rc): rc["index"] for rc in raw_clips}
            for fut in concurrent.futures.as_completed(futures):
                try:
                    idx, out = fut.result()
                    results[idx] = out
                except Exception as e:
                    log.error(f"Worker raised exception: {e}", e)

        for rc in raw_clips:
            out = results.get(rc["index"])
            if out:
                final_clips.append(out)

        # One line for the whole render, naming how many clips actually came out.
        _want = len(raw_clips)
        _got = len(final_clips)
        joblog.end("Render clips", ok=(_got == _want and _got > 0),
                   detail=f"{_got}/{_want} produced"
                          + ("" if _got == _want else "  <-- some clips failed"))

        # ── Posting sheets: one <clip>_POST.txt beside each rendered clip ──
        # Written after the render because each sheet is named for the finished file
        # and quotes its rank, so it travels inside the downloaded ZIP.
        verdict_by_idx = {v["index"]: v for v in council_verdicts}
        clip_files = {}
        if kits:
            log.section("PUBLISH KIT FILES")
            written = 0
            for rc in raw_clips:
                idx = rc["index"]
                out, kit = results.get(idx), kits.get(idx)
                if not out:
                    continue
                clip_files[idx] = os.path.basename(out)
                if not kit:
                    continue
                # The posted length includes the cold open, so a hooked clip is
                # longer than its source window — report what actually got rendered.
                body = float(rc.get("end") or 0) - float(rc.get("start") or 0)
                hooked = (hook_first and rc.get("hook_start") is not None
                          and rc.get("hook_end") is not None)
                extra = (float(rc["hook_end"]) - float(rc["hook_start"])) if hooked else 0.0
                try:
                    publish_kit.write_kit_file(
                        kit, out,
                        clip_meta={
                            "filename": os.path.basename(out),
                            "start": rc.get("start"),
                            "end": rc.get("end"),
                            "duration": body + extra,
                            "hook_text": rc.get("hook_text", "") if hooked else "",
                        },
                        verdict=verdict_by_idx.get(idx),
                    )
                    written += 1
                except OSError as e:
                    log.error(f"Could not write posting sheet for clip {idx}: {e}")
            log.log(f"   {written} posting sheet(s) written next to the clips.")

        # One machine-readable file for the UI's ranking + copy panel.
        if council_verdicts or kits:
            try:
                with open(os.path.join(job_dir, "publish_kit.json"), "w", encoding="utf-8") as f:
                    json.dump({
                        "ranking": council_verdicts,
                        "kits": {str(k): v for k, v in kits.items()},
                        "files": {str(k): v for k, v in clip_files.items()},
                        "hooks": {str(rc["index"]): {
                            "start": rc.get("hook_start"),
                            "end": rc.get("hook_end"),
                            "text": rc.get("hook_text", ""),
                        } for rc in raw_clips if rc.get("hook_start") is not None},
                    }, f, indent=2, ensure_ascii=False)
            except OSError as e:
                log.error(f"Could not write publish_kit.json: {e}")

    except Exception as e:
        log.section("SUBTITLE PIPELINE CRASHED")
        log.error(f"Unhandled exception: {e}", e)
        joblog.step("Render pipeline", ok=False, detail=str(e)[:110])
        final_clips = []

    log.finalize(final_clips)

    joblog.fact("clips", len(final_clips))
    short = joblog.finish(f"{len(final_clips)} clip(s) rendered" if final_clips
                          else "NO clips rendered")
    if short:
        log.log(f"\nShort run log: {short}")
    return final_clips, log.path


# ─────────────────────────────────────────────────────────────
# CLI usage
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Burn word-by-word subtitles onto raw clips produced by select_clips.py"
    )
    parser.add_argument(
        "job_dir",
        help="Path to the job directory produced by select_clips.py "
             "(e.g. output/<job_id>), containing clips_manifest.json"
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Optional explicit path to clips_manifest.json (defaults to <job_dir>/clips_manifest.json)"
    )
    parser.add_argument("--no-burn", action="store_true",
                        help="Render clean 9:16 clips with NO captions (overrides the manifest)")
    parser.add_argument("--layout", choices=["single", "dual"], default=None,
                        help="single = one language track; dual = Hindi top + English bottom")
    parser.add_argument("--language", choices=["hindi", "english", "hinglish"], default=None,
                        help="Caption language for single layout")
    parser.add_argument("--position", choices=["top", "middle", "bottom", "below"], default=None,
                        help="bottom = on the video; below = under the video (in the letterbox bar)")
    parser.add_argument("--style",
                        choices=list(VALID_CAPTION_STYLES),
                        default=None,
                        help="Caption look: " + ", ".join(VALID_CAPTION_STYLES))
    parser.add_argument("--accent", default=None,
                        help="Highlight colour for the karaoke/word_pop active word, e.g. '#FFE600'")
    parser.add_argument("--caption-color", default=None,
                        help="Caption TEXT colour for any style, e.g. '#E8F5E9'")
    parser.add_argument("--words", type=int, default=None,
                        help=f"Words on screen at once ({WORDS_ON_SCREEN_MIN}-{WORDS_ON_SCREEN_MAX}); "
                             "omit for the style's own default")
    parser.add_argument("--title", action="store_true", default=None,
                        help="Overlay an AI-generated headline title")
    args = parser.parse_args()

    overrides = {}
    if args.no_burn:        overrides["burn_subtitles"] = False
    if args.layout:         overrides["subtitle_layout"] = args.layout
    if args.language:       overrides["subtitle_language"] = args.language
    if args.position:       overrides["subtitle_position"] = args.position
    if args.style:          overrides["caption_style"] = args.style
    if args.accent:         overrides["caption_accent"] = args.accent
    if args.caption_color:  overrides["caption_color"] = args.caption_color
    if args.words:          overrides["caption_words"] = args.words
    if args.title:          overrides["show_title"] = True

    clips, log_path = execute_subtitle_workflow(
        args.job_dir, manifest_path=args.manifest,
        subtitle_options=overrides or None,
    )

    print(f"\nDone. {len(clips)} subtitled clip(s) produced.")
    print(f"Log: {log_path}")