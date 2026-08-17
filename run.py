#!/usr/bin/env python3
"""
run.py — one command to set up AND start ShortsAI.

Detects the OS/distro and installs everything automatically:
  • Python deps via pip  (fastapi, uvicorn, groq, yt-dlp, …)
  • ffmpeg with libx264  via the system package manager
  • All 18 caption fonts  downloaded into ./fonts/

Usage:
    python3 run.py                   # first-time setup + start the server
    python3 run.py --reload          # dev mode: auto-restart on code changes
    python3 run.py --port 9000       # use a different port
    python3 run.py --host 0.0.0.0    # expose on your LAN / phone
    python3 run.py --reinstall       # force re-run pip install
    python3 run.py --install-ffmpeg  # auto-install/fix ffmpeg
    python3 run.py --skip-fonts      # skip font downloads (faster re-runs)
    python3 run.py --no-sudo         # skip every step that needs sudo
    python3 run.py --setup-only      # set up without starting the server
    python3 run.py --skip-test       # skip API key + subtitle engine tests

Supported platforms:
  Fedora  — dnf + RPM Fusion (adds libx264)
  Ubuntu / Debian / WSL — apt-get
  Arch / Manjaro — pacman
  openSUSE — zypper
  macOS — Homebrew
  Windows — winget / chocolatey / manual
"""

import os
import sys
import shutil
import argparse
import platform
import subprocess
import urllib.request
import urllib.error
from pathlib import Path

HERE      = Path(__file__).resolve().parent
VENV      = HERE / ".venv"
STAMP     = VENV / ".installed"
REQS      = HERE / "requirements.txt"
FONTS_DIR = HERE / "fonts"

# ── Font catalogue ─────────────────────────────────────────────────────────────
FONT_URLS = {
    # Hindi / Devanagari
    # THE critical font: without a Devanagari face, Hindi captions render as empty
    # boxes. The old notofonts/devanagari path 404s; this mirror is the live one.
    "NotoSansDevanagari-Bold.ttf":
        "https://github.com/notofonts/notofonts.github.io/raw/main/fonts/NotoSansDevanagari/hinted/ttf/NotoSansDevanagari-Bold.ttf",
    "Mukta-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/mukta/Mukta-Bold.ttf",
    "Hind-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/hind/Hind-Bold.ttf",
    "RozhaOne-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/rozhaone/RozhaOne-Regular.ttf",
    "Kalam-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/kalam/Kalam-Bold.ttf",
    # Google Fonts ships these three only as VARIABLE fonts — there is no
    # static -Bold.ttf in the repo, so the old static URLs 404'd and the app
    # offered three fonts it could never render. The [wght] file works fine:
    # libass synthesises the weight from the ASS style's Bold flag.
    "Baloo2-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/baloo2/Baloo2%5Bwght%5D.ttf",
    "Laila-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/laila/Laila-Bold.ttf",
    "Rajdhani-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/rajdhani/Rajdhani-Bold.ttf",
    "Khand-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/khand/Khand-Bold.ttf",
    "Teko-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/teko/Teko%5Bwght%5D.ttf",
    # English / Latin
    "Poppins-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/poppins/Poppins-Bold.ttf",
    "Anton-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/anton/Anton-Regular.ttf",
    "BebasNeue-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/bebasneue/BebasNeue-Regular.ttf",
    "ArchivoBlack-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/archivoblack/ArchivoBlack-Regular.ttf",
    "FjallaOne-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/fjallaone/FjallaOne-Regular.ttf",
    # Variable-only upstream, same as Baloo 2 above.
    "Oswald-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/oswald/Oswald%5Bwght%5D.ttf",
    "Montserrat-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/montserrat/Montserrat%5Bwght%5D.ttf",
    "Staatliches-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/staatliches/Staatliches-Regular.ttf",
    "BarlowCondensed-Bold.ttf":
        "https://github.com/google/fonts/raw/main/ofl/barlowcondensed/BarlowCondensed-Bold.ttf",
    "Righteous-Regular.ttf":
        "https://github.com/google/fonts/raw/main/ofl/righteous/Righteous-Regular.ttf",
}

# ── Colours ────────────────────────────────────────────────────────────────────
def c(text, code):
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"

def info(msg):  print(c("• ", "36") + msg)
def ok(msg):    print(c("✓ ", "32") + msg)
def warn(msg):  print(c("! ", "33") + msg)
def err(msg):   print(c("✗ ", "31") + msg, file=sys.stderr)
def hdr(msg):   print(c(f"\n── {msg} ", "1") + c("─" * max(0, 50 - len(msg)), "90"))

# ── Shell helpers ──────────────────────────────────────────────────────────────
def run_cmd(cmd, check=True, **kw):
    print("  " + c("$ " + " ".join(str(x) for x in cmd), "90"))
    return subprocess.run(cmd, check=check, **kw)

def have(cmd):
    return shutil.which(cmd) is not None

def venv_python():
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

