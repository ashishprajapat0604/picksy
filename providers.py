"""
providers.py — provider-agnostic, fault-tolerant building blocks shared by
select_clips.py and burn_subtitles.py.

The goal is ROBUSTNESS: a single provider hiccup (Groq/Deepgram 500s, rate
limits, network blips) must never sink a job. Every capability here is a
*chain* of independent providers that are tried in order until one succeeds.

Capabilities
------------
  chat()                  Text/JSON completion. Chain: Gemini -> Groq ->
                          OpenRouter. Order: CHAT_ORDER env.
  transcribe_audio()      Word-level transcription. Chain: Deepgram nova-3 ->
                          Groq Whisper -> local faster-whisper (offline).
                          Order: TRANSCRIBE_ORDER env.
  run_cmd()               subprocess with a timeout and retry/backoff, so a
                          transient ffmpeg/yt-dlp failure is retried instead of
                          killing the job.
  select_video_encoder()  Picks the fastest available ffmpeg encoder
                          (NVENC -> AMF -> QSV -> libx264), with a hard CPU
                          fallback that ALWAYS works.
  provider_status()       What is actually configured/reachable right now —
                          powers the UI's provider badges.

All functions degrade gracefully: missing API key / missing package / provider
error simply moves on to the next link in the chain.
"""

import os
import re
import json
import time
import shutil
import subprocess

import joblog

# Load .env here rather than only in app.py: every entry point (the server, the
# burn_subtitles.py CLI, run.py's self-test) imports this module, so keys are
# always present no matter how the pipeline is started.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass


# ─────────────────────────────────────────────────────────────
# Small logging shim so this module works with or without the
# DiagnosticLog objects used elsewhere (it only needs `.log`).
# ─────────────────────────────────────────────────────────────

def _say(log, msg):
    try:
        if log is not None:
            log.log(msg)
        else:
            print(msg)
    except Exception:
        print(msg)


# ─────────────────────────────────────────────────────────────
# CHAT  (Google Gemini  ->  Groq  ->  OpenRouter)
# ─────────────────────────────────────────────────────────────

DEFAULT_GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# A non-retryable HTTP status means "trying again won't help" (auth/bad request).
_NON_RETRYABLE = (400, 401, 403)


def _retry_after_seconds(err, fallback):
    """How long to wait after a 429, read from the provider's own headers.

    Groq's free tier is token-per-minute limited, and it says exactly how long the
    caller must wait. Sleeping a blind 2s and giving up — which is what this used to
    do — turns one throttled call into a failed job, so honour the header when it is
    there and cap it so a pathological value cannot stall the pipeline."""
    headers = getattr(getattr(err, "response", None), "headers", None) or \
              getattr(err, "headers", None) or {}
    for name in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        try:
            raw = (headers.get(name) or "").strip()
        except Exception:
            raw = ""
        if not raw:
            continue
        secs = _parse_duration(raw)
        if secs is not None and secs > 0:
            return min(75.0, max(1.0, secs + 1.0))
    return fallback


# Order matters: 'ms' must be tried before 'm', or "185ms" reads as 185 MINUTES.
_DURATION_UNITS = (("ms", 0.001), ("h", 3600.0), ("m", 60.0), ("s", 1.0))
_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(ms|h|m|s)", re.I)


def _parse_duration(raw):
    """Seconds from a Retry-After value, or None if it is not a duration.

    Two formats show up in practice and they must not be confused. HTTP's own
    Retry-After is a bare integer count of seconds ("60"), while Groq reports
    Go-style compound durations in its rate-limit headers — "2m52.8s", "6s", and
    very commonly sub-second values like "185ms". Reading those milliseconds as
    minutes is a 60000x error in the direction that hurts: it would park the
    pipeline for the full backoff cap on a header that meant "carry on in a fifth
    of a second"."""
    try:
        return float(raw)          # bare seconds, per the HTTP spec
    except (TypeError, ValueError):
        pass
    total, matched = 0.0, False
    for value, unit in _DURATION_TOKEN.findall(str(raw)):
        for name, mult in _DURATION_UNITS:
            if unit.lower() == name:
                total += float(value) * mult
                matched = True
                break
    return total if matched else None


