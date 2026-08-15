import os
import re
import sys
import math
import time
import uuid
import subprocess
import json
import traceback
import datetime
import gdown
import providers

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ─────────────────────────────────────────────────────────────
# Diagnostic Logger
# ─────────────────────────────────────────────────────────────

class DiagnosticLog:
    """Writes a human-readable diagnostic report to a .txt file."""

    def __init__(self, job_dir: str):
        self.job_dir = job_dir
        self.path = os.path.join(job_dir, "DIAGNOSTIC_REPORT.txt")
        self.lines = []
        self._write_header()

    def _write_header(self):
        self.lines.append("=" * 70)
        self.lines.append("   CLIP SELECTION DIAGNOSTIC REPORT")
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

    def finalize(self, raw_clips: list):
        self.section("FINAL RESULT")
        if raw_clips:
            self.log(f"SUCCESS: {len(raw_clips)} raw clip(s) produced:")
            for c in raw_clips:
                path = c["raw_path"]
                size_kb = os.path.getsize(path) // 1024 if os.path.exists(path) else 0
                self.log(f"   - {os.path.basename(path)}  ({size_kb} KB)  score={c.get('score')}  | {c.get('reason','')}")
        else:
            self.log("FAILED: ZERO raw clips were produced. Check errors above.")
        self.log("")
        self.log(f"Report saved to: {self.path}")
        self._flush()


# ─────────────────────────────────────────────────────────────
# Download helpers
# ─────────────────────────────────────────────────────────────

def download_from_gdrive(url: str, output_path: str, log: DiagnosticLog):
    log.log("[Step 1] Google Drive link - downloading via gdown...")
    last_err = None
    for attempt in range(1, 4):
        try:
            gdown.download(url, output_path, quiet=False, fuzzy=True)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                break
            last_err = RuntimeError("gdown produced no file")
        except Exception as e:
            last_err = e
            log.log(f"         gdown attempt {attempt}/3 failed: {e}")
            time.sleep(2 * attempt)
    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        raise RuntimeError(f"Google Drive download failed: {last_err}")

    size = os.path.getsize(output_path)
    log.log(f"         Downloaded file size: {size} bytes")
    if size < 100_000:
        with open(output_path, "r", errors="ignore") as f:
            content = f.read(500)
        if "<html" in content.lower():
            raise ValueError(
                "Downloaded an HTML error page instead of video. "
                "Make sure the link is set to 'Anyone with the link can view'."
            )

def _ytdlp_format(quality: str) -> str:
    """Map a UI quality choice to a yt-dlp format string (caps the video height)."""
    q = str(quality or "best").lower()
    heights = {"1080": 1080, "720": 720, "480": 480, "360": 360}
    if q in heights:
        h = heights[q]
        return (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]/"
                f"best[height<={h}][ext=mp4]/best[height<={h}]/best")
    return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"


def _ytdlp_binary() -> list:
    """yt-dlp as a CLI if it is on PATH, else the module inside the current
    interpreter — so a venv install works even when the console script isn't
    exported to PATH."""
    if providers.have_binary("yt-dlp"):
        return ["yt-dlp"]
    return [sys.executable, "-m", "yt_dlp"]


DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "3600"))


# "[download]  42.7% of  118.44MiB at   2.31MiB/s ETA 00:33"
_DL_PROGRESS = re.compile(
    r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%\s+of\s+~?\s*([\d.]+\w+)"
    r"(?:\s+at\s+([\d.]+\w+/s))?(?:\s+ETA\s+([\d:]+))?")


def _report_download_line(line: str, progress_callback):
    """Turn one yt-dlp progress line into a status string for the UI.

    yt-dlp reports 0-100% per FILE, and a merged mp4 is two files (video, then
    audio), so the bar legitimately runs to 100 twice. The label says which pass is
    running rather than pretending it is one continuous number, because silently
    resetting a progress bar to 0 reads as a failure."""
    if not progress_callback:
        return
    m = _DL_PROGRESS.search(line or "")
    if not m:
        return
    pct, total, speed, eta = m.group(1), m.group(2), m.group(3), m.group(4)
    bits = [f"Downloading {float(pct):.0f}% of {total}"]
    if speed:
        bits.append(f"at {speed}")
    if eta and eta != "Unknown":
        bits.append(f"ETA {eta}")
    progress_callback(" ".join(bits), float(pct))


def _download_with_ytdlp(url: str, output_path: str, log: DiagnosticLog,
                         video_quality: str = "best", progress_callback=None) -> str:
    """Download via yt-dlp, escalating through progressively more permissive
    strategies. Each strategy fixes a different real-world failure:

      1. cookies + requested quality      — normal path (age/login-gated videos)
      2. no cookies                       — expired/broken cookies.txt
      3. android player client            — bypasses most 'Sign in to confirm' walls
      4. plain 'best'                     — format string matched nothing

    Only strategy 1 uses cookies.txt, and only when the file actually exists, so a
    missing cookies file no longer hard-fails every download.
    """
    base = _ytdlp_binary()
    fmt = _ytdlp_format(video_quality)
    cookies = os.path.join(BASE_DIR, "cookies.txt")
    have_cookies = os.path.exists(cookies) and os.path.getsize(cookies) > 0

    strategies = []
    if have_cookies:
        strategies.append(("cookies + requested quality", ["--cookies", cookies, "-f", fmt]))
    strategies += [
        ("no cookies", ["-f", fmt]),
        ("android client", ["-f", fmt, "--extractor-args", "youtube:player_client=android"]),
        ("any available format", ["-f", "best"]),
    ]

    last_err = ""
    for name, extra in strategies:
        log.log(f"[Step 1] yt-dlp strategy: {name}  (quality={video_quality})")
        cmd = base + extra + [
            "--no-playlist",
            "--retries", "5",
            "--fragment-retries", "10",
            "--socket-timeout", "30",
            "--merge-output-format", "mp4",
            # yt-dlp redraws its progress bar with \r, which is invisible to a line
            # reader. --newline puts each update on its own line; --progress forces
            # the bar to appear at all when stdout is a pipe rather than a terminal.
            "--newline", "--progress",
            "-o", output_path,
            url,
        ]
        result = providers.run_cmd_streaming(
            cmd, timeout=DOWNLOAD_TIMEOUT, log=log, label="yt-dlp",
            on_line=lambda ln: _report_download_line(ln, progress_callback))
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10_000:
            log.log(f"         Video saved to: {output_path} "
                    f"({os.path.getsize(output_path)//1024} KB) via '{name}'")
            return output_path
        last_err = (result.stderr or result.stdout or "")[-800:]
        log.log(f"         strategy '{name}' failed -> trying next")
        # A partial file from a failed attempt would confuse the next strategy.
        if os.path.exists(output_path) and os.path.getsize(output_path) <= 10_000:
            try:
                os.remove(output_path)
            except OSError:
                pass

    log.error(f"Every yt-dlp strategy failed. Last error:\n{last_err}")
    raise RuntimeError(
        "Video download failed. The link may be private, region-locked, or require "
        "fresh cookies (see cookies.txt in the README)."
    )


def download_video(url: str, job_dir: str, log: DiagnosticLog, video_quality: str = "best",
                   progress_callback=None) -> str:
    video_output_path = os.path.join(job_dir, "raw_video.mp4")
    if "drive.google.com" in url:
        download_from_gdrive(url, video_output_path, log)
    else:
        _download_with_ytdlp(url, video_output_path, log, video_quality, progress_callback)
    return video_output_path


# ─────────────────────────────────────────────────────────────
# Audio extraction
# ─────────────────────────────────────────────────────────────

def extract_audio(video_path: str, output_path: str, log: DiagnosticLog) -> str:
    log.log(f"[Audio] Extracting audio from: {video_path}")
    log.log(f"        Video file exists: {os.path.exists(video_path)}")
    log.log(f"        Video file size:   {os.path.getsize(video_path) if os.path.exists(video_path) else 'N/A'} bytes")

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Video not found: {video_path}")

    # 16 kHz mono is the native sample rate every ASR engine (Whisper/Deepgram)
    # resamples to anyway — extracting at 16 kHz makes a much smaller file, so the
    # ffmpeg encode is faster AND the upload to the transcription API is far quicker.
    #
    # Fallback ladder: the compact 16 kHz encode fails on some odd source streams
    # (broken timestamps, exotic codecs), so we retry with progressively more
    # forgiving settings rather than losing the job.
    attempts = [
        ("16kHz mono mp3", ["-vn", "-ar", "16000", "-ac", "1", "-b:a", "32k", "-f", "mp3"]),
        ("44.1kHz mono mp3", ["-vn", "-ar", "44100", "-ac", "1", "-b:a", "64k", "-f", "mp3"]),
        ("stream copy -> mp3", ["-vn", "-acodec", "libmp3lame", "-q:a", "5", "-f", "mp3"]),
    ]

    last_err = ""
    for name, args in attempts:
        command = ["ffmpeg", "-y", "-err_detect", "ignore_err", "-i", video_path] + args + [output_path]
        result = providers.run_cmd(command, timeout=1800, retries=1, log=log, label="ffmpeg-audio")
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 1000:
            log.log(f"        Audio saved via '{name}': {output_path} "
                    f"({os.path.getsize(output_path)} bytes)")
            return output_path
        last_err = (result.stderr or "")[-800:]
        log.log(f"        audio strategy '{name}' failed -> trying next")

    log.error(f"FFmpeg audio extraction failed after all strategies:\n{last_err}")
    raise RuntimeError(f"FFmpeg audio extraction failed:\n{last_err}")