def venv_bin(name):
    suffix = ".exe" if os.name == "nt" else ""
    scripts = "Scripts" if os.name == "nt" else "bin"
    return VENV / scripts / (name + suffix)

# ── OS / distro detection ──────────────────────────────────────────────────────
def _read_os_release():
    for path in ("/etc/os-release", "/usr/lib/os-release"):
        try:
            return Path(path).read_text().lower()
        except Exception:
            pass
    return ""

def _detect_distro():
    text = _read_os_release()
    if "fedora" in text:
        return "fedora"
    if "arch" in text or "manjaro" in text or "endeavour" in text or "garuda" in text:
        return "arch"
    if "ubuntu" in text or "debian" in text or "mint" in text or "pop!" in text:
        return "debian"
    if "opensuse" in text or "suse" in text:
        return "opensuse"
    if "rhel" in text or "centos" in text or "rocky" in text or "almalinux" in text:
        return "rhel"
    return "linux"

def detect_os():
    """Return (os_type, distro). distro is only meaningful on Linux/WSL."""
    if os.name == "nt":
        return "windows", ""
    if platform.system().lower() == "darwin":
        return "mac", ""
    try:
        if "microsoft" in Path("/proc/version").read_text().lower():
            return "wsl", _detect_distro()
    except Exception:
        pass
    return "linux", _detect_distro()

# ── Python version ─────────────────────────────────────────────────────────────
def check_python():
    vi = sys.version_info
    if vi < (3, 10):
        warn(f"Python {vi.major}.{vi.minor} detected — 3.10+ is recommended")
    else:
        ok(f"Python {vi.major}.{vi.minor}.{vi.micro}")

# ── Virtual environment ────────────────────────────────────────────────────────
def ensure_venv():
    if venv_python().exists():
        ok("Virtual environment ready  (.venv)")
        return False
    info("Creating virtual environment in .venv …")
    import venv as _venv
    _venv.EnvBuilder(with_pip=True).create(VENV)
    ok("Virtual environment created")
    return True

# ── Python dependencies ────────────────────────────────────────────────────────
def install_deps(force=False):
    if not REQS.exists():
        err(f"requirements.txt not found at {REQS}")
        sys.exit(1)
    if STAMP.exists() and not force:
        ok("Python dependencies already installed  (--reinstall to refresh)")
        return
    vpy = str(venv_python())
    info("Installing Python dependencies (first run may take a minute) …")
    try:
        run_cmd([vpy, "-m", "pip", "install", "--upgrade", "pip", "--quiet"])
        run_cmd([vpy, "-m", "pip", "install", "-r", str(REQS)])
    except subprocess.CalledProcessError:
        err("pip install failed — check your internet connection and try again.")
        sys.exit(1)
    STAMP.write_text("ok\n")
    ok("Python dependencies installed")

def check_ytdlp():
    if venv_bin("yt-dlp").exists():
        ok("yt-dlp ready  (venv)")
    elif have("yt-dlp"):
        ok("yt-dlp ready  (system)")
    else:
        warn("yt-dlp not found in venv — YouTube downloads may fail (run after pip install)")

# ── ffmpeg ─────────────────────────────────────────────────────────────────────
# H.264 encoders the render pipeline can use, fastest (GPU) first. The pipeline
# auto-detects the working one at render time (see providers.select_video_encoder),
# so the toolchain is "ok" if ANY of these is present — NOT only libx264. A box that
# renders with h264_amf / h264_nvenc must not be told to "reinstall ffmpeg".
_H264_ENCODERS = ("h264_nvenc", "h264_amf", "h264_qsv", "libx264")

def _ffmpeg_encoders_blob():
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True, text=True
        )
        return result.stdout + result.stderr
    except Exception:
        return ""

def _has_libx264():
    return "libx264" in _ffmpeg_encoders_blob()

def _available_h264_encoders():
    blob = _ffmpeg_encoders_blob()
    return [e for e in _H264_ENCODERS if e in blob]

def _ffmpeg_ok():
    return have("ffmpeg") and have("ffprobe") and bool(_available_h264_encoders())

def _rpm_fusion_installed():
    try:
        return subprocess.run(
            ["rpm", "-q", "rpmfusion-free-release"],
            capture_output=True
        ).returncode == 0
    except Exception:
        return False

def _ffmpeg_free_installed():
    try:
        return subprocess.run(
            ["rpm", "-q", "ffmpeg-free"],
            capture_output=True
        ).returncode == 0
    except Exception:
        return False