def _groq_chat(prompt, temperature, json_mode, models, log):
    """Try each Groq model with rate-limit-aware retry/backoff. Returns text or None.

    Groq's free tier caps llama-3.3-70b at 12k tokens/minute, and a Hindi transcript
    tokenises at roughly 0.5 tokens per character — so a couple of chunks in a row can
    exhaust the whole minute. A 429 here is normal and recoverable, and is treated as
    such: wait out the window the header names rather than burning the attempt."""
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq
    except Exception as e:
        _say(log, f"    [chat] groq sdk unavailable: {e}")
        return None

    client = Groq(api_key=api_key)
    kwargs = {"messages": [{"role": "user", "content": prompt}], "temperature": temperature}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    attempts = int(os.environ.get("GROQ_MAX_ATTEMPTS", "3"))
    for model in models:
        for attempt in range(1, attempts + 1):
            try:
                resp = client.chat.completions.create(model=model, **kwargs)
                joblog.ai("chat", "groq", model, ok=True)
                return resp.choices[0].message.content.strip()
            except Exception as e:
                status = getattr(e, "status_code", None)
                if status in _NON_RETRYABLE:
                    _say(log, f"    [chat] groq {model}: non-retryable {status}: {e}")
                    joblog.ai("chat", "groq", model, ok=False, detail=f"HTTP {status}")
                    break
                if status == 404:
                    _say(log, f"    [chat] groq model '{model}' not available on this key — skipping")
                    joblog.ai("chat", "groq", model, ok=False, detail="404 not on this key")
                    break
                if attempt >= attempts:
                    _say(log, f"    [chat] groq {model} gave up after {attempts} attempts: {e}")
                    joblog.ai("chat", "groq", model, ok=False,
                              detail=f"gave up after {attempts} tries"
                                     + (f" (HTTP {status})" if status else ""))
                    break
                wait = _retry_after_seconds(e, 2.0 * attempt)
                kind = "rate-limited" if status == 429 else "failed"
                _say(log, f"    [chat] groq {model} attempt {attempt} {kind} — waiting {wait:.0f}s")
                time.sleep(wait)
    return None


# Gemini models tried in order, first one that answers wins. Google retires model
# ids on a rolling basis (gemini-2.0-flash and gemini-2.5-flash are both 404 to new
# keys now), which is exactly why this is a LIST and not a constant: a retired
# default used to take the whole provider down silently. The `-latest` alias sits in
# the middle as the always-valid safety net.
DEFAULT_GEMINI_MODELS = ["gemini-3.7-flash", "gemini-flash-latest", "gemini-3.5-flash"]

_GEMINI_ENDPOINT = ("https://generativelanguage.googleapis.com/v1beta/models"
                    "/{model}:generateContent")

# Model ids that 404'd this process — never retried, so a long job pays the dead-model
# cost once instead of once per transcript chunk.
_GEMINI_DEAD = set()


def _gemini_models(prefer=None):
    """Preferred model first, then the configured one, then the built-in ladder.

    `prefer` is how the UI's "Gemini Pro" choice reaches the wire. The rest of the
    ladder stays behind it, so picking Pro and hitting its tighter quota falls back
    to Flash instead of failing the job."""
    chosen = (os.environ.get("GEMINI_MODEL") or "").strip()
    ordered = ([prefer] if prefer else []) + ([chosen] if chosen else []) + DEFAULT_GEMINI_MODELS
    seen, out = set(), []
    for m in ordered:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _gemini_extract(payload):
    """Pull the answer text out of a generateContent response.

    Gemini's flash models think before they answer, and those reasoning parts come
    back in the SAME parts array flagged `thought: true`. Concatenating everything
    blindly prepends the model's scratchpad to the JSON and breaks json.loads, so
    thought parts are dropped here."""
    candidates = payload.get("candidates") or []
    if not candidates:
        return "", (payload.get("promptFeedback") or {}).get("blockReason") or "no candidates"
    cand = candidates[0]
    parts = (cand.get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts
                   if isinstance(p, dict) and not p.get("thought"))
    return text.strip(), cand.get("finishReason") or ""