# ─────────────────────────────────────────────────────────────
# Full-video transcription (used ONLY for highlight selection)
# ─────────────────────────────────────────────────────────────

def transcribe_full_video(audio_path: str, job_dir: str, log: DiagnosticLog) -> str:
    """Transcribe the full video for AI highlight selection AND subtitle slicing.

    ROBUST: tries a full provider chain so a single outage never sinks a job —
        1. Groq Whisper (whisper-large-v3 -> turbo, with retries)
        2. Deepgram nova-3
        3. local faster-whisper (offline; never rate-limits — ideal on a GPU box)
    Order is configurable via env TRANSCRIBE_ORDER. See providers.transcribe_audio.
    """
    log.log("[Step 3] Transcribing full video (robust multi-provider) for highlight selection...")

    data = providers.transcribe_audio(audio_path, language="hi", log=log)
    if not data or not data.get("segments"):
        raise RuntimeError(
            "All transcription providers failed (Groq / Deepgram / local Whisper). "
            "Check API keys, network, or install faster-whisper for an offline fallback."
        )

    segments = data.get("segments", [])
    words_total = sum(len(s.get("words", [])) for s in segments)
    log.log(f"  Transcription engine used: {data.get('_engine', 'unknown')}")

    log.section("FULL TRANSCRIPTION RESULT")
    log.log(f"  Total segments : {len(segments)}")
    log.log(f"  Total words    : {words_total}")
    if segments:
        log.log(f"  Duration       : {segments[0]['start']:.2f}s to {segments[-1]['end']:.2f}s")

    # Write the human-readable transcript to its OWN plain-text file, so the
    # diagnostic log stays clean and the transcription is easy to read/share.
    transcript_txt_path = os.path.join(job_dir, "transcript_full.txt")
    with open(transcript_txt_path, "w", encoding="utf-8") as tf:
        tf.write("FULL VIDEO TRANSCRIPT\n")
        tf.write(f"Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        if segments:
            tf.write(f"Duration: {segments[0]['start']:.2f}s to {segments[-1]['end']:.2f}s\n")
        tf.write(f"Segments: {len(segments)}  |  Words: {words_total}\n")
        tf.write("=" * 70 + "\n\n")
        # Timestamped view (one line per segment)
        for seg in segments:
            tf.write(f"[{seg['start']:7.2f} - {seg['end']:7.2f}]  {seg['text'].strip()}\n")
        # Clean prose view (no timestamps), handy for copy/paste
        tf.write("\n" + "=" * 70 + "\nPLAIN TEXT\n" + "=" * 70 + "\n\n")
        tf.write(" ".join(seg["text"].strip() for seg in segments).strip() + "\n")

    # The diagnostic log only points at the transcript file (no giant dump).
    log.log(f"  Transcript text saved: {transcript_txt_path}")

    transcript_path = os.path.join(job_dir, "transcript_full.json")
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    log.log(f"  Full transcript JSON saved: {transcript_path}")
    return transcript_path


# ─────────────────────────────────────────────────────────────
# Context-aware boundary snapping
# ─────────────────────────────────────────────────────────────

# Characters that mark the end of a complete spoken thought (Latin + Devanagari danda)
_SENTENCE_END_CHARS = (".", "!", "?", "।", "॥", "…")

# Client requirement: every clip must be at least MIN and at most MAX seconds long.
# These are the defaults; a caller can override per-job via options
# {"min_clip_len": 20, "max_clip_len": 40}. Defined here (before first use as a
# default argument value) so module import doesn't hit a forward reference.
DEFAULT_MIN_CLIP_LEN = 20.0
DEFAULT_MAX_CLIP_LEN = 40.0

# Hard limits on what the user is allowed to ask for. 7s is the shortest clip that
# can still land a joke; 90s (1:30) is the ceiling every short-form surface accepts
# (Reels, Shorts and TikTok all cut off past this). The UI slider spans exactly this
# range, and anything arriving over the API is clamped into it.
CLIP_LEN_FLOOR = 7.0
CLIP_LEN_CEIL = 90.0


def clamp_clip_bounds(min_len, max_len, log=None) -> tuple:
    """Force a (min, max) clip-length pair into [CLIP_LEN_FLOOR, CLIP_LEN_CEIL].

    Bad input never fails a job: unparseable values fall back to the defaults, and
    an inverted pair is swapped rather than rejected."""
    def _num(value, fallback):
        try:
            return float(value)
        except (TypeError, ValueError):
            return fallback

    lo = _num(min_len, DEFAULT_MIN_CLIP_LEN)
    hi = _num(max_len, DEFAULT_MAX_CLIP_LEN)
    if lo > hi:
        lo, hi = hi, lo
    lo = max(CLIP_LEN_FLOOR, min(CLIP_LEN_CEIL, lo))
    hi = max(CLIP_LEN_FLOOR, min(CLIP_LEN_CEIL, hi))
    if hi <= lo:
        # A zero-width window would make snapping impossible — give it room to breathe.
        hi = min(CLIP_LEN_CEIL, lo + 5.0)
        if hi <= lo:
            lo = max(CLIP_LEN_FLOOR, hi - 5.0)
    if log and (lo, hi) != (_num(min_len, lo), _num(max_len, hi)):
        log.log(f"  Clip length {min_len}–{max_len}s clamped to "
                f"{lo:.0f}–{hi:.0f}s (allowed {CLIP_LEN_FLOOR:.0f}–{CLIP_LEN_CEIL:.0f}s)")
    return lo, hi


def _ends_a_sentence(text: str) -> bool:
    text = (text or "").strip()
    return bool(text) and text[-1] in _SENTENCE_END_CHARS


def snap_to_sentence_boundaries(raw_start: float, raw_end: float, segments: list,
                                 min_dur: float = DEFAULT_MIN_CLIP_LEN,
                                 max_dur: float = DEFAULT_MAX_CLIP_LEN) -> tuple:
    """Snap a raw [start, end] window so the clip begins at the start of a spoken
    thought and ends on a COMPLETE sentence (never mid-context).

    Strategy:
      - Start: snap to the nearest segment start, but prefer one that begins a new
        sentence (i.e. the previous segment ended with sentence punctuation).
      - End: walk forward to the last segment that fits inside max_dur AND ends on
        sentence-ending punctuation. Only if no punctuated end exists do we fall
        back to a plain segment boundary. This is what stops clips being cut in the
        middle of a sentence.
    """
    if not segments:
        return raw_start, raw_end

    # --- choose a start that ideally opens a fresh sentence ---
    idx = min(range(len(segments)), key=lambda i: abs(segments[i]["start"] - raw_start))
    # Nudge to the nearest sentence-opening segment within a small window (<=2 segs).
    for back in range(0, 3):
        j = idx - back
        if j < 0:
            break
        prev_ok = (j == 0) or _ends_a_sentence(segments[j - 1].get("text", ""))
        if prev_ok:
            idx = j
            break
    clip_start = segments[idx]["start"]

    # --- extend the end, preferring a sentence-ending segment within max_dur ---
    last_fit_end = None          # last segment end that fits in max_dur (fallback)
    last_sentence_end = None     # last segment that fits AND ends a sentence (preferred)
    for seg in segments[idx:]:
        if seg["end"] - clip_start > max_dur:
            break
        last_fit_end = seg["end"]
        if seg["end"] - clip_start >= min_dur and _ends_a_sentence(seg.get("text", "")):
            last_sentence_end = seg["end"]

    if last_sentence_end is not None:
        clip_end = last_sentence_end
    elif last_fit_end is not None:
        clip_end = last_fit_end
    else:
        # Single very long segment: clamp to max_dur.
        clip_end = min(segments[idx]["end"], clip_start + max_dur)

    # Guarantee a minimum duration if we can.
    if clip_end - clip_start < min_dur:
        for seg in segments:
            if seg["start"] >= clip_end and seg["end"] - clip_start <= max_dur:
                clip_end = seg["end"]
                if clip_end - clip_start >= min_dur:
                    break

    clip_end = min(clip_end, clip_start + max_dur)
    return round(clip_start, 3), round(clip_end, 3)


# ─────────────────────────────────────────────────────────────
# Clip-count + dense coverage generation
# ─────────────────────────────────────────────────────────────

_ABS_MAX_CLIPS = 80          # hard safety ceiling on clips per video
_AI_MAX = 8                  # how many "best" clips we ask the LLM for
_DEDUP_TOL = 2.0             # clips within 2s start AND 2s end are "identical"

# Sequential ("Part 1, Part 2, …") mode splits the WHOLE video, so it needs a much
# higher ceiling than highlight mode: a 1-hour video at 30s per part is 120 clips.
_ABS_MAX_PARTS = 500
DEFAULT_CHUNK_LEN = 30.0     # seconds per part when the caller doesn't say


def _length_cycle(min_dur: float, max_dur: float) -> list:
    """Build a small set of varied target lengths that all sit inside
    [min_dur, max_dur], so coverage clips don't all come out the same length."""
    lo, hi = float(min_dur), float(max_dur)
    if hi <= lo:
        return [lo]
    # 4 evenly spaced lengths from min to max (inclusive)
    steps = 4
    return [round(lo + (hi - lo) * i / (steps - 1), 1) for i in range(steps)]


def _distinct(s: float, e: float, chosen: list, tol: float = _DEDUP_TOL) -> bool:
    """True if [s,e] differs from every already-chosen clip by more than tol on
    either the start or the end. Overlap is allowed; near-identical is not."""
    for c in chosen:
        if abs(c["start"] - s) < tol and abs(c["end"] - e) < tol:
            return False
    return True


def _sentence_start_indices(segments: list) -> list:
    """Indices of segments that begin a sentence (previous segment ended on
    punctuation). These make clean, context-safe clip starts."""
    idxs = [i for i, seg in enumerate(segments)
            if i == 0 or _ends_a_sentence(segments[i - 1].get("text", ""))]
    return idxs or list(range(len(segments)))


def _generate_dense_clips(segments: list, n_needed: int, chosen: list,
                          log, min_dur: float = DEFAULT_MIN_CLIP_LEN,
                          max_dur: float = DEFAULT_MAX_CLIP_LEN) -> list:
    """Produce up to n_needed coverage clips SPREAD OUT across the video with minimal
    overlap. Each starts at a sentence boundary, ends on a complete sentence, varies
    in length, and starts at least `min_gap` from every other clip."""
    if n_needed <= 0 or not segments:
        return []

    total = segments[-1]["end"] - segments[0]["start"]
    min_gap = max(min_dur, (total / max(1, n_needed)) * 0.85)

    length_cycle = _length_cycle(min_dur, max_dur)
    starts = _sentence_start_indices(segments)
    pool = []
    for k, si in enumerate(starts):
        a = segments[si]["start"]
        L = length_cycle[k % len(length_cycle)]
        s, e = snap_to_sentence_boundaries(a, a + L, segments, min_dur, max_dur)
        if e - s >= min_dur:
            pool.append((s, e))
    pool.sort()

    picked = []
    chosen_starts = [c["start"] for c in chosen]

    def far_enough(s):
        return all(abs(s - cs) >= min_gap for cs in chosen_starts) and \
               all(abs(s - p[0]) >= min_gap for p in picked)

    for (s, e) in pool:
        if len(picked) >= n_needed:
            break
        if far_enough(s):
            picked.append((s, e))

    # If the gap was too strict to reach the target, relax and top up by even spread.
    if len(picked) < n_needed:
        remaining = [p for p in pool if p not in picked]
        if remaining and n_needed > len(picked):
            stepf = max(1.0, len(remaining) / float(n_needed - len(picked)))
            i = 0.0
            while len(picked) < n_needed and int(i) < len(remaining):
                cand = remaining[int(i)]
                if _distinct(cand[0], cand[1], [{"start": p[0], "end": p[1]} for p in picked] + chosen):
                    picked.append(cand)
                i += stepf

    picked.sort()
    return [{"start": s, "end": e, "score": 6,
             "reason": f"Coverage clip ({e - s:.0f}s)"} for (s, e) in picked]


# ─────────────────────────────────────────────────────────────
# AI selection — MODULAR & CHUNKED
# ─────────────────────────────────────────────────────────────
# The free-tier LLM context is small, so a long transcript is split into chunks
# and each chunk is analysed separately, then the picks are merged. To use a
# paid/large-context model later, set these env vars — NO code change needed:
#   GROQ_SELECTION_MODEL          (e.g. a 128k-context model)
#   GROQ_SELECTION_MODEL_FALLBACK
#   SELECTION_CHUNK_CHARS=200000  (large => whole transcript in ONE call, no chunking)
SELECTION_MODEL_PRIMARY  = os.environ.get("GROQ_SELECTION_MODEL", "llama-3.3-70b-versatile")
SELECTION_MODEL_FALLBACK = os.environ.get("GROQ_SELECTION_MODEL_FALLBACK", "llama-3.1-8b-instant")
SELECTION_CHUNK_CHARS    = int(os.environ.get("SELECTION_CHUNK_CHARS", "6000"))

def _selection_prompt_header(min_len: float, max_len: float) -> str:
    """The selector's brief. Written around ONE objective — views — because that is
    what the client is optimising for. Length bounds are injected so the model is
    told the real window the renderer will enforce."""
    return f"""You are a viral short-form strategist for Instagram Reels and YouTube Shorts.
Your ONLY objective is VIEWS. You are not summarising the video and you are not
looking for the "most important" parts — you are hunting the moments that would make
a stranger stop scrolling, watch to the end, and send it to a friend.

WHAT ACTUALLY GOES VIRAL (in priority order):
1. STOPS THE SCROLL IN 3 SECONDS — the first spoken line must be a bold claim, a
   shocking number, conflict, a question the viewer needs answered, or raw emotion.
   A moment that needs 10 seconds of build-up is worthless no matter how good it gets.
2. EMOTIONAL SPIKE — anger, shock, laughter, secondhand embarrassment, awe, outrage.
   Neutral information does not travel. Strong feeling does.
3. SHAREABLE / ARGUABLE — would someone tag a friend, or fight in the comments?
   Opinions, hot takes, callouts and relatable pain beat balanced explanation.
4. SELF-CONTAINED — it must make full sense to someone who has never seen this video
   and has no idea who is talking.
5. PAYOFF BEFORE THE END — a punchline, twist, reveal or resolution lands inside the
   clip. If the moment only sets something up, it is not a clip.
6. ENDS ON A COMPLETE THOUGHT — never stop mid-sentence.

REJECT: intros, outros, greetings, sponsor reads, housekeeping, "as I was saying",
setup with no payoff, and anything that is merely informative but emotionally flat.

SCORING (1-10) — score PREDICTED VIEWS, not quality or importance:
  10  would genuinely take off — instant hook plus a strong emotional payoff
  7-9 strong scroll-stopper with a clear payoff
  4-6 watchable but nothing that compels a share
  1-3 flat, slow, or needs outside context — do not pick these

Be selective. Returning three excellent moments beats returning ten mediocre ones.

Each clip must be between {min_len:.0f} and {max_len:.0f} seconds and must start at the
beginning of a sentence."""

# Cap the user's brief so a pasted essay can't crowd the transcript out of the
# context window (the transcript is what the model actually has to reason over).
_MAX_CLIP_PROMPT_CHARS = 1200


def _clip_prompt_block(clip_prompt: str) -> str:
    """Render the user's free-text brief as the highest-priority instruction block.

    Returned empty when no brief was given, so the default behaviour is unchanged."""
    brief = (clip_prompt or "").strip()
    if not brief:
        return ""
    if len(brief) > _MAX_CLIP_PROMPT_CHARS:
        brief = brief[:_MAX_CLIP_PROMPT_CHARS].rstrip() + "…"
    return f"""

╔══ THE USER'S BRIEF — THIS OVERRIDES EVERYTHING ABOVE ══╗
{brief}
╚════════════════════════════════════════════════════════╝

HOW TO APPLY THE BRIEF:
- Only return moments that genuinely match the brief. Relevance to it beats raw virality.
- Score by how well the moment fits the brief (10 = perfect match, 1 = unrelated).
- In "reason", state explicitly how the moment matches the brief.
- If NOTHING in this transcript portion matches the brief, return an empty list:
  {{"highlights": []}} — do NOT pad with unrelated moments."""


def _chunk_segments(segments: list, budget_chars: int) -> list:
    """Split segments into consecutive groups whose combined text stays under
    budget_chars, so each group fits the LLM context window."""
    chunks, cur, cur_len = [], [], 0
    for seg in segments:
        line_len = len(seg.get("text", "")) + 24  # +timestamp overhead
        if cur and cur_len + line_len > budget_chars:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(seg)
        cur_len += line_len
    if cur:
        chunks.append(cur)
    return chunks


def _call_selection_llm(client, prompt: str, log: DiagnosticLog, prefer_model: str = "auto"):
    """Single LLM call with full provider fallback (Groq models -> Gemini).
    Returns parsed list or []."""
    raw = providers.chat(
        prompt, temperature=0.2, json_mode=True, log=log,
        groq_models=[SELECTION_MODEL_PRIMARY, SELECTION_MODEL_FALLBACK],
        prefer_model=prefer_model,
    )
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        hl = parsed.get("highlights", parsed) if isinstance(parsed, dict) else parsed
        return hl if isinstance(hl, list) else []
    except Exception as e:
        log.log(f"    selection: could not parse LLM JSON: {e}")
        return []


def select_highlights_chunked(segments: list, num_clips: int, per_chunk: int,
                              log: DiagnosticLog, clip_prompt: str = "",
                              min_len: float = DEFAULT_MIN_CLIP_LEN,
                              max_len: float = DEFAULT_MAX_CLIP_LEN,
                              prefer_model: str = "auto") -> list:
    """Run the LLM selector across transcript chunks and merge the picks.

    `clip_prompt` is the user's free-text description of the clips they want; when
    present it is injected as the top-priority instruction and the model is told to
    return nothing rather than pad with off-brief moments.
    Returns a list of raw {start, end, score, reason} (un-snapped)."""
    status = providers.provider_status()
    if not status["chat_ready"] or not segments:
        return []

    client = None  # providers.chat manages its own clients/fallback
    chunks = _chunk_segments(segments, SELECTION_CHUNK_CHARS)
    brief = _clip_prompt_block(clip_prompt)
    log.log(f"  AI selection: {len(segments)} segments -> {len(chunks)} chunk(s) "
            f"(model={prefer_model if prefer_model != 'auto' else SELECTION_MODEL_PRIMARY + ' (auto)'}, "
            f"~{SELECTION_CHUNK_CHARS} chars/chunk, {per_chunk} picks/chunk)")
    if brief:
        log.log(f"  User brief active: {clip_prompt.strip()[:200]}")

    all_picks = []
    for ci, chunk in enumerate(chunks):
        chunk_text = "".join(f"[{s['start']:.2f} - {s['end']:.2f}] {s['text']}\n" for s in chunk)
        prompt = f"""{_selection_prompt_header(min_len, max_len)}{brief}

TASK:
From the transcript portion below, return up to {per_chunk} of the strongest clip(s). Use the
EXACT timestamps shown (in seconds). Score each 1-10. For "reason", briefly note the hook and
the payoff.

Output ONLY valid JSON: {{"highlights": [{{"start": float, "end": float, "score": int, "reason": "string"}}]}}

TRANSCRIPT PORTION:
{chunk_text}"""
        picks = _call_selection_llm(client, prompt, log, prefer_model)
        log.log(f"    chunk {ci+1}/{len(chunks)}: {len(picks)} pick(s)")
        all_picks.extend(picks)

    return all_picks


# ─────────────────────────────────────────────────────────────
# Last-resort highlights: no transcript at all
# ─────────────────────────────────────────────────────────────

def probe_duration(path: str, log: DiagnosticLog) -> float:
    """Video duration in seconds via ffprobe, or 0.0 if it can't be determined."""
    r = providers.run_cmd(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        timeout=60, retries=2, log=log, label="ffprobe")
    try:
        return float((r.stdout or "").strip())
    except (TypeError, ValueError):
        return 0.0


def time_based_highlights(duration: float, num_clips: int,
                          min_len: float, max_len: float, log: DiagnosticLog) -> list:
    """Evenly spaced clips cut purely on the clock, used when NO transcript exists
    (every ASR provider failed). Sentence-boundary snapping is impossible without
    text, so this trades clean cuts for still shipping usable clips."""
    if duration <= 0:
        return []
    length = max(min_len, min(max_len, (min_len + max_len) / 2.0))
    # Skip the first/last 2% (intros and outros are rarely the good part).
    usable_start = duration * 0.02
    usable_end = max(usable_start + length, duration * 0.98)
    span = usable_end - usable_start

    fit = max(1, int(span // length))
    n = max(1, min(num_clips, fit))
    step = span / n

    out = []
    for i in range(n):
        s = usable_start + i * step
        e = min(s + length, duration)
        if e - s < min_len * 0.6:
            continue
        out.append({
            "start": round(s, 3), "end": round(e, 3), "score": 5,
            "reason": f"Time-based clip {i + 1} (no transcript available)",
        })
    log.log(f"  Fallback: generated {len(out)} time-based clip(s) across {duration:.0f}s")
    return out


# ─────────────────────────────────────────────────────────────
# Sequential "Part 1 / Part 2 / Part 3" splitting
# ─────────────────────────────────────────────────────────────

def sequential_parts(duration: float, chunk_len: float, log: DiagnosticLog,
                     label_format: str = "Part {n}") -> list:
    """Split the WHOLE video into back-to-back parts of `chunk_len` seconds.

    Every part starts exactly where the previous one ended — 0-30, 30-60, 60-90 …
    so playing them in order reproduces the original video with nothing missing
    and nothing repeated. This is deliberately NOT sentence-snapped: the promise
    here is gapless coverage, and nudging boundaries to sentences would either
    overlap or drop audio between parts.

    The trailing remainder becomes its own part when it is long enough to stand
    alone (>= 1/3 of a chunk, min 5s); otherwise it is merged into the last part
    so no footage is lost and no 2-second orphan clip is produced.
    """
    if duration <= 0:
        log.log("  Sequential split: duration unknown — cannot split")
        return []

    chunk_len = max(3.0, float(chunk_len))
    if chunk_len >= duration:
        log.log(f"  Sequential split: part length ({chunk_len:.0f}s) >= video "
                f"({duration:.0f}s) — emitting a single part")
        bounds = [(0.0, duration)]
    else:
        bounds = []
        n_full = int(duration // chunk_len)
        for i in range(n_full):
            bounds.append((i * chunk_len, (i + 1) * chunk_len))

        tail = duration - n_full * chunk_len
        min_tail = max(5.0, chunk_len / 3.0)
        if tail >= min_tail:
            bounds.append((n_full * chunk_len, duration))
        elif tail > 0.25 and bounds:
            # Too short to be its own part — absorb it into the final one.
            s, _e = bounds[-1]
            bounds[-1] = (s, duration)
            log.log(f"  Sequential split: {tail:.1f}s tail merged into the last part")

    if len(bounds) > _ABS_MAX_PARTS:
        log.log(f"  WARNING: {len(bounds)} parts exceeds the {_ABS_MAX_PARTS} ceiling — "
                f"keeping the first {_ABS_MAX_PARTS}. Use a longer part length for full coverage.")
        bounds = bounds[:_ABS_MAX_PARTS]

    parts = []
    for i, (s, e) in enumerate(bounds):
        n = i + 1
        try:
            label = label_format.format(n=n, total=len(bounds))
        except (KeyError, IndexError, ValueError):
            label = f"Part {n}"
        parts.append({
            "start": round(s, 3),
            "end": round(e, 3),
            "score": 10,                # order is meaningful here, not "quality"
            "part": n,
            "part_label": label,
            "reason": label,
        })

    total = sum(p["end"] - p["start"] for p in parts)
    log.log(f"  Sequential split: {len(parts)} part(s) of ~{chunk_len:.0f}s "
            f"covering {total:.0f}s of {duration:.0f}s")
    return parts


# ─────────────────────────────────────────────────────────────
# Hook-first: the 3-second cold open
# ─────────────────────────────────────────────────────────────
# Viral reels rarely open where the clip opens. They front-load the climax — the
# punchline, the reveal, the shout — for ~3 seconds, THEN roll the clip from its
# real beginning. The peak is therefore seen twice, which is the point: the viewer
# stays because they now know a payoff is coming.
#
# We pick that peak window INSIDE each clip (never from elsewhere in the video), so
# the cold open is always something the clip actually delivers on.

HOOK_LEN_DEFAULT = 3.0

# The hook flexes around the target so it can land on a complete spoken line. A
# fixed 3.00s cut is what made the old version feel like a random fragment.
HOOK_MIN_LEN = 1.6
HOOK_MAX_LEN = 5.5

# A hook only makes sense when the body is long enough to feel like a separate
# thing. Prepending 3s to an 8s clip just makes it stutter, so short clips are left
# alone and reported as skipped.
HOOK_MIN_CLIP_LEN = 10.0

# Clips per hook-picking call — the reply is one line each, so this can be generous.
_HOOK_BATCH = 14


def _segments_within(segments: list, start: float, end: float) -> list:
    """Segments overlapping [start, end], in order."""
    return [s for s in segments
            if s.get("end", 0) > start and s.get("start", 0) < end]


def _text_between(segments: list, start: float, end: float, limit: int = 220) -> str:
    txt = " ".join((s.get("text") or "").strip()
                   for s in _segments_within(segments, start, end)).strip()
    txt = " ".join(txt.split())
    return txt[:limit] + ("…" if len(txt) > limit else "")


def _words_between(segments: list, start: float, end: float) -> list:
    """Every word timestamp overlapping [start, end], in order."""
    out = []
    for seg in _segments_within(segments, start, end):
        for w in seg.get("words", []):
            if w.get("end", 0) > start and w.get("start", 0) < end:
                out.append(w)
    return out


def _snap_hook(anchor: float, segments: list, clip_start: float, clip_end: float,
               target_len: float):
    """Turn a rough anchor time into a hook window that is a COMPLETE SPOKEN PHRASE.

    A raw N-second slice is what made the old cold open feel like a random clip: it
    started halfway through one word and stopped halfway through another, so the
    viewer heard a fragment with no meaning. Instead:

      - start on the SEGMENT boundary at or just before the anchor, so the hook opens
        on the first word of a line rather than mid-word;
      - extend through whole segments until the window is at least the target length;
      - if that overshoots HOOK_MAX_LEN, cut back to the last WORD boundary that fits,
        so the hook still ends on a finished word rather than a clipped syllable.

    Returns (start, end) or None when the clip cannot host a sensible hook.
    """
    if clip_end - clip_start < HOOK_MIN_CLIP_LEN:
        return None

    inside = [s for s in segments
              if s.get("start", 0) >= clip_start - 0.01 and s.get("end", 0) <= clip_end + 0.01]
    if not inside:
        # No transcript for this stretch — fall back to a plain window, still clamped.
        hs = min(max(anchor, clip_start), max(clip_start, clip_end - target_len))
        he = min(hs + target_len, clip_end)
        return (round(hs, 3), round(he, 3)) if he - hs >= HOOK_MIN_LEN else None

    # Open on the line containing (or nearest before) the anchor.
    at_or_before = [s for s in inside if s["start"] <= anchor + 0.25]
    opener = at_or_before[-1] if at_or_before else inside[0]
    hs = float(opener["start"])

    # Grow through whole lines until we have enough to be worth watching.
    he = float(opener["end"])
    for seg in inside:
        if seg["start"] < opener["start"] - 0.01:
            continue
        he = float(seg["end"])
        if he - hs >= target_len:
            break

    # Too long: cut back to the last word that finishes inside the ceiling.
    if he - hs > HOOK_MAX_LEN:
        limit = hs + HOOK_MAX_LEN
        ends = [float(w["end"]) for w in _words_between(segments, hs, limit + 0.01)
                if float(w["end"]) <= limit + 0.01 and float(w["end"]) - hs >= HOOK_MIN_LEN]
        he = max(ends) if ends else limit

    he = min(he, clip_end)
    if he - hs < HOOK_MIN_LEN:
        return None
    return round(hs, 3), round(he, 3)


def _hook_heuristic(clip_start: float, clip_end: float, segments: list,
                    hook_len: float):
    """No-AI fallback: open on the line nearest the clip's payoff zone (~65% of the
    way in), which is where punchlines and reveals usually land."""
    if clip_end - clip_start < HOOK_MIN_CLIP_LEN:
        return None
    return _snap_hook(clip_start + (clip_end - clip_start) * 0.65,
                      segments, clip_start, clip_end, hook_len)


def _ask_llm_for_hooks(eligible: list, segments: list, hook_len: float,
                       log: DiagnosticLog, prefer_model: str = "auto") -> dict:
    """One call per batch. Returns {clip_id: hook_start_seconds}."""
    picks = {}
    batches = [eligible[i:i + _HOOK_BATCH] for i in range(0, len(eligible), _HOOK_BATCH)]

    for bi, batch in enumerate(batches):
        blocks = []
        for cid, h in batch:
            lines = "".join(
                f"    [{s['start']:.2f}] {(s.get('text') or '').strip()}\n"
                for s in _segments_within(segments, h["start"], h["end"])
            )
            blocks.append(
                f"CLIP {cid}  (plays from {h['start']:.2f}s to {h['end']:.2f}s — the hook "
                f"must start between {h['start']:.2f} and {h['end'] - hook_len:.2f})\n"
                f"{lines or '    (no speech)'}"
            )
        prompt = f"""You are editing viral Reels. Each clip below gets a COLD OPEN spliced onto
its front: you choose ONE LINE from inside the clip, that line plays first, and then
the whole clip plays from its real beginning.

PICK THE LINE THAT WOULD MAKE A STRANGER STOP SCROLLING. That means:
  - the punchline, the reveal, the twist, or the single most shocking sentence
  - a bold claim, a number, an insult, a confession, or raw emotion
  - a line that makes no sense on its own and MAKES YOU NEED THE CONTEXT

NEVER pick:
  - the clip's own first line (the viewer hears it two seconds later anyway)
  - filler, throat-clearing or narration: "so", "anyway", "now what is going on here",
    "let me tell you", "as I said", "moving on", "today I'm going to"
  - a question the clip never answers, or a line that is purely setup

Return the EXACT start timestamp of the chosen line, copied from the list. Pick the
line itself — the length is handled for you, so do not try to hit {hook_len:.0f} seconds.

Output ONLY valid JSON:
{{"hooks": [{{"clip": <id>, "start": <exact timestamp of the line>, "line": "first 5 words of it", "why": "under 8 words"}}]}}

CLIPS:
{chr(10).join(blocks)}"""

        raw = providers.chat(prompt, temperature=0.3, json_mode=True, log=log,
                             prefer_model=prefer_model)
        if not raw:
            log.log(f"    hook batch {bi + 1}/{len(batches)}: no reply — using heuristic")
            continue
        try:
            parsed = json.loads(raw)
            rows = parsed.get("hooks", parsed) if isinstance(parsed, dict) else parsed
        except (ValueError, TypeError) as e:
            log.log(f"    hook batch {bi + 1}/{len(batches)}: unparseable JSON ({e})")
            continue
        if not isinstance(rows, list):
            continue
        got = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                cid = int(row.get("clip"))
                hs = float(row.get("start"))
            except (TypeError, ValueError):
                continue
            note = str(row.get("why", "") or "").strip()
            line = str(row.get("line", "") or "").strip()
            picks[cid] = (hs, f"{note}  “{line}…”" if line else note)
            got += 1
        log.log(f"    hook batch {bi + 1}/{len(batches)}: {got} pick(s)")

    return picks


def select_hooks(highlights: list, segments: list, log: DiagnosticLog,
                 hook_len: float = HOOK_LEN_DEFAULT, prefer_model: str = "auto") -> int:
    """Choose a `hook_len`-second cold open inside each clip, in place.

    Writes `hook_start`, `hook_end` and `hook_text` onto every highlight that gets
    one. Clips shorter than HOOK_MIN_CLIP_LEN are skipped. Returns how many hooks
    were set.
    """
    log.section(f"HOOK-FIRST SELECTION ({hook_len:.0f}s cold open)")
    if not highlights:
        return 0

    hook_len = max(1.0, min(float(hook_len), 10.0))

    eligible, skipped = [], 0
    for cid, h in enumerate(highlights):
        if (h["end"] - h["start"]) < max(HOOK_MIN_CLIP_LEN, hook_len * 2.5):
            skipped += 1
            continue
        eligible.append((cid, h))

    if skipped:
        log.log(f"  {skipped} clip(s) too short for a cold open "
                f"(under {max(HOOK_MIN_CLIP_LEN, hook_len * 2.5):.0f}s) — left as-is")
    if not eligible:
        log.log("  No clip is long enough for a hook.")
        return 0

    picks = {}
    if segments and providers.provider_status().get("chat_ready"):
        log.log(f"  Asking the AI for the peak moment in {len(eligible)} clip(s)…")
        picks = _ask_llm_for_hooks(eligible, segments, hook_len, log, prefer_model)
    elif not segments:
        log.log("  No transcript — using the payoff-zone heuristic for every clip.")
    else:
        log.log("  No chat provider — using the payoff-zone heuristic for every clip.")

    made = 0
    for cid, h in eligible:
        why = ""
        window = None

        if cid in picks:
            hs, why = picks[cid]
            # The anchor only has to land inside the clip — _snap_hook grows it out
            # to a whole line, so a timestamp near the end is still usable.
            if h["start"] - 0.5 <= hs < h["end"]:
                window = _snap_hook(hs, segments, h["start"], h["end"], hook_len)
                if window is None:
                    log.log(f"    Clip {cid + 1}: no complete line at {hs:.2f}s — using heuristic")
                    why = ""
            else:
                log.log(f"    Clip {cid + 1}: AI hook {hs:.2f}s is outside "
                        f"[{h['start']:.2f}, {h['end']:.2f}] — using heuristic")
                why = ""

        if window is None:
            window = _hook_heuristic(h["start"], h["end"], segments, hook_len)
        if window is None:
            continue

        h["hook_start"], h["hook_end"] = window
        h["hook_text"] = _text_between(segments, *window) if segments else ""
        made += 1
        offset = h["hook_start"] - h["start"]
        log.log(f"    Clip {cid + 1}: hook at +{offset:.1f}s into the clip "
                f"({h['hook_start']:.2f}s–{h['hook_end']:.2f}s, "
                f"{h['hook_end'] - h['hook_start']:.1f}s)"
                f"{'  | ' + why if why else ''}")
        if h.get("hook_text"):
            log.log(f"              \"{h['hook_text'][:80]}\"")

    log.log(f"\n  {made}/{len(highlights)} clip(s) will open with a "
            f"{hook_len:.0f}s cold open.")
    return made


# ─────────────────────────────────────────────────────────────
# Highlight Engine (entry point)
# ─────────────────────────────────────────────────────────────

def get_ai_highlights(transcript_path: str, job_dir: str, log: DiagnosticLog,
                      options: dict = None) -> tuple:
    log.section("AI HIGHLIGHT SELECTION")
    api_key = os.environ.get("GROQ_API_KEY")
    highlights_path = os.path.join(job_dir, "highlights.json")

    options = options or {}

    # Read the transcript FIRST so the clip count can scale with video length.
    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        segments = data.get("segments", [])
    except Exception as e:
        log.error(f"Failed to read transcript: {e}", e)
        segments = []

    total_duration = (segments[-1]["end"] - segments[0]["start"]) if segments else 60.0
    minutes = max(1, round(total_duration / 60.0))
    log.log(f"  Total video duration: {total_duration:.2f}s (~{minutes} min)")

    # ── Clip mode ──────────────────────────────────────────────────────────────
    #   "multi" (default) — maximise coverage: ~1 clip per minute, each 20-40s.
    #   "best"            — quality over quantity: only the most interesting
    #                       ~half-as-many moments, each a longer 40-60s clip.
    clip_mode = str(options.get("clip_mode", "multi")).lower()
    if clip_mode not in ("multi", "best"):
        clip_mode = "multi"
    log.log(f"  Clip mode: {clip_mode}")

    # How many clips to produce:
    #   - "auto" (default): one clip per minute of video (15-min video -> ~15 clips).
    #     In "best" mode this halves to the strongest moments only.
    #   - an explicit number: used directly, but never more than the per-mode cap.
    if clip_mode == "best":
        max_for_video = max(1, min(math.ceil(minutes / 2), _ABS_MAX_CLIPS))
    else:
        max_for_video = max(1, min(minutes, _ABS_MAX_CLIPS))
    raw_nc = options.get("num_clips", "auto")
    auto_mode = bool(options.get("auto_clips")) or str(raw_nc).strip().lower() in ("", "auto", "0", "none")
    if auto_mode:
        num_clips = max_for_video
    else:
        try:
            num_clips = int(raw_nc)
        except (TypeError, ValueError):
            num_clips = max_for_video
            auto_mode = True
        num_clips = max(1, min(num_clips, max_for_video))
    ai_target = num_clips   # let the AI fill as many as it can; coverage tops up the rest
    log.log(f"  Target clips: {num_clips} ({'auto' if auto_mode else 'manual'}, max {max_for_video} = 1/min) | "
            f"AI picks first, coverage fills remainder")

    # Per-clip length bounds — the user picks these anywhere in 7s-1:30 via the UI
    # slider, and whatever arrives is clamped into that range.
    #
    # "best" mode still leans longer, but ONLY when the caller never expressed a
    # preference: an explicit request always wins, otherwise the mode would silently
    # ignore the slider the user just moved.
    asked_min = options.get("min_clip_len")
    asked_max = options.get("max_clip_len")
    untouched = (asked_min in (None, "", DEFAULT_MIN_CLIP_LEN)
                 and asked_max in (None, "", DEFAULT_MAX_CLIP_LEN))
    if clip_mode == "best" and untouched:
        min_len, max_len = 40.0, 60.0
    else:
        min_len, max_len = clamp_clip_bounds(asked_min, asked_max, log)
    log.log(f"  Clip length bounds: {min_len:.0f}s min / {max_len:.0f}s max")

    valid = []

    # Run the modular, chunked LLM selector and turn its picks into validated clips.
    per_chunk = max(2, math.ceil(num_clips / max(1, math.ceil(
        (len(segments) and sum(len(s.get('text','')) for s in segments) or 1) / SELECTION_CHUNK_CHARS))))
    clip_prompt = str(options.get("clip_prompt", "") or "").strip()
    raw_picks = select_highlights_chunked(segments, num_clips, per_chunk, log,
                                          clip_prompt=clip_prompt,
                                          min_len=min_len, max_len=max_len,
                                          prefer_model=str(options.get("selection_model", "auto")))

    for h in raw_picks:
        try:
            raw_s = float(h.get("start", 0))
            raw_e = float(h.get("end", 0))
        except (TypeError, ValueError):
            continue
        snapped_s, snapped_e = snap_to_sentence_boundaries(raw_s, raw_e, segments, min_len, max_len)
        dur = snapped_e - snapped_s
        if dur >= min_len and _distinct(snapped_s, snapped_e, valid):
            valid.append({
                "start": snapped_s, "end": snapped_e,
                "score": int(h.get("score", 8)) if str(h.get("score", 8)).isdigit() else 8,
                "reason": h.get("reason", "AI-selected highlight"),
            })
    valid.sort(key=lambda v: v.get("score", 0), reverse=True)
    log.log(f"  AI produced {len(valid)} valid, distinct clip(s)")

    # Fill out to the requested count with coverage clips. These may overlap the
    # AI picks and each other, but each is distinct in start point, length, and/or
    # ending (never identical), and always cut on sentence boundaries.
    #
    # When the user gave a brief, generic coverage clips would defeat it — the point
    # is clips ABOUT something. So we only fall back to coverage if the brief matched
    # nothing at all, which beats handing back an empty result.
    if len(valid) < num_clips and segments:
        if clip_prompt and valid:
            log.log(f"\n  Brief active: keeping the {len(valid)} matching clip(s) only "
                    f"(not padding to {num_clips} with unrelated coverage clips).")
        else:
            if clip_prompt:
                log.log("\n  Brief matched nothing — falling back to general coverage clips.")
            needed = num_clips - len(valid)
            coverage = _generate_dense_clips(segments, needed, valid, log, min_len, max_len)
            log.log(f"  Coverage fill: requested {needed} more, generated {len(coverage)}")
            valid.extend(coverage)

    valid = sorted(valid, key=lambda v: v.get("score", 0), reverse=True)[:num_clips]
    log.log(f"\n  Final clip selection ({len(valid)} clips), sorted by predicted performance:")
    for i, v in enumerate(valid):
        log.log(f"    Clip {i+1}: {v['start']:.2f}s -> {v['end']:.2f}s  "
                f"({v['end']-v['start']:.1f}s)  score={v['score']}  | {v['reason']}")

    with open(highlights_path, "w", encoding="utf-8") as f:
        json.dump(valid, f, indent=4, ensure_ascii=False)

    return highlights_path, valid


# ─────────────────────────────────────────────────────────────
# Cut raw clips (NO subtitles burned - that happens in a separate script)
# ─────────────────────────────────────────────────────────────

def cut_raw_clips(video_path: str, highlights: list, job_dir: str, log: DiagnosticLog) -> list:
    """Cuts each highlight range from the source video into its own file,
    without burning subtitles. Returns list of dicts with paths + metadata."""
    log.section("CLIP CUTTING (raw, no subtitles)")
    clips_dir = os.path.join(job_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    log.log(f"   Video path      : {video_path}")
    log.log(f"   Video exists    : {os.path.exists(video_path)}")
    log.log(f"   Total highlights: {len(highlights)}")

    raw_clips = []

    for i, clip in enumerate(highlights):
        start_time = clip["start"]
        end_time = clip["end"]
        clip_index = i + 1
        raw_output = os.path.join(clips_dir, f"viral_clip_{clip_index}_raw.mp4")

        log.log(f"\n   Clip {clip_index}")
        log.log(f"     Range  : {start_time:.3f}s -> {end_time:.3f}s  ({end_time-start_time:.1f}s)")
        log.log(f"     Reason : {clip.get('reason','')}")
        log.log(f"     Output : {raw_output}")

        # Output-seeking (-i before -ss) gives a frame-accurate cut (re-encoded anyway,
        # so no speed penalty from avoiding input-seek). avoid_negative_ts ensures the
        # clip starts at exactly t=0, so a later re-transcription's word timestamps
        # line up 1:1 with the rendered frames (no subtitle drift downstream).
        command = [
            "ffmpeg",
            "-i", video_path,
            "-ss", str(start_time),
            "-to", str(end_time),
            "-avoid_negative_ts", "make_zero",
            "-c:v", "libx264",
            "-preset", "medium",
            "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-y", raw_output,
        ]
        log.log(f"     FFmpeg cmd: {' '.join(command)}")

        result = subprocess.run(command, capture_output=True, text=True)
        log.log(f"     FFmpeg return code: {result.returncode}")
        if result.returncode != 0:
            log.log(f"     FFMPEG STDERR:\n{result.stderr[-1500:]}")
            log.log(f"     FAILED (non-zero return code)")
            continue

        if not os.path.exists(raw_output):
            log.log(f"     FAILED - output file does not exist")
            continue

        file_size = os.path.getsize(raw_output)
        log.log(f"     Output file size: {file_size} bytes")
        if file_size < 1000:
            log.log(f"     FAILED - output file too small ({file_size} bytes)")
            continue

        log.log(f"     SUCCESS -> {raw_output}")
        raw_clips.append({
            "index": clip_index,
            "raw_path": raw_output,
            "start": start_time,
            "end": end_time,
            "reason": clip.get("reason", ""),
            "score": clip.get("score", 0),
        })

    return raw_clips


# ─────────────────────────────────────────────────────────────
# Main entry point - clip selection only
# ─────────────────────────────────────────────────────────────

def execute_selection_workflow(
    url: str = None,
    local_file_path: str = None,
    options: dict = None,
    status_callback=None,
) -> tuple:
    """Runs download -> transcribe -> AI highlight selection -> cut raw clips.

    Produces a job_dir containing:
      - raw_video.mp4 (or symlinked/copied local file reference)
      - audio.mp3
      - transcript_full.json   (full-video transcript, for reference)
      - highlights.json        (selected highlight ranges + scores + reasons)
      - clips/viral_clip_N_raw.mp4   (cut, no subtitles)
      - clips_manifest.json    (everything a subtitle-burning script needs)
      - DIAGNOSTIC_REPORT.txt

    A second script can point at job_dir and read clips_manifest.json to
    pick up subtitle generation + burning from here.
    """
    job_id  = str(uuid.uuid4())
    # Absolute so the pipeline works regardless of the server's working directory.
    job_dir = os.path.join(BASE_DIR, "output", job_id)
    os.makedirs(job_dir, exist_ok=True)

    log = DiagnosticLog(job_dir)
    log.section("JOB INFO")
    log.log(f"   Job ID   : {job_id}")
    log.log(f"   Job dir  : {job_dir}")
    log.log(f"   Input URL: {url or 'N/A'}")
    log.log(f"   Local    : {local_file_path or 'N/A'}")

    if options is None:
        options = {"viral": True, "emotional": True, "key": True, "trend": False, "num_clips": 3}
    options.setdefault("num_clips", 3)
    # Clip selection mode: "multi" (~1 clip/min, 20-40s) or "best" (~n/2 clips, 40-60s).
    options.setdefault("clip_mode", "multi")
    # Free-text brief describing the clips the user wants ('' = no brief, pick generally).
    options.setdefault("clip_prompt", "")
    # Clip-length bounds. The user sets these anywhere in 7s-1:30; "best" mode only
    # overrides them when the caller left both at the defaults.
    options.setdefault("min_clip_len", DEFAULT_MIN_CLIP_LEN)
    options.setdefault("max_clip_len", DEFAULT_MAX_CLIP_LEN)
    # Hook-first: splice the clip's own peak moment onto the front as a cold open.
    options.setdefault("hook_first", True)
    options.setdefault("hook_len", HOOK_LEN_DEFAULT)
    # Post-render passes (run in burn_subtitles once every clip is cut):
    #   viral_council — 3 personas + judge rank the clips by predicted views
    #   publish_kit   — titles, caption, hashtags and a posting guide per clip
    options.setdefault("viral_council", True)
    options.setdefault("publish_kit", True)
    # Subtitle/caption preferences — these don't affect selection, but we persist
    # them into the manifest so the burn stage (burn_subtitles.py) can read them.
    options.setdefault("burn_subtitles", True)
    options.setdefault("subtitle_layout", "single")        # "single" | "dual"
    options.setdefault("subtitle_language", "hindi")        # "hindi" | "english" | "hinglish"
    options.setdefault("subtitle_position", "bottom")       # "top" | "middle" | "bottom"
    options.setdefault("caption_style", "outline")          # outline|box|white_box|bold_yellow|karaoke|word_pop
    options.setdefault("caption_accent", "")                # '#RRGGBB' highlight for the karaoke/word_pop active word
    options.setdefault("caption_color", "")                 # '#RRGGBB' caption text colour ('' = the style's own)
    options.setdefault("caption_words", 0)                  # words on screen at once (1-8; 0 = per-style default)
    options.setdefault("title_font", "")                    # English-font key for the AI headline ("" = same as captions)
    options.setdefault("title_style", "")                   # caption-style key for the headline ("" = the original yellow caps)
    options.setdefault("title_color", "")                   # '#RRGGBB' headline colour ("" = from the style)
    options.setdefault("aspect", "9:16")                    # 9:16 | 4:5 | 1:1 | 16:9
    options.setdefault("fit", "fit")                        # fit = letterbox, fill = crop to cover
    options.setdefault("selection_model", "auto")           # which LLM picks the clips (providers.SELECTION_MODELS)
    options.setdefault("hindi_font", "")                    # noto|mukta|hind|rozha|kalam ('' = auto)
    options.setdefault("english_font", "")                  # poppins|anton|bebas|archivo|fjalla ('' = auto)
    options.setdefault("video_quality", "best")             # best|1080|720|480|360 (link downloads)
    options.setdefault("show_title", False)
    # Sequential ("Part 1, Part 2, …") mode.
    options.setdefault("chunk_len", DEFAULT_CHUNK_LEN)      # seconds per part
    options.setdefault("part_label_format", "Part {n}")     # {n} = number, {total} = count
    options.setdefault("series_title", "")                  # fixed title burned on every part
    options.setdefault("show_part_label", True)
    # Free placement for the three overlays; None = use the preset position.
    options.setdefault("caption_xy", None)
    options.setdefault("title_xy", None)
    options.setdefault("part_xy", None)
    # Logo / watermark PNG. logo_path is an absolute path on this machine (the API
    # resolves the uploaded filename before it gets here); '' means no watermark.
    options.setdefault("logo_path", "")
    options.setdefault("logo_scale", 0.18)      # share of frame width
    options.setdefault("logo_xy", None)         # {"x":0..1,"y":0..1}, centre-anchored
    options.setdefault("logo_opacity", 1.0)
    log.log(f"   Options  : {options}")

    raw_clips = []
    highlights = []

    try:
        if local_file_path:
            if status_callback: status_callback("Step 1/4: Loading local video file...")
            log.section("STEP 1 - VIDEO INPUT")
            log.log(f"   Using local file: {local_file_path}")
            log.log(f"   File exists      : {os.path.exists(local_file_path)}")
            log.log(f"   File size        : {os.path.getsize(local_file_path) if os.path.exists(local_file_path) else 'N/A'} bytes")
            video_path = local_file_path
        else:
            if status_callback: status_callback("Step 1/4: Downloading video...")
            log.section("STEP 1 - VIDEO DOWNLOAD")
            # yt-dlp reports per-file percentages; forward them straight to the
            # same status line the rest of the pipeline writes to.
            video_path = download_video(
                url, job_dir, log, options.get("video_quality", "best"),
                progress_callback=(lambda msg, pct: status_callback(f"Step 1/4: {msg}"))
                                  if status_callback else None)

        # ── SEQUENTIAL MODE ──────────────────────────────────────────────────
        # Splitting the whole video into Part 1 / Part 2 / Part 3 needs no AI and
        # no transcript — only the duration. Transcription runs ONLY when captions
        # are switched on, which makes a captionless 1-hour split near-instant.
        sequential = str(options.get("clip_mode", "multi")).lower() == "sequential"
        full_audio_path = None
        transcript_path = None

        if sequential:
            log.section("STEP 2 - SEQUENTIAL SPLIT")
            if status_callback: status_callback("Step 2/4: Measuring video length...")
            duration = probe_duration(video_path, log)
            chunk_len = options.get("chunk_len", DEFAULT_CHUNK_LEN)
            try:
                chunk_len = float(chunk_len)
            except (TypeError, ValueError):
                chunk_len = DEFAULT_CHUNK_LEN
            log.log(f"   Video duration : {duration:.1f}s")
            log.log(f"   Part length    : {chunk_len:.0f}s")
            highlights = sequential_parts(
                duration, chunk_len, log,
                label_format=str(options.get("part_label_format") or "Part {n}"))
            if not highlights:
                raise RuntimeError(
                    "Could not split the video: its duration could not be read. "
                    "The file may be corrupt or still downloading.")
            with open(os.path.join(job_dir, "highlights.json"), "w", encoding="utf-8") as f:
                json.dump(highlights, f, indent=4, ensure_ascii=False)

            if options.get("burn_subtitles", True):
                if status_callback: status_callback("Step 3/4: Extracting audio...")
                log.section("STEP 3 - AUDIO + TRANSCRIPTION (for captions)")
                full_audio_path = extract_audio(
                    video_path, os.path.join(job_dir, "audio.mp3"), log)
                try:
                    transcript_path = transcribe_full_video(full_audio_path, job_dir, log)
                except Exception as e:
                    log.error(f"Transcription unavailable: {e}", e)
                    log.log("   Captions disabled for this job — parts will still be cut.")
                    options["burn_subtitles"] = False
            else:
                log.log("   Captions are OFF — skipping audio extraction and transcription "
                        "entirely (nothing to transcribe).")
                if status_callback:
                    status_callback("Captions off — cutting parts directly...")

        else:
            if status_callback: status_callback("Step 2/4: Extracting audio...")
            log.section("STEP 2 - AUDIO EXTRACTION")
            full_audio_path = extract_audio(video_path, os.path.join(job_dir, "audio.mp3"), log)

            # ── STAGE 2: ONE Whisper transcription of the full video (for selection only).
            # We deliberately do NOT run Deepgram on the whole video — that bills the full
            # duration. Deepgram runs later, per selected clip only (much cheaper here).
            if status_callback: status_callback("Step 3/4: Transcribing full video (Whisper)...")
            log.section("STEP 3 - FULL TRANSCRIPTION")
            try:
                transcript_path = transcribe_full_video(full_audio_path, job_dir, log)
            except Exception as e:
                log.error(f"Transcription unavailable: {e}", e)

        # ── STAGE 3: AI clip selection (chunked LLM; no video cutting here) ──
        # Sequential mode already decided its parts above and skips this entirely.
        if not sequential:
            if status_callback:
                status_callback("Step 4/4: AI is finding the most engaging moments...")
            if transcript_path:
                _, highlights = get_ai_highlights(transcript_path, job_dir, log, options)
            else:
                # LAST RESORT: every ASR provider failed. Rather than returning nothing,
                # cut evenly spaced clips off the clock. Captions are force-disabled for
                # this job since there is no text to render.
                log.section("FALLBACK - TIME-BASED SELECTION (no transcript)")
                log.log("   All transcription providers failed. Cutting time-based clips and "
                        "disabling captions for this job.")
                if status_callback:
                    status_callback("Transcription unavailable — cutting time-based clips...")
                duration = probe_duration(video_path, log)
                minutes = max(1, round(duration / 60.0)) if duration else 1
                raw_nc = options.get("num_clips", "auto")
                try:
                    want = int(raw_nc)
                except (TypeError, ValueError):
                    want = minutes   # "auto" and any unparseable value scale with length
                highlights = time_based_highlights(
                    duration, min(want, _ABS_MAX_CLIPS),
                    float(options.get("min_clip_len", DEFAULT_MIN_CLIP_LEN)),
                    float(options.get("max_clip_len", DEFAULT_MAX_CLIP_LEN)), log)
                options["burn_subtitles"] = False
                with open(os.path.join(job_dir, "highlights.json"), "w", encoding="utf-8") as f:
                    json.dump(highlights, f, indent=4, ensure_ascii=False)

        # ── STAGE 3b: pick each clip's 3-second cold open ────────────────────
        # Skipped in sequential mode on purpose: parts promise gapless, in-order
        # coverage of the source, and splicing a replayed peak onto the front of
        # "Part 3" would break exactly that promise.
        hooks_made = 0
        if options.get("hook_first", True) and not sequential and highlights:
            if status_callback:
                status_callback("Finding each clip's 3-second hook...")
            hook_segments = []
            if transcript_path:
                try:
                    with open(transcript_path, "r", encoding="utf-8") as f:
                        hook_segments = json.load(f).get("segments", [])
                except (OSError, ValueError) as e:
                    log.error(f"Could not re-read transcript for hook selection: {e}")
            hooks_made = select_hooks(highlights, hook_segments, log,
                                      hook_len=options.get("hook_len", HOOK_LEN_DEFAULT),
                                      prefer_model=str(options.get("selection_model", "auto")))
            with open(os.path.join(job_dir, "highlights.json"), "w", encoding="utf-8") as f:
                json.dump(highlights, f, indent=4, ensure_ascii=False)

        # NOTE: We do NOT cut raw clips anymore. The burn stage seeks into the source
        # video directly (-ss/-to) and cuts + scales + burns in a single ffmpeg pass,
        # which removes an entire encode/IO round-trip per clip.
        clip_entries = [{
            "index": i + 1,
            "raw_path": video_path,      # burn stage seeks into the source video
            "start": h["start"],
            "end": h["end"],
            "reason": h.get("reason", ""),
            "score": h.get("score", 0),
            # Sequential mode only: the "Part 3" text and its ordinal. Absent for
            # highlight clips, which have no meaningful running order.
            "part": h.get("part"),
            "part_label": h.get("part_label", ""),
            # Hook-first: absolute source times of the peak moment replayed as the
            # cold open. Absent when this clip did not get one.
            "hook_start": h.get("hook_start"),
            "hook_end": h.get("hook_end"),
            "hook_text": h.get("hook_text", ""),
        } for i, h in enumerate(highlights)]
        raw_clips = clip_entries

        # Write manifest for the next (subtitle) script
        manifest = {
            "job_id": job_id,
            "job_dir": job_dir,
            "video_path": video_path,
            "audio_path": full_audio_path,
            "transcript_path": transcript_path,
            "source_is_full_video": True,   # raw_path points at the full source
            "clip_prompt": options.get("clip_prompt", ""),
            "has_transcript": bool(transcript_path),
            "clip_mode": options.get("clip_mode", "multi"),
            "sequential": sequential,
            # Hook-first cold open: how many clips got one, and how long it is.
            "hook_first": bool(options.get("hook_first", True)) and not sequential,
            "hook_len": float(options.get("hook_len", HOOK_LEN_DEFAULT)),
            "hooks_made": hooks_made,
            "selection_model": options.get("selection_model", "auto"),
            # Post-render extras the burn stage runs once all clips are cut.
            "viral_council": bool(options.get("viral_council", True)),
            "publish_kit": bool(options.get("publish_kit", True)),
            # Caption/subtitle preferences chosen by the client, read by burn_subtitles.py.
            "subtitle_options": {
                "burn_subtitles":    options.get("burn_subtitles", True),
                "subtitle_layout":   options.get("subtitle_layout", "single"),
                "subtitle_language": options.get("subtitle_language", "hindi"),
                "subtitle_position": options.get("subtitle_position", "bottom"),
                "caption_style":     options.get("caption_style", "outline"),
                "caption_accent":    options.get("caption_accent", ""),
                "caption_color":     options.get("caption_color", ""),
                "caption_words":     options.get("caption_words", 0),
                "title_font":        options.get("title_font", ""),
                "title_style":       options.get("title_style", ""),
                "title_color":       options.get("title_color", ""),
                "aspect":            options.get("aspect", "9:16"),
                "fit":               options.get("fit", "fit"),
                "hindi_font":        options.get("hindi_font", ""),
                "english_font":      options.get("english_font", ""),
                "show_title":        options.get("show_title", False),
                # Sequential extras: a fixed title shown on every part, and the
                # "Part N" badge.
                "series_title":      options.get("series_title", ""),
                "show_part_label":   options.get("show_part_label", True),
                # Free placement (drag & drop in the UI). Each is {"x":0..1,"y":0..1}
                # in 9:16 frame fractions, or None to use the preset position.
                "caption_xy":        options.get("caption_xy"),
                "title_xy":          options.get("title_xy"),
                "part_xy":           options.get("part_xy"),
                # Watermark PNG composited above the captions on every clip.
                "logo_path":         options.get("logo_path", ""),
                "logo_scale":        options.get("logo_scale", 0.18),
                "logo_xy":           options.get("logo_xy"),
                "logo_opacity":      options.get("logo_opacity", 1.0),
            },
            "clips": clip_entries,
        }
        manifest_path = os.path.join(job_dir, "clips_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=4, ensure_ascii=False)
        log.log(f"\nManifest saved: {manifest_path}")

    except Exception as e:
        log.section("SELECTION PIPELINE CRASHED")
        log.error(f"Unhandled exception: {e}", e)
        raw_clips = []
        highlights = []

    log.finalize(raw_clips)

    return raw_clips, highlights, log.path