def _install_ffmpeg_fedora(sudo):
    if not sudo:
        warn("Need sudo to install ffmpeg on Fedora. Install manually:")
        warn("  sudo dnf install -y https://mirrors.rpmfusion.org/free/fedora/"
             "rpmfusion-free-release-$(rpm -E %fedora).noarch.rpm")
        warn("  sudo dnf swap -y ffmpeg-free ffmpeg --allowerasing")
        return False
    try:
        if not _rpm_fusion_installed():
            info("  Enabling RPM Fusion free repository …")
            fedora_ver = subprocess.check_output(
                ["rpm", "-E", "%fedora"], text=True
            ).strip()
            url = (f"https://mirrors.rpmfusion.org/free/fedora/"
                   f"rpmfusion-free-release-{fedora_ver}.noarch.rpm")
            run_cmd(["sudo", "dnf", "install", "-y", url])
        else:
            ok("  RPM Fusion free already enabled")

        if _ffmpeg_free_installed():
            info("  Swapping ffmpeg-free → ffmpeg (this adds libx264) …")
            run_cmd(["sudo", "dnf", "swap", "-y", "ffmpeg-free", "ffmpeg",
                     "--allowerasing"])
        else:
            info("  Installing ffmpeg from RPM Fusion …")
            run_cmd(["sudo", "dnf", "install", "-y", "ffmpeg"])
        return True
    except subprocess.CalledProcessError as e:
        err(f"  Fedora ffmpeg install failed: {e}")
        warn("  Try manually: sudo dnf swap -y ffmpeg-free ffmpeg --allowerasing")
        return False

def _install_ffmpeg_debian(sudo):
    if not sudo:
        warn("Need sudo. Run: sudo apt-get install -y ffmpeg")
        return False
    try:
        run_cmd(["sudo", "apt-get", "update", "-qq"])
        run_cmd(["sudo", "apt-get", "install", "-y", "ffmpeg"])
        return True
    except subprocess.CalledProcessError as e:
        err(f"  apt-get install ffmpeg failed: {e}")
        return False

def _install_ffmpeg_arch(sudo):
    if not sudo:
        warn("Need sudo. Run: sudo pacman -S --noconfirm ffmpeg")
        return False
    try:
        run_cmd(["sudo", "pacman", "-S", "--noconfirm", "ffmpeg"])
        return True
    except subprocess.CalledProcessError as e:
        err(f"  pacman install ffmpeg failed: {e}")
        return False

def _install_ffmpeg_opensuse(sudo):
    if not sudo:
        warn("Need sudo. Run: sudo zypper install -y ffmpeg")
        return False
    try:
        run_cmd(["sudo", "zypper", "--non-interactive", "install", "ffmpeg"])
        return True
    except subprocess.CalledProcessError as e:
        err(f"  zypper install ffmpeg failed: {e}")
        return False

def _install_ffmpeg_mac():
    if not have("brew"):
        warn("Homebrew not found — install from https://brew.sh/ then: brew install ffmpeg")
        return False
    try:
        run_cmd(["brew", "install", "ffmpeg"])
        return True
    except subprocess.CalledProcessError as e:
        err(f"  brew install ffmpeg failed: {e}")
        return False

def _install_ffmpeg_windows():
    for mgr, cmd in [
        ("winget",  ["winget", "install", "--id", "Gyan.FFmpeg", "-e",
                     "--source", "winget", "--silent"]),
        ("choco",   ["choco", "install", "ffmpeg", "-y"]),
        ("scoop",   ["scoop", "install", "ffmpeg"]),
    ]:
        if have(mgr):
            try:
                run_cmd(cmd)
                return True
            except subprocess.CalledProcessError:
                warn(f"  {mgr} install failed, trying next …")
    warn("Could not auto-install ffmpeg on Windows.")
    warn("Download manually from: https://www.gyan.dev/ffmpeg/builds/")
    warn("  → ffmpeg-release-essentials.zip → extract → add bin/ to PATH")
    return False

def ensure_ffmpeg(os_type, distro, try_install, sudo):
    if _ffmpeg_ok():
        ok("ffmpeg + libx264 ready")
        return

    if have("ffmpeg") and not _has_libx264():
        warn("ffmpeg found but libx264 is MISSING — subtitle encoding will fail")
        if not try_install:
            if os_type in ("linux", "wsl") and distro == "fedora":
                warn("  Fix: python3 run.py --install-ffmpeg")
                warn("    or: sudo dnf swap -y ffmpeg-free ffmpeg --allowerasing")
            else:
                warn("  Fix: python3 run.py --install-ffmpeg")
            return
    elif not have("ffmpeg"):
        warn("ffmpeg not found — REQUIRED for video processing")
        if not try_install:
            warn("  Fix: python3 run.py --install-ffmpeg")
            return

    info("Installing / upgrading ffmpeg …")
    installed = False
    if os_type in ("linux", "wsl"):
        if distro == "fedora":
            installed = _install_ffmpeg_fedora(sudo)
        elif distro == "debian":
            installed = _install_ffmpeg_debian(sudo)
        elif distro == "arch":
            installed = _install_ffmpeg_arch(sudo)
        elif distro == "opensuse":
            installed = _install_ffmpeg_opensuse(sudo)
        elif distro == "rhel":
            installed = _install_ffmpeg_fedora(sudo)  # dnf-based
        else:
            installed = _install_ffmpeg_debian(sudo)   # best guess
    elif os_type == "mac":
        installed = _install_ffmpeg_mac()
    elif os_type == "windows":
        installed = _install_ffmpeg_windows()

    if installed:
        if _ffmpeg_ok():
            ok("ffmpeg + libx264 confirmed working")
        elif have("ffmpeg"):
            warn("ffmpeg installed but libx264 check failed — encoding may still work")
        else:
            warn("ffmpeg install reported success but binary not found — restart shell?")
    # else: distro-specific function already printed the warning