def _gemini_chat(prompt, temperature, json_mode, log, prefer_model=None):
    """Google Gemini over plain REST. Returns text or None.

    Deliberately has NO SDK dependency. The google-generativeai package was an
    install-time trap: when it was missing (which it was), a perfectly valid
    GEMINI_API_KEY still reported the provider as unavailable and the UI greyed the
    Gemini card out with "key not set". urllib is in the standard library, so a key
    on its own is now genuinely sufficient. This also accepts both the legacy
    'AIza…' keys and the current 'AQ.…' AI Studio format, since the wire protocol is
    the same for both."""
    api_key = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    if not api_key:
        return None
    import urllib.request
    import urllib.error

    cfg = {"temperature": temperature,
           "maxOutputTokens": int(os.environ.get("GEMINI_MAX_TOKENS", "8192"))}
    if json_mode:
        cfg["responseMimeType"] = "application/json"
    data = json.dumps({"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                       "generationConfig": cfg}).encode("utf-8")
    timeout = int(os.environ.get("GEMINI_TIMEOUT", "240"))

    for model in _gemini_models(prefer_model):
        if model in _GEMINI_DEAD:
            continue
        for attempt in range(1, 3):
            try:
                req = urllib.request.Request(
                    _GEMINI_ENDPOINT.format(model=model), data=data,
                    headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                text, finish = _gemini_extract(payload)
                if text:
                    joblog.ai("chat", "gemini", model, ok=True)
                    return text
                _say(log, f"    [chat] gemini {model} returned no text (finish={finish})")
                joblog.ai("chat", "gemini", model, ok=False, detail=f"empty (finish={finish})")
                break
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", "replace")[:200]
                except Exception:
                    pass
                if e.code == 404:
                    # Retired/unknown model id — move down the ladder, permanently.
                    _GEMINI_DEAD.add(model)
                    _say(log, f"    [chat] gemini model '{model}' unavailable (404) — trying next")
                    joblog.ai("chat", "gemini", model, ok=False, detail="404 retired model")
                    break
                if e.code in _NON_RETRYABLE:
                    _say(log, f"    [chat] gemini: non-retryable {e.code}: {body}")
                    joblog.ai("chat", "gemini", model, ok=False, detail=f"HTTP {e.code}")
                    return None
                wait = _retry_after_seconds(e, 2.0 * attempt)
                _say(log, f"    [chat] gemini {model} attempt {attempt} failed "
                          f"(HTTP {e.code}) — waiting {wait:.0f}s")
                if attempt >= 2:
                    joblog.ai("chat", "gemini", model, ok=False,
                              detail=f"HTTP {e.code}" + (" rate limited" if e.code == 429 else ""))
                time.sleep(wait)
            except Exception as e:
                _say(log, f"    [chat] gemini {model} attempt {attempt} failed: {e}")
                time.sleep(2 * attempt)
    return None


def _openrouter_chat(prompt, temperature, json_mode, log):
    """OpenRouter fallback — one key fronts dozens of models, including free ones.
    Uses plain HTTP so no extra SDK is needed. Returns text or None."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return None
    import urllib.request
    import urllib.error

    model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode("utf-8")

    for attempt in range(1, 3):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=data,
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                parsed = json.loads(resp.read().decode("utf-8"))
            return (parsed["choices"][0]["message"]["content"] or "").strip()
        except urllib.error.HTTPError as e:
            if e.code in _NON_RETRYABLE:
                _say(log, f"    [chat] openrouter: non-retryable {e.code}")
                return None
            _say(log, f"    [chat] openrouter attempt {attempt} failed: HTTP {e.code}")
            time.sleep(2 * attempt)
        except Exception as e:
            _say(log, f"    [chat] openrouter attempt {attempt} failed: {e}")
            time.sleep(2 * attempt)
    return None


DEFAULT_CHAT_ORDER = "gemini,groq,openrouter"

# The default picker is Gemini, not Groq. Selection is the one task in the pipeline
# that has to read a WHOLE transcript, and the two providers are not comparable
# there: Gemini Flash takes a 19-minute Hindi transcript (~12k tokens) in a single
# call, while Groq's free tier allows 12k tokens PER MINUTE across all calls — so the
# same transcript has to be split into a dozen chunks that then throttle each other.
DEFAULT_SELECTION_MODEL = "gemini"


# Models the UI can offer for the selection pass, in the order they are shown.
# key -> (provider, concrete model or None, label, one-line note)
SELECTION_MODELS = [
    ("gemini",            ("gemini", None, "Gemini Flash",
                           "Google. Reads the whole transcript in one pass — the default, "
                           "and the best pick for long videos.")),
    ("gemini-pro",        ("gemini", "gemini-pro-latest", "Gemini Pro",
                           "Google's reasoning model. Slower and on a tighter free quota, "
                           "but the best judge of where a thought actually ends — worth it "
                           "for interviews and podcasts.")),
    ("auto",              (None, None, "Auto",
                           "Try each provider in turn. Never fails while one key works.")),
    ("groq:llama-3.3-70b-versatile", ("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B",
                           "Groq. Strong judgement, but a small free-tier budget: long "
                           "videos get split up and can hit rate limits.")),
    ("groq:llama-3.1-8b-instant",    ("groq", "llama-3.1-8b-instant", "Llama 3.1 8B",
                           "Groq. Much faster and cheaper, rougher picks.")),
    ("openrouter",        ("openrouter", None, "OpenRouter",
                           "Whatever OPENROUTER_MODEL points at.")),
]


def selection_model_catalogue():
    """The model list for the UI, each flagged with whether its key is actually set."""
    st = provider_status().get("chat", {})
    out = []
    for key, (prov, model, label, note) in SELECTION_MODELS:
        out.append({
            "key": key, "label": label, "help": note,
            "provider": prov or "auto",
            # Auto always has something to try as long as any one key is set.
            "available": bool(st.get(prov)) if prov else any(st.values()),
            "default": key == DEFAULT_SELECTION_MODEL,
        })
    return out


def chat(prompt, temperature=0.2, json_mode=False, log=None, groq_models=None,
         prefer_model=None):
    """Run a single-prompt completion through the whole provider chain.

    Order is configurable via env CHAT_ORDER (comma list of
    gemini,groq,openrouter). Returns the raw response text (caller parses
    it), or "" if every provider failed. `json_mode=True` asks for strict JSON.

    `prefer_model` is a key from SELECTION_MODELS. It moves that provider to the
    front (and pins its model, for Groq) WITHOUT dropping the rest of the chain —
    a chosen model that is rate-limited still falls back rather than failing the
    job, which is the whole reason this function exists.
    """
    models = groq_models or DEFAULT_GROQ_MODELS
    forced = dict(SELECTION_MODELS).get((prefer_model or "auto").strip())
    forced_provider = forced[0] if forced else None
    if forced and forced[1]:
        models = [forced[1]] + [m for m in models if m != forced[1]]
    engines = {
        "gemini": lambda: _gemini_chat(prompt, temperature, json_mode, log,
                                       forced[1] if forced_provider == "gemini" else None),
        "groq": lambda: _groq_chat(prompt, temperature, json_mode, models, log),
        "openrouter": lambda: _openrouter_chat(prompt, temperature, json_mode, log),
    }
    order_str = os.environ.get("CHAT_ORDER", DEFAULT_CHAT_ORDER)
    order = [o.strip().lower() for o in order_str.split(",") if o.strip()]
    if forced_provider:
        order = [forced_provider] + [o for o in order if o != forced_provider]
        _say(log, f"    [chat] preferred model: {prefer_model}")

    for i, name in enumerate(order):
        fn = engines.get(name)
        if not fn:
            continue
        out = fn()
        if out:
            if i > 0:
                _say(log, f"    [chat] recovered via fallback provider '{name}'")
            return out
        if i + 1 < len(order):
            _say(log, f"    [chat] '{name}' unavailable -> trying '{order[i + 1]}'")
    _say(log, "    [chat] ALL chat providers failed (returning empty)")
    joblog.ai("chat", "ALL PROVIDERS", "", ok=False, detail="every provider failed")
    return ""


# How much transcript one selection call may carry, per provider, in CHARACTERS.
#
# These are budgets for the FREE tiers, and the spread between them is the whole
# reason long videos used to fail. Hindi tokenises at roughly 0.5 tokens/char on
# Llama's tokenizer, so Groq's 12k tokens-per-minute ceiling is only ~22k characters
# of Hindi per MINUTE — shared by every call. Gemini Flash has a million-token
# context and a far larger per-minute allowance, so it swallows a feature-length
# transcript whole and never needs splitting at all.
CHAT_CHUNK_CHARS = {
    "gemini":     240000,
    "groq":         9000,
    "openrouter":  24000,
}
DEFAULT_CHUNK_CHARS = 9000


def chunk_chars_for(prefer_model=None):
    """Characters of transcript to put in one selection call for this model choice.

    SELECTION_CHUNK_CHARS overrides everything when set, so a paid tier can be dialled
    in without touching code. "auto" is costed as the FIRST provider in the chain that
    actually has a key — that is the one that will really answer, and sizing chunks for
    a provider that never runs is how a long video ends up split 13 ways for nothing."""
    override = (os.environ.get("SELECTION_CHUNK_CHARS") or "").strip()
    if override:
        try:
            return max(1500, int(override))
        except ValueError:
            pass

    key = (prefer_model or "auto").strip()
    forced = dict(SELECTION_MODELS).get(key)
    provider = forced[0] if forced else None
    if not provider:
        st = provider_status().get("chat", {})
        order = [o.strip().lower() for o in
                 os.environ.get("CHAT_ORDER", DEFAULT_CHAT_ORDER).split(",") if o.strip()]
        provider = next((p for p in order if st.get(p)), None)
    return CHAT_CHUNK_CHARS.get(provider, DEFAULT_CHUNK_CHARS)


# ─────────────────────────────────────────────────────────────
# TRANSCRIPTION  (Groq Whisper  ->  Deepgram  ->  local faster-whisper)
# Every engine returns the SAME normalised shape:
#   {"segments": [{"start","end","text","words":[{"word","start","end"}]}]}
# ─────────────────────────────────────────────────────────────

def _groq_whisper_transcribe(audio_path, language, log):
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return None
    try:
        from groq import Groq
    except Exception:
        return None

    client = Groq(api_key=api_key)
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    models = ["whisper-large-v3", "whisper-large-v3-turbo"]
    for model in models:
        for attempt in range(1, 4):
            try:
                _say(log, f"  [transcribe] Groq Whisper {model} attempt {attempt}/3")
                tr = client.audio.transcriptions.create(
                    file=(os.path.basename(audio_path), audio_bytes),
                    model=model,
                    response_format="verbose_json",
                    language=language,
                    timestamp_granularities=["word", "segment"],
                )
                data = json.loads(tr.model_dump_json())
                segs = data.get("segments", [])
                top_words = data.get("words", [])
                # Attach root-level word timestamps to their segments if needed.
                if top_words and segs and "words" not in segs[0]:
                    for s in segs:
                        s["words"] = []
                    for w in top_words:
                        for s in segs:
                            if s["start"] <= w["start"] <= s["end"]:
                                s["words"].append(w)
                                break
                if segs:
                    joblog.ai("transcribe", "groq", model, ok=True, detail=f"{len(segs)} segments")
                    return {"segments": segs, "words": top_words, "_engine": f"groq:{model}"}
            except Exception as e:
                status = getattr(e, "status_code", None)
                if status in _NON_RETRYABLE:
                    _say(log, f"  [transcribe] Groq {model}: non-retryable {status}: {e}")
                    break
                wait = 2 ** attempt
                _say(log, f"  [transcribe] Groq {model} error (attempt {attempt}): {e} — retry in {wait}s")
                time.sleep(wait)
    return None


def _deepgram_transcribe(audio_path, language, log, diarize=False):
    """diarize=True asks Deepgram for speaker labels, which arrive on every word
    and utterance. That is what podcast mode cuts on (a clip should end when the
    guest finishes answering, not mid-way into the next question) and what gives
    the captions a colour per speaker."""
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        return None
    try:
        from deepgram import DeepgramClient
    except Exception:
        return None

    with open(audio_path, "rb") as f:
        buf = f.read()

    # nova-3 is the best model but occasionally 500s; nova-2 is the proven fallback.
    for model in ("nova-3", "nova-2"):
        for attempt in range(1, 3):
            try:
                _say(log, f"  [transcribe] Deepgram {model} attempt {attempt}/2")
                # api_key MUST be passed explicitly: deepgram-sdk 7.x dropped the
                # no-arg constructor's env lookup, which silently broke transcription.
                client = DeepgramClient(api_key=api_key)
                kw = {}
                if diarize:
                    kw["diarize"] = True
                response = client.listen.v1.media.transcribe_file(
                    request=buf, model=model, language=language,
                    smart_format=True, utterances=True, **kw,
                )
                if hasattr(response, "to_dict"):
                    data = response.to_dict()
                elif hasattr(response, "model_dump"):
                    data = response.model_dump()
                else:
                    data = json.loads(response.json())

                out = {"segments": []}
                for u in data.get("results", {}).get("utterances", []):
                    seg = {"start": u.get("start"), "end": u.get("end"),
                           "text": u.get("transcript"), "words": []}
                    # speaker is absent unless diarize was requested; keeping it
                    # None rather than 0 lets callers tell "one speaker" apart
                    # from "we never asked".
                    if u.get("speaker") is not None:
                        seg["speaker"] = u.get("speaker")
                    for w in u.get("words", []):
                        wd = {"word": w.get("punctuated_word", w.get("word")),
                              "start": w.get("start"), "end": w.get("end")}
                        if w.get("speaker") is not None:
                            wd["speaker"] = w.get("speaker")
                        seg["words"].append(wd)
                    out["segments"].append(seg)
                if out["segments"]:
                    out["_engine"] = f"deepgram:{model}"
                    joblog.ai("transcribe", "deepgram", model, ok=True,
                              detail=f"{len(out['segments'])} segments"
                                     + (" (diarised)" if diarize else ""))
                    return out
                _say(log, f"  [transcribe] Deepgram {model} returned no utterances")
                joblog.ai("transcribe", "deepgram", model, ok=False, detail="no utterances")
                break
            except Exception as e:
                status = getattr(e, "status_code", None)
                if status in _NON_RETRYABLE:
                    _say(log, f"  [transcribe] Deepgram {model}: non-retryable {status}: {e}")
                    joblog.ai("transcribe", "deepgram", model, ok=False, detail=f"HTTP {status}")
                    return None
                _say(log, f"  [transcribe] Deepgram {model} attempt {attempt} failed: {e}")
                time.sleep(2 * attempt)
    return None


def _local_whisper_transcribe(audio_path, language, log):
    """Offline fallback via faster-whisper. Never rate-limits, never 500s — this is
    the bulletproof last resort, ideal on a GPU box (RTX 3050) with CUDA.

    Controlled by env:
      LOCAL_WHISPER_MODEL  (default 'small'; use 'medium'/'large-v3' on a 3050)
      LOCAL_WHISPER_DEVICE (default 'auto' -> cuda if available else cpu)
    """
    try:
        from faster_whisper import WhisperModel
    except Exception:
        _say(log, "  [transcribe] faster-whisper not installed — offline fallback unavailable")
        return None

    model_name = os.environ.get("LOCAL_WHISPER_MODEL", "small")
    device = os.environ.get("LOCAL_WHISPER_DEVICE", "auto")
    compute = os.environ.get("LOCAL_WHISPER_COMPUTE", "")
    try:
        if device == "auto":
            try:
                import torch  # noqa
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        if not compute:
            compute = "float16" if device == "cuda" else "int8"
        _say(log, f"  [transcribe] local faster-whisper model={model_name} device={device} compute={compute}")
        model = WhisperModel(model_name, device=device, compute_type=compute)
        segments_iter, _info = model.transcribe(
            audio_path, language=language, word_timestamps=True,
        )
        out = {"segments": []}
        for seg in segments_iter:
            words = []
            for w in (seg.words or []):
                words.append({"word": w.word.strip(), "start": w.start, "end": w.end})
            out["segments"].append({"start": seg.start, "end": seg.end,
                                    "text": seg.text.strip(), "words": words})
        if out["segments"]:
            out["_engine"] = f"local:{model_name}"
            joblog.ai("transcribe", "local-whisper", model_name, ok=True,
                      detail=f"{len(out['segments'])} segments")
            return out
    except Exception as e:
        _say(log, f"  [transcribe] local whisper failed: {e}")
    return None


def transcribe_audio(audio_path, language="hi", log=None, prefer=None, diarize=False):
    """Word-level transcription with full provider fallback.

    Order is configurable via env TRANSCRIBE_ORDER (comma list of
    deepgram,groq,local) or the `prefer` arg. Returns the normalised dict
    {"segments":[...]} of the first engine that succeeds, else None.

    Deepgram leads because this transcript does double duty: it is what the clip
    selector reads AND what the Hindi captions are timed from. nova-3 is markedly
    better than Whisper on Hindi and on code-switched Hindi/English speech, so
    putting it first improves the picks and the subtitles at the same time — and it
    matches what the per-clip caption pass (CLIP_TRANSCRIBE_ORDER) already did.
    """
    order_str = prefer or os.environ.get("TRANSCRIBE_ORDER", "deepgram,groq,local")
    order = [o.strip().lower() for o in order_str.split(",") if o.strip()]
    engines = {
        "groq": _groq_whisper_transcribe,
        "deepgram": _deepgram_transcribe,
        "local": _local_whisper_transcribe,
    }
    for name in order:
        fn = engines.get(name)
        if not fn:
            continue
        # Only Deepgram can label speakers. Podcast mode needs that, so when it is
        # asked for, engines that cannot provide it are skipped rather than silently
        # returning a transcript with no speakers (which would make podcast mode
        # look broken instead of unavailable).
        if diarize:
            if name != "deepgram":
                _say(log, f"  [transcribe] '{name}' cannot label speakers — skipped "
                          f"(podcast mode needs Deepgram)")
                joblog.ai("transcribe", name, "", ok=False, detail="skipped: cannot diarise")
                continue
            data = fn(audio_path, language, log, diarize=True)
        else:
            data = fn(audio_path, language, log)
        if data and data.get("segments"):
            _say(log, f"  [transcribe] SUCCESS via {data.get('_engine', name)} "
                      f"({len(data['segments'])} segments)")
            return data
    _say(log, "  [transcribe] ALL transcription providers failed")
    joblog.ai("transcribe", "ALL ENGINES", "", ok=False, detail="every engine failed")
    return None


# ─────────────────────────────────────────────────────────────
# SUBPROCESS with timeout + retry
# ─────────────────────────────────────────────────────────────

def run_cmd(cmd, timeout=None, retries=1, backoff=3, log=None, label=""):
    """Run an external command with a hard timeout and optional retries.

    A hung ffmpeg/yt-dlp would otherwise block a worker thread forever, so every
    external call in the pipeline goes through here. Returns the final
    CompletedProcess (returncode != 0 on failure); a timeout is reported as
    returncode 124 rather than raising, so callers handle one failure shape.
    """
    label = label or (cmd[0] if cmd else "cmd")
    last = None
    for attempt in range(1, max(1, retries) + 1):
        try:
            last = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if last.returncode == 0:
                return last
            _say(log, f"    [{label}] attempt {attempt}/{retries} failed "
                      f"(rc={last.returncode}): {(last.stderr or '').strip()[-200:]}")
        except subprocess.TimeoutExpired:
            _say(log, f"    [{label}] attempt {attempt}/{retries} TIMED OUT after {timeout}s")
            last = subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")
        except FileNotFoundError as e:
            # Missing binary — retrying cannot help.
            return subprocess.CompletedProcess(cmd, 127, "", str(e))
        except Exception as e:
            _say(log, f"    [{label}] attempt {attempt}/{retries} errored: {e}")
            last = subprocess.CompletedProcess(cmd, 1, "", str(e))
        if attempt < retries:
            time.sleep(backoff * attempt)
    return last


def run_cmd_streaming(cmd, timeout=None, log=None, label="", on_line=None):
    """Like run_cmd, but hands every stdout line to `on_line` as it arrives.

    run_cmd uses capture_output, which buffers until the process exits — fine for
    ffprobe, useless for a ten-minute download the user is watching. This keeps the
    same CompletedProcess return shape (rc 124 on timeout) so callers do not need a
    second failure path. stderr is folded into stdout because yt-dlp splits progress
    and warnings across both and the caller only wants one stream to scan.
    """
    label = label or (cmd[0] if cmd else "cmd")
    buf = []
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(cmd, 127, "", str(e))
    except Exception as e:
        return subprocess.CompletedProcess(cmd, 1, "", str(e))

    deadline = (time.time() + timeout) if timeout else None
    try:
        for line in proc.stdout:
            buf.append(line)
            if on_line:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:
                    pass          # a broken progress callback must not kill the download
            if deadline and time.time() > deadline:
                proc.kill()
                _say(log, f"    [{label}] TIMED OUT after {timeout}s")
                return subprocess.CompletedProcess(cmd, 124, "".join(buf),
                                                   f"timed out after {timeout}s")
        proc.wait(timeout=30)
    except Exception as e:
        try: proc.kill()
        except Exception: pass
        return subprocess.CompletedProcess(cmd, 1, "".join(buf), str(e))

    out = "".join(buf)
    return subprocess.CompletedProcess(cmd, proc.returncode or 0, out,
                                       "" if proc.returncode == 0 else out[-2000:])


def have_binary(name):
    return shutil.which(name) is not None


# ─────────────────────────────────────────────────────────────
# PROVIDER STATUS  (drives the setup screen / provider badges)
# ─────────────────────────────────────────────────────────────

def provider_status():
    """Report which capabilities are actually usable right now.

    Purely local inspection (keys + installed packages + binaries) — no network
    calls, so it is cheap enough for the UI to poll.
    """
    def _key(*names):
        return any((os.environ.get(n) or "").strip() for n in names)

    def _mod(name):
        try:
            __import__(name)
            return True
        except Exception:
            return False

    chat_providers = {
        # Gemini is reached over plain REST, so the key is the ONLY requirement —
        # there is no SDK left to be missing and no second way for this to read
        # False while a working key sits in .env.
        "gemini": _key("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "groq": _key("GROQ_API_KEY"),
        "openrouter": _key("OPENROUTER_API_KEY"),
    }
    transcribe_providers = {
        "groq": _key("GROQ_API_KEY"),
        "deepgram": _key("DEEPGRAM_API_KEY") and _mod("deepgram"),
        "local": _mod("faster_whisper"),
    }
    return {
        "chat": chat_providers,
        "transcribe": transcribe_providers,
        "chat_ready": any(chat_providers.values()),
        "transcribe_ready": any(transcribe_providers.values()),
        "ffmpeg": have_binary("ffmpeg") and have_binary("ffprobe"),
        "ytdlp": have_binary("yt-dlp"),
    }


# ─────────────────────────────────────────────────────────────
# VIDEO ENCODER SELECTION  (NVENC -> AMF -> QSV -> libx264)
# ─────────────────────────────────────────────────────────────

_ENCODERS_BLOB = None
_ENCODER_OK = {}   # name -> bool, cached result of a real test-encode

# Quality knob shared across encoders (lower = better quality / bigger file).
_CQ = os.environ.get("ENCODE_CQ", "23")

# Each entry: name -> (ffmpeg args, encoder token to look for in `-encoders`)
_ENCODER_TABLE = {
    "nvenc":   (["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", _CQ, "-b:v", "0"], "h264_nvenc"),
    "amf":     (["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp",
                 "-qp_i", _CQ, "-qp_p", _CQ, "-qp_b", _CQ], "h264_amf"),
    "qsv":     (["-c:v", "h264_qsv", "-preset", "faster", "-global_quality", _CQ], "h264_qsv"),
    "cpu":     (["-c:v", "libx264", "-preset", "veryfast", "-crf", _CQ, "-threads", "2"], "libx264"),
}
CPU_ENCODER_ARGS = _ENCODER_TABLE["cpu"][0]

# Auto-detect preference order: GPU encoders first, CPU last (always works).
_AUTO_ORDER = ["nvenc", "amf", "qsv", "cpu"]


def _encoders_blob():
    global _ENCODERS_BLOB
    if _ENCODERS_BLOB is None:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                               capture_output=True, text=True)
            _ENCODERS_BLOB = (r.stdout or "") + (r.stderr or "")
        except Exception:
            _ENCODERS_BLOB = ""
    return _ENCODERS_BLOB


def _encoder_works(name, log=None):
    """Truly verify an encoder by doing a 1-frame test-encode.

    `ffmpeg -encoders` only lists what the build was COMPILED with — e.g. a full
    build lists h264_nvenc even on an AMD box with no NVIDIA GPU. The only reliable
    check is to actually run the encoder once. Result is cached per process.
    """
    if name in _ENCODER_OK:
        return _ENCODER_OK[name]
    if name == "cpu":
        _ENCODER_OK[name] = True
        return True
    # Fast reject if the encoder isn't even compiled in.
    if _ENCODER_TABLE[name][1] not in _encoders_blob():
        _ENCODER_OK[name] = False
        return False
    # 256x144 @ 10fps / 2 frames: small enough to be instant, but above the minimum
    # frame size some hardware encoders (notably AMF) require to initialise.
    cmd = (["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=256x144:d=0.2:r=10", "-frames:v", "2"]
           + _ENCODER_TABLE[name][0] + ["-pix_fmt", "yuv420p", "-f", "null", "-"])
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        ok = (r.returncode == 0)
        if not ok:
            _say(log, f"   Encoder probe '{name}' unusable on this machine "
                      f"({(r.stderr or '').strip()[-160:]})")
    except Exception as e:
        ok = False
        _say(log, f"   Encoder probe '{name}' errored: {e}")
    _ENCODER_OK[name] = ok
    return ok


def select_video_encoder(log=None, prefer=None):
    """Return (ffmpeg_args, name) for the fastest WORKING H.264 encoder.

    Override with env FFMPEG_ENCODER = auto | nvenc | amf | qsv | cpu (default auto).
    Each GPU candidate is verified with a real test-encode (cached), so on the RTX
    3050 box this resolves to nvenc, and on a box with no usable GPU it cleanly
    falls through to the CPU encoder — which is the guaranteed final fallback.
    """
    choice = (prefer or os.environ.get("FFMPEG_ENCODER", "auto")).strip().lower()

    if choice in _ENCODER_TABLE and choice != "auto":
        if _encoder_works(choice, log):
            _say(log, f"   Video encoder: {choice} (forced)")
            return _ENCODER_TABLE[choice][0], choice
        _say(log, f"   Video encoder: requested '{choice}' not usable -> auto-detecting")

    for name in _AUTO_ORDER:
        if _encoder_works(name, log):
            _say(log, f"   Video encoder: {name} (auto)")
            return _ENCODER_TABLE[name][0], name

    return CPU_ENCODER_ARGS, "cpu"
