"""
providers.py — provider-agnostic, fault-tolerant building blocks shared by
select_clips.py and burn_subtitles.py.

The goal is ROBUSTNESS: a single provider hiccup (Groq/Deepgram 500s, rate
limits, network blips) must never sink a job. Every capability here is a
*chain* of independent providers that are tried in order until one succeeds.

Capabilities
------------
  chat()                  Text/JSON completion. Chain: Groq -> Gemini ->
                          OpenRouter -> local Ollama. Order: CHAT_ORDER env.
  transcribe_audio()      Word-level transcription. Chain: Groq Whisper ->
                          Deepgram nova-3 -> local faster-whisper (offline).
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
# CHAT  (Groq  ->  Google Gemini)
# ─────────────────────────────────────────────────────────────

DEFAULT_GROQ_MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

# A non-retryable HTTP status means "trying again won't help" (auth/bad request).
_NON_RETRYABLE = (400, 401, 403, 404)


def _groq_chat(prompt, temperature, json_mode, models, log):
    """Try each Groq model with light retry/backoff. Returns text or None."""
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

    for model in models:
        for attempt in range(1, 3):
            try:
                resp = client.chat.completions.create(model=model, **kwargs)
                return resp.choices[0].message.content.strip()
            except Exception as e:
                status = getattr(e, "status_code", None)
                if status in _NON_RETRYABLE:
                    _say(log, f"    [chat] groq {model}: non-retryable {status}: {e}")
                    break
                _say(log, f"    [chat] groq {model} attempt {attempt} failed: {e}")
                time.sleep(2 * attempt)
    return None


def _gemini_chat(prompt, temperature, json_mode, log):
    """Google Gemini free-tier fallback. Returns text or None."""
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        return None
    try:
        import google.generativeai as genai
    except Exception:
        _say(log, "    [chat] gemini sdk not installed (pip install google-generativeai) — skipping")
        return None

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
    try:
        genai.configure(api_key=api_key)
        cfg = {"temperature": temperature}
        if json_mode:
            cfg["response_mime_type"] = "application/json"
        model = genai.GenerativeModel(model_name)
        for attempt in range(1, 3):
            try:
                resp = model.generate_content(prompt, generation_config=cfg)
                return (resp.text or "").strip()
            except Exception as e:
                _say(log, f"    [chat] gemini {model_name} attempt {attempt} failed: {e}")
                time.sleep(2 * attempt)
    except Exception as e:
        _say(log, f"    [chat] gemini setup failed: {e}")
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


def _ollama_chat(prompt, temperature, json_mode, log):
    """Local Ollama fallback — no API key, no network, no rate limits. This is the
    last resort that keeps text tasks alive when every hosted provider is down.
    Only attempted when a local Ollama server is actually reachable."""
    import urllib.request
    import urllib.error

    host = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1")
    body = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if json_mode:
        body["format"] = "json"
    try:
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            parsed = json.loads(resp.read().decode("utf-8"))
        return (parsed.get("response") or "").strip()
    except Exception as e:
        _say(log, f"    [chat] ollama unavailable at {host}: {e}")
        return None


DEFAULT_CHAT_ORDER = "groq,gemini,openrouter,ollama"


# Models the UI can offer for the selection pass, in the order they are shown.
# key -> (provider, concrete model or None, label, one-line note)
SELECTION_MODELS = [
    ("auto",              (None, None, "Auto",
                           "Try each provider in turn. Never fails while one key works.")),
    ("groq:llama-3.3-70b-versatile", ("groq", "llama-3.3-70b-versatile", "Llama 3.3 70B",
                           "Groq. The strongest judgement, and the default.")),
    ("groq:llama-3.1-8b-instant",    ("groq", "llama-3.1-8b-instant", "Llama 3.1 8B",
                           "Groq. Much faster and cheaper, rougher picks.")),
    ("gemini",            ("gemini", None, "Gemini Flash",
                           "Google. Good long-context reader; needs GEMINI_API_KEY.")),
    ("openrouter",        ("openrouter", None, "OpenRouter",
                           "Whatever OPENROUTER_MODEL points at.")),
    ("ollama",            ("ollama", None, "Local (Ollama)",
                           "Runs on this machine. No key, no network, no limits.")),
]


def selection_model_catalogue():
    """The model list for the UI, each flagged with whether its key is actually set."""
    st = provider_status().get("chat", {})
    out = []
    for key, (prov, model, label, note) in SELECTION_MODELS:
        out.append({
            "key": key, "label": label, "help": note,
            "provider": prov or "auto",
            # Ollama is probed lazily and Auto always has something to try, so
            # neither is ever reported as unavailable.
            "available": True if prov in (None, "ollama") else bool(st.get(prov)),
        })
    return out


def chat(prompt, temperature=0.2, json_mode=False, log=None, groq_models=None,
         prefer_model=None):
    """Run a single-prompt completion through the whole provider chain.

    Order is configurable via env CHAT_ORDER (comma list of
    groq,gemini,openrouter,ollama). Returns the raw response text (caller parses
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
        "groq": lambda: _groq_chat(prompt, temperature, json_mode, models, log),
        "gemini": lambda: _gemini_chat(prompt, temperature, json_mode, log),
        "openrouter": lambda: _openrouter_chat(prompt, temperature, json_mode, log),
        "ollama": lambda: _ollama_chat(prompt, temperature, json_mode, log),
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
    return ""


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


def _deepgram_transcribe(audio_path, language, log):
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
                response = client.listen.v1.media.transcribe_file(
                    request=buf, model=model, language=language,
                    smart_format=True, utterances=True,
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
                    for w in u.get("words", []):
                        seg["words"].append({"word": w.get("punctuated_word", w.get("word")),
                                             "start": w.get("start"), "end": w.get("end")})
                    out["segments"].append(seg)
                if out["segments"]:
                    out["_engine"] = f"deepgram:{model}"
                    return out
                _say(log, f"  [transcribe] Deepgram {model} returned no utterances")
                break
            except Exception as e:
                status = getattr(e, "status_code", None)
                if status in _NON_RETRYABLE:
                    _say(log, f"  [transcribe] Deepgram {model}: non-retryable {status}: {e}")
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
            return out
    except Exception as e:
        _say(log, f"  [transcribe] local whisper failed: {e}")
    return None


def transcribe_audio(audio_path, language="hi", log=None, prefer=None):
    """Word-level transcription with full provider fallback.

    Order is configurable via env TRANSCRIBE_ORDER (comma list of
    groq,deepgram,local) or the `prefer` arg. Returns the normalised dict
    {"segments":[...]} of the first engine that succeeds, else None.
    """
    order_str = prefer or os.environ.get("TRANSCRIBE_ORDER", "groq,deepgram,local")
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
        data = fn(audio_path, language, log)
        if data and data.get("segments"):
            _say(log, f"  [transcribe] SUCCESS via {data.get('_engine', name)} "
                      f"({len(data['segments'])} segments)")
            return data
    _say(log, "  [transcribe] ALL transcription providers failed")
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
        "groq": _key("GROQ_API_KEY"),
        "gemini": _key("GEMINI_API_KEY", "GOOGLE_API_KEY") and _mod("google.generativeai"),
        "openrouter": _key("OPENROUTER_API_KEY"),
        "ollama": False,   # probed lazily; treated as bonus, never required
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