# ── Fonts ──────────────────────────────────────────────────────────────────────
def ensure_fonts(skip):
    if skip:
        ok("Skipping font downloads  (--skip-fonts)")
        return

    FONTS_DIR.mkdir(exist_ok=True)
    missing = [n for n in FONT_URLS if not (FONTS_DIR / n).exists()]

    if not missing:
        ok(f"All {len(FONT_URLS)} fonts already present in ./fonts/")
        return

    already = len(FONT_URLS) - len(missing)
    if already:
        info(f"{already} fonts already cached; downloading {len(missing)} missing …")
    else:
        info(f"Downloading {len(FONT_URLS)} fonts into ./fonts/ …")

    ok_count = fail_count = 0
    for name in missing:
        url   = FONT_URLS[name]
        dest  = FONTS_DIR / name
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            dest.write_bytes(data)
            kb = len(data) // 1024
            if kb < 10:
                warn(f"  tiny ({kb} KB) — possibly corrupt, removing: {name}")
                dest.unlink(missing_ok=True)
                fail_count += 1
            else:
                print(f"  {c('✓', '32')}  {name}  ({kb} KB)")
                ok_count += 1
        except Exception as exc:
            warn(f"  FAILED  {name}  ({exc})")
            if dest.exists():
                dest.unlink(missing_ok=True)
            fail_count += 1

    if ok_count:
        ok(f"{ok_count} font(s) downloaded to ./fonts/")
    if fail_count:
        warn(f"{fail_count} font(s) failed — subtitles will fall back to whichever fonts are present")

# ── Templates ──────────────────────────────────────────────────────────────────
def ensure_templates():
    tdir   = HERE / "templates"
    tindex = tdir / "index.html"
    src    = HERE / "index.html"
    if tindex.exists():
        ok("templates/index.html in place")
        return
    if src.exists():
        tdir.mkdir(exist_ok=True)
        shutil.copy2(src, tindex)
        ok("Copied index.html → templates/index.html")
    else:
        warn("index.html not found — the web UI may not load")

# ── .env ───────────────────────────────────────────────────────────────────────
def ensure_env():
    env = HERE / ".env"
    example = HERE / ".env.example"
    if not env.exists():
        if example.exists():
            shutil.copy2(example, env)
        else:
            env.write_text(
                "# ShortsAI — fill in your API keys before processing videos\n"
                "GROQ_API_KEY=\n"
                "DEEPGRAM_API_KEY=\n"
                "GEMINI_API_KEY=\n"
                "OPENROUTER_API_KEY=\n"
            )
        try:
            env.chmod(0o600)   # holds secrets once filled in
        except OSError:
            pass
        warn(".env created — add your keys in the browser (Setup button) or edit .env")
        return

    def _is_set(name):
        return any(ln.startswith(name + "=") and ln.split("=", 1)[1].strip()
                   for ln in env.read_text().splitlines())

    configured = [n for n in ("GROQ_API_KEY", "DEEPGRAM_API_KEY",
                              "GEMINI_API_KEY", "OPENROUTER_API_KEY") if _is_set(n)]
    if configured:
        ok(f".env — {len(configured)} key(s) set: {', '.join(configured)}")
    else:
        warn(".env found but no API keys are set — add them in the browser (Setup button)")

# ── Self-test ─────────────────────────────────────────────────────────────────
# Each _*_TEST_CODE snippet runs inside the venv so it can import installed packages.
# Exit code convention: 0 = pass, 1 = fail, 2 = not-configured/skipped.

_GROQ_TEST_CODE = """\
import os, sys
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
key = os.getenv("GROQ_API_KEY", "").strip()
if not key:
    print("NO_KEY"); sys.exit(2)
try:
    from groq import Groq
    models = Groq(api_key=key).models.list()
    whisper = [m.id for m in models.data if "whisper" in m.id.lower()]
    print("OK  ({} models, whisper: {})".format(
        len(models.data), ", ".join(whisper) if whisper else "none listed"))
except Exception as e:
    print("FAIL  {}".format(e)); sys.exit(1)
"""

_DEEPGRAM_TEST_CODE = """\
import os, sys, urllib.request, urllib.error
try:
    from dotenv import load_dotenv; load_dotenv()
except ImportError:
    pass
key = os.getenv("DEEPGRAM_API_KEY", "").strip()
if not key:
    print("NO_KEY"); sys.exit(2)
try:
    req = urllib.request.Request(
        "https://api.deepgram.com/v1/projects",
        headers={"Authorization": "Token " + key}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        print("OK  (HTTP {})".format(r.status))
except urllib.error.HTTPError as e:
    if e.code == 401:
        print("FAIL  401 Unauthorized — key is invalid"); sys.exit(1)
    print("FAIL  HTTP {}".format(e.code)); sys.exit(1)
except Exception as e:
    print("FAIL  {}".format(e)); sys.exit(1)
"""

# Minimal valid ASS file that exercises subtitle rendering.
_ASS_TEST_CONTENT = """\
[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,80,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,0,2,60,60,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{\\an2}ShortsAI subtitle test
"""


def _venv_run(code, timeout=20):
    """Run a Python snippet inside the venv.
    Returns (status, message): status True=pass, False=fail, None=not-configured."""
    vpy = str(venv_python())
    if not venv_python().exists():
        return None, "venv not ready — run without --skip-install first"
    try:
        result = subprocess.run(
            [vpy, "-c", code],
            capture_output=True, text=True,
            timeout=timeout, cwd=str(HERE),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode == 2:          # not-configured / optional
            return None, stdout or "not set"
        if result.returncode == 0:
            return True, stdout or "OK"
        last_err = stderr.splitlines()[-1] if stderr else ""
        return False, stdout or last_err or "unknown error"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s — check your internet connection"
    except Exception as e:
        return False, str(e)


def self_test():
    """Live-test both API keys and the full subtitle encode pipeline."""
    import tempfile

    hdr("Self-test")

    # 1 ── Groq API ────────────────────────────────────────────────────────────
    info("Groq API key …")
    status, msg = _venv_run(_GROQ_TEST_CODE, timeout=15)
    if status is True:
        ok(f"  Groq  {msg}")
    elif status is None:
        warn("  Groq  GROQ_API_KEY not set in .env")
        warn("    → Get a free key at console.groq.com")
    else:
        err(f"  Groq  INVALID — {msg}")
        warn("    → Check GROQ_API_KEY in .env  |  get a key at console.groq.com")

    # 2 ── Deepgram API (optional) ─────────────────────────────────────────────
    info("Deepgram API key (optional) …")
    status, msg = _venv_run(_DEEPGRAM_TEST_CODE, timeout=15)
    if status is True:
        ok(f"  Deepgram  {msg}")
    elif status is None:
        ok("  Deepgram  not configured  "
           "(optional — Hindi subtitles still work via Groq Whisper)")
    else:
        warn(f"  Deepgram  INVALID — {msg}")
        warn("    → Check DEEPGRAM_API_KEY in .env  |  leave it blank to disable")

    # 3 ── Subtitle engine ─────────────────────────────────────────────────────
    info("Subtitle engine (ffmpeg ASS burn + H.264 encode) …")
    if not have("ffmpeg"):
        warn("  Subtitle engine  skipped — ffmpeg not found")
        warn("    → Run with --install-ffmpeg to fix")
        return

    # Minimal per-encoder args for the 1-second smoke test (the real pipeline uses
    # richer rate-control args — here we only need to confirm the encode succeeds).
    _test_encoder_args = {
        "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4"],
        "h264_amf":   ["-c:v", "h264_amf", "-quality", "speed"],
        "h264_qsv":   ["-c:v", "h264_qsv", "-preset", "faster"],
        "libx264":    ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "35"],
    }
    # Mirror the render pipeline: actually test-encode each available encoder
    # (GPU first) and accept the first that works. This is what stops an AMD/NVIDIA
    # box without libx264 from being falsely told to "reinstall ffmpeg".
    candidates = _available_h264_encoders() or ["libx264"]
    last_err = ""
    try:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir   = Path(tmp)
            ass_file = tmpdir / "test.ass"
            ass_file.write_text(_ASS_TEST_CONTENT, encoding="utf-8")

            # Escape the subtitle path EXACTLY like the render pipeline
            # (burn_subtitles._escape_ffmpeg_path): backslash->slash and escape ':'
            # and "'". Without this, a Windows path (C:\Users\...) breaks ffmpeg's
            # filtergraph parser and the test fails for EVERY encoder — which is what
            # made Windows clients see the bogus "reinstall ffmpeg" message.
            ass_filter = "subtitles='{}'".format(
                str(ass_file).replace("\\", "/").replace("'", "\\'").replace(":", "\\:"))

            for enc in candidates:
                out_file = tmpdir / f"out_{enc}.mp4"
                result = subprocess.run(
                    [
                        "ffmpeg", "-y", "-loglevel", "error",
                        # synthetic 1-second 1080×1920 black source — no real input needed
                        "-f", "lavfi", "-i",
                        "color=c=black:size=1080x1920:rate=30:duration=1",
                        "-vf", ass_filter,                  # burn the subtitle (escaped path)
                        *_test_encoder_args.get(enc, ["-c:v", enc]),
                        "-pix_fmt", "yuv420p", "-t", "1", str(out_file),
                    ],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0 and out_file.exists() \
                        and out_file.stat().st_size > 2_000:
                    size_kb = out_file.stat().st_size // 1024
                    ok(f"  Subtitle engine  OK  (encoder: {enc}, {size_kb} KB test clip)")
                    break
                lines = (result.stderr or "").strip().splitlines()
                last_err = lines[-1] if lines else "no output produced"
            else:
                # Every available encoder failed — diagnose the ACTUAL cause instead
                # of blaming libx264, which this box may not even use.
                err(f"  Subtitle engine  FAILED — {last_err}")
                low = last_err.lower()
                if "ass" in low or "subtitle" in low or "libass" in low or "filter" in low:
                    warn("    → ffmpeg build lacks libass (subtitle burn-in). Install a FULL ffmpeg build "
                         "(Windows: gyan.dev 'ffmpeg-release-full').")
                else:
                    warn("    → No usable H.264 encoder. Install a FULL ffmpeg build "
                         "(Windows: gyan.dev 'ffmpeg-release-full'; Linux: --install-ffmpeg).")
    except subprocess.TimeoutExpired:
        warn("  Subtitle engine  timed out (ffmpeg took > 30 s — machine may be overloaded)")
    except Exception as e:
        err(f"  Subtitle engine  unexpected error — {e}")


# ── Final readiness summary ────────────────────────────────────────────────────
def readiness_summary():
    issues = []
    if not _ffmpeg_ok():
        if not have("ffmpeg"):
            issues.append("ffmpeg missing  →  run with --install-ffmpeg")
        else:
            issues.append("ffmpeg has no usable H.264 encoder (nvenc/amf/qsv/libx264)  "
                          "→  install a FULL ffmpeg build")
    fonts_present = sum(1 for n in FONT_URLS if (FONTS_DIR / n).exists())
    if fonts_present == 0:
        issues.append("no fonts in ./fonts/  →  run with --skip-fonts=false or bash fetch_fonts.sh")
    env = HERE / ".env"
    text = env.read_text() if env.exists() else ""
    def _is_set(name):
        return any(ln.startswith(name + "=") and ln.split("=", 1)[1].strip()
                   for ln in text.splitlines())

    if not (_is_set("GROQ_API_KEY") or _is_set("DEEPGRAM_API_KEY")):
        issues.append("no transcription key set  →  add GROQ_API_KEY "
                      "(free at console.groq.com) in the browser Setup panel")

    if issues:
        print()
        warn("Action needed before the app will fully work:")
        for issue in issues:
            print(f"  {c('→', '33')} {issue}")
    else:
        print()
        ok("All systems go — ready to process videos")

# ── Desktop shortcut / app launcher ────────────────────────────────────────────
# Goal: after the one-line install, ShortsAI opens like a normal app — from the
# Start Menu on Windows, the applications menu on Linux, ~/Applications on macOS —
# instead of the user having to remember a terminal command.
#
# Created once (tracked by SHORTCUT_STAMP) so deleting the shortcut on purpose
# doesn't make it reappear on every launch. `--shortcut` forces a rebuild.

ASSETS         = HERE / "assets"
SHORTCUT_STAMP = VENV / ".shortcut"
WIN_LAUNCHER   = HERE / "ShortsAI.bat"


def _win_make_shortcut():
    """Create Start Menu + Desktop .lnk files pointing at ShortsAI.bat.

    Uses WScript.Shell via PowerShell — available on every Windows without extra
    packages. Desktop/Start Menu paths come from GetFolderPath rather than being
    built by hand, so OneDrive-redirected folders resolve correctly.
    """
    if not WIN_LAUNCHER.exists():
        warn("ShortsAI.bat missing — cannot create a shortcut")
        return False

    icon = ASSETS / "piksy.ico"
    # A PowerShell here-string would need escaping for every path, so values are
    # injected as variables with doubled single-quotes instead.
    def ps_str(p):
        return "'" + str(p).replace("'", "''") + "'"

    script = f"""
$ErrorActionPreference = 'Stop'
$target  = {ps_str(WIN_LAUNCHER)}
$workdir = {ps_str(HERE)}
$icon    = {ps_str(icon)}
$shell   = New-Object -ComObject WScript.Shell

function New-ShortsAILink($path) {{
    $lnk = $shell.CreateShortcut($path)
    $lnk.TargetPath       = $target
    $lnk.WorkingDirectory = $workdir
    $lnk.Description      = 'ShortsAI - turn long videos into vertical Shorts'
    # ",0" selects the first icon in the file; WScript.Shell expects path,index.
    if (Test-Path $icon) {{ $lnk.IconLocation = "$icon,0" }}
    $lnk.Save()
    Write-Output $path
}}

$desktop = [Environment]::GetFolderPath('Desktop')
$startup = Join-Path ([Environment]::GetFolderPath('StartMenu')) 'Programs'
if (-not (Test-Path $startup)) {{ New-Item -ItemType Directory -Path $startup -Force | Out-Null }}

New-ShortsAILink (Join-Path $desktop 'ShortsAI.lnk')
New-ShortsAILink (Join-Path $startup 'ShortsAI.lnk')
"""
    ps1 = HERE / ".shortcut.ps1"
    try:
        # utf-8-sig: Windows PowerShell 5.1 decodes BOM-less .ps1 files as ANSI,
        # which mangles any non-ASCII character in a path (e.g. a user folder
        # like C:\Users\José). The BOM forces correct UTF-8 decoding.
        ps1.write_text(script, encoding="utf-8-sig")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            warn(f"Could not create the Windows shortcut: "
                 f"{(result.stderr or '').strip().splitlines()[-1] if result.stderr else 'unknown error'}")
            return False
        for line in (result.stdout or "").strip().splitlines():
            ok(f"Shortcut created: {line.strip()}")
        return True
    except Exception as e:
        warn(f"Could not create the Windows shortcut: {e}")
        return False
    finally:
        try:
            ps1.unlink()
        except OSError:
            pass


def _linux_make_shortcut():
    """Write a .desktop entry so ShortsAI shows up in the applications menu."""
    apps = Path.home() / ".local" / "share" / "applications"
    try:
        apps.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        warn(f"Could not create {apps}: {e}")
        return False

    icon = ASSETS / "piksy.png"
    # Exec must be an absolute command; %-signs are reserved in .desktop files.
    entry = (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ShortsAI\n"
        "GenericName=Short-form video maker\n"
        "Comment=Turn long videos into vertical Shorts with AI captions\n"
        f"Exec={sys.executable} {HERE / 'run.py'}\n"
        f"Path={HERE}\n"
        f"Icon={icon if icon.exists() else 'video-x-generic'}\n"
        "Terminal=true\n"
        "Categories=AudioVideo;Video;\n"
        "Keywords=video;shorts;reels;subtitles;captions;\n"
    )
    dest = apps / "shortsai.desktop"
    try:
        dest.write_text(entry, encoding="utf-8")
        dest.chmod(0o755)
    except OSError as e:
        warn(f"Could not write {dest}: {e}")
        return False

    # Refresh the menu database when the tool is available (not on minimal WMs).
    if have("update-desktop-database"):
        try:
            subprocess.run(["update-desktop-database", str(apps)],
                           capture_output=True, timeout=20)
        except Exception:
            pass
    ok(f"Shortcut created: {dest}")
    return True


def _mac_make_shortcut():
    """Write a double-clickable .command into ~/Applications."""
    apps = Path.home() / "Applications"
    try:
        apps.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        warn(f"Could not create {apps}: {e}")
        return False

    dest = apps / "ShortsAI.command"
    script = (
        "#!/bin/bash\n"
        "# ShortsAI — double-click launcher (macOS)\n"
        f'cd "{HERE}" || exit 1\n'
        f'exec "{sys.executable}" run.py "$@"\n'
    )
    try:
        dest.write_text(script, encoding="utf-8")
        dest.chmod(0o755)
    except OSError as e:
        warn(f"Could not write {dest}: {e}")
        return False
    ok(f"Shortcut created: {dest}")
    info("  (first double-click: right-click → Open, to clear Gatekeeper)")
    return True


def _reopen_hint(os_type):
    """One line telling the user how to open ShortsAI again next time — the
    shortcut if they have one, otherwise the command."""
    if SHORTCUT_STAMP.exists():
        if os_type == "windows":
            return "Next time, open " + c("ShortsAI", "1") + " from the Start Menu or your Desktop."
        if os_type == "mac":
            return "Next time, double-click " + c("ShortsAI", "1") + " in ~/Applications."
        if os_type == "linux":
            return "Next time, open " + c("ShortsAI", "1") + " from your applications menu."
    return "Start any time with:  " + c(f"cd {HERE} && python3 run.py", "1")


def ensure_shortcut(os_type, force=False, skip=False):
    """Create the desktop/menu launcher once. Never fatal — a failed shortcut
    must not stop the app from starting."""
    if skip:
        return
    if SHORTCUT_STAMP.exists() and not force:
        return
    try:
        if os_type == "windows":
            created = _win_make_shortcut()
        elif os_type == "mac":
            created = _mac_make_shortcut()
        elif os_type == "linux":
            created = _linux_make_shortcut()
        else:
            # WSL has no desktop of its own — the app is reached from the Windows browser.
            return
        if created:
            try:
                SHORTCUT_STAMP.write_text("ok\n")
            except OSError:
                pass
    except Exception as e:
        warn(f"Shortcut creation skipped: {e}")


# ── Launch ─────────────────────────────────────────────────────────────────────
def _port_is_free(host, port):
    import socket
    probe_host = "127.0.0.1" if host == "0.0.0.0" else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((probe_host, port)) != 0


def pick_port(host, port, span=20):
    """Return the first free port at or after `port`.

    Re-running the launcher while an old server is still up would otherwise die with
    'address already in use' — the common case being a second terminal.
    """
    for candidate in range(port, port + span):
        if _port_is_free(host, candidate):
            if candidate != port:
                warn(f"Port {port} is busy — using {candidate} instead")
            return candidate
    warn(f"Ports {port}–{port + span - 1} are all busy; trying {port} anyway")
    return port


def _keys_missing():
    """True when no transcription key is configured — the one thing that blocks
    every pipeline run, so the browser should land on the setup panel."""
    env = HERE / ".env"
    if not env.exists():
        return True
    text = env.read_text()
    for name in ("GROQ_API_KEY", "DEEPGRAM_API_KEY"):
        for line in text.splitlines():
            if line.startswith(name + "=") and line.split("=", 1)[1].strip():
                return False
    return True


def open_browser_when_up(url, port, host):
    """Open the browser once the server actually answers, so the user never sees a
    connection-refused page from opening too early."""
    import threading
    import webbrowser

    def _wait():
        import time
        probe_host = "127.0.0.1" if host == "0.0.0.0" else host
        import socket
        for _ in range(100):          # ~20s
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((probe_host, port)) == 0:
                    try:
                        webbrowser.open(url)
                    except Exception:
                        pass
                    return
            time.sleep(0.2)

    threading.Thread(target=_wait, daemon=True).start()


def launch(host, port, reload_, open_browser=True):
    vpy = str(venv_python())
    port = pick_port(host, port)
    cmd = [vpy, "-m", "uvicorn", "app:app", "--host", host, "--port", str(port)]
    if reload_:
        cmd.append("--reload")

    shown = host if host != "0.0.0.0" else "localhost"
    url = f"http://{shown}:{port}"
    # Land first-time users straight on the API-key form.
    target = url + "/#setup" if _keys_missing() else url

    print()
    ok(f"Starting ShortsAI  →  {url}")
    if host == "0.0.0.0":
        info("Reachable from other devices on your network at this machine's IP.")
    if open_browser:
        info("Opening your browser…")
        open_browser_when_up(target, port, host)
    if SHORTCUT_STAMP.exists():
        info(_reopen_hint(detect_os()[0]))
    info("Press Ctrl+C to stop.\n")
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        print()
        info("Stopped.")

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(
        description="Set up and run ShortsAI with one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--host",           default="127.0.0.1",
                    help="Bind host (use 0.0.0.0 to expose on LAN/phone)")
    ap.add_argument("--port",           type=int, default=8000,
                    help="Port (default 8000)")
    ap.add_argument("--reload",         action="store_true",
                    help="Dev mode: auto-restart when you edit Python files")
    ap.add_argument("--reinstall",      action="store_true",
                    help="Force re-run pip install even if already done")
    ap.add_argument("--skip-install",   action="store_true",
                    help="Skip pip install entirely (use if deps are already installed)")
    ap.add_argument("--install-ffmpeg", action="store_true",
                    help="Auto-install or fix ffmpeg via system package manager")
    ap.add_argument("--skip-fonts",     action="store_true",
                    help="Skip font downloads (faster re-runs after first setup)")
    ap.add_argument("--no-sudo",        action="store_true",
                    help="Skip every step that requires sudo")
    ap.add_argument("--setup-only",     action="store_true",
                    help="Run all setup steps but do not start the server")
    ap.add_argument("--skip-test",      action="store_true",
                    help="Skip API key and subtitle engine self-tests")
    ap.add_argument("--no-browser",     action="store_true",
                    help="Do not open the web UI in a browser on start")
    ap.add_argument("--shortcut",       action="store_true",
                    help="(Re)create the desktop / Start Menu shortcut and exit")
    ap.add_argument("--no-shortcut",    action="store_true",
                    help="Do not create a desktop / Start Menu shortcut")
    args = ap.parse_args()

    os.chdir(HERE)
    os_type, distro = detect_os()
    sudo = not args.no_sudo

    label = f"{os_type}" + (f" / {distro}" if distro else "")
    print(c("\n  ShortsAI launcher", "1") + c(f"  ({label})\n", "90"))

    # Standalone shortcut repair — no setup, no server.
    if args.shortcut:
        hdr("Desktop shortcut")
        ensure_shortcut(os_type, force=True)
        return

    hdr("Python")
    check_python()
    fresh = ensure_venv()

    hdr("Python packages")
    if not args.skip_install:
        install_deps(force=args.reinstall or (fresh and not STAMP.exists()))
    else:
        warn("Skipping pip install  (--skip-install)")
    check_ytdlp()

    hdr("ffmpeg")
    ensure_ffmpeg(os_type, distro, args.install_ffmpeg, sudo)

    hdr("Fonts  (18 families)")
    ensure_fonts(args.skip_fonts)

    hdr("App files")
    ensure_templates()
    ensure_env()
    ensure_shortcut(os_type, skip=args.no_shortcut)

    readiness_summary()

    if not args.skip_test:
        self_test()

    if args.setup_only:
        print()
        ok("Setup complete.  " + _reopen_hint(os_type))
        return

    launch(args.host, args.port, args.reload, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
