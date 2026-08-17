# ShortsAI — Installation Guide

How to install ShortsAI on **Windows**, **Linux** or **macOS**, from nothing to your
first clip. No coding needed — you paste one line, then one API key.

**Total time:** about 10 minutes, most of it waiting for downloads.

Jump to your system:
[Windows](#windows) · [Linux](#linux) · [macOS](#macos) · [Getting your API key](#step-2--get-your-free-api-key) ·
[Making your first clips](#step-3--make-your-first-clips) · [Troubleshooting](#troubleshooting)

---

## Before you start

You need two free things installed. Every system section below tells you exactly how
to get them — this is just so you know what they are.

| | What it is | Why ShortsAI needs it |
|---|---|---|
| **Git** | Downloads code from the internet | To fetch ShortsAI itself |
| **Python 3.10 or newer** | The language ShortsAI is written in | To run it |

Everything else — ffmpeg, 18 caption fonts, all the Python packages — is installed
**for you** by the installer. You don't need to know what any of it is.

You'll also want **5 GB of free disk space** and a normal internet connection.

---

# Windows

### Step 1 — Install Git and Python

Open **PowerShell**: press the Windows key, type `powershell`, press Enter.

Paste these two lines, one at a time:

```powershell
winget install --id Git.Git -e
winget install --id Python.Python.3.12 -e
```

> **⚠ Now close PowerShell completely and open a new one.**
>
> This is the single most common thing that goes wrong. Windows only notices newly
> installed programs in windows opened *afterwards*. If you skip this, the next step
> fails with "Python 3.10+ is required" even though you just installed it.

*Already have Python?* Check with `python --version` — if it says 3.10 or higher,
skip this step. If it opens the Microsoft Store instead, use the winget command above.

### Step 2 — Install ShortsAI

In your **new** PowerShell window, paste:

```powershell
irm https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.ps1 | iex
```

Press Enter and wait. You'll see it clone the code, install packages, download ffmpeg
and the fonts. **The first run takes several minutes** — that's normal, let it finish.

You don't need to change any security settings — `irm | iex` isn't affected by
PowerShell's script-execution policy.

Windows may pop up a **"Do you want to allow this app to make changes?"** box while
ffmpeg installs. Click **Yes**; that's Windows installing the video engine, and it's
the only prompt you should see.

### Step 3 — Done

When it finishes, your browser opens automatically and you'll have:

- **ShortsAI** in your Start Menu
- A **ShortsAI** icon on your Desktop

ShortsAI is installed at `C:\Users\<your name>\shortsai`.

### Opening it next time

Click **ShortsAI** in the Start Menu or on your Desktop. That's it — no commands ever
again.

A small black window opens alongside the app. **Leave it open while you're using
ShortsAI** — closing it stops the app. That window is also where errors appear if
something goes wrong.

Now go to [Step 2 — Get your free API key](#step-2--get-your-free-api-key).

---

# Linux

### Step 1 — Install Git and Python

Open a terminal and run the line for your distribution:

```bash
# Ubuntu / Debian / Mint / Pop!_OS
sudo apt update && sudo apt install -y git python3 python3-venv

# Fedora
sudo dnf install -y git python3

# Arch / Manjaro
sudo pacman -S --noconfirm git python

# openSUSE
sudo zypper install -y git python3
```

### Step 2 — Install ShortsAI

```bash
curl -fsSL https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.sh | bash
```

**You'll be asked for your password once**, when it installs ffmpeg through your
package manager. That's expected — installing system software needs it.

> Don't want to give sudo access? Install ffmpeg yourself first (`sudo apt install
> ffmpeg`, etc.), then run the installer with `bash -s -- --no-sudo` appended.

ShortsAI installs to `~/shortsai`.

### Step 3 — Done

Your browser opens automatically, and **ShortsAI** appears in your applications menu.

### Opening it next time

Open **ShortsAI** from your applications menu (press the Super/Windows key and type
"ShortsAI"). A terminal window opens with it — **leave it open while you work**;
closing it stops the app.

If your desktop doesn't show the new entry right away, log out and back in.

Now go to [Step 2 — Get your free API key](#step-2--get-your-free-api-key).

---

# macOS

### Step 1 — Install Git and Python

Open **Terminal** (press `Cmd + Space`, type `terminal`, press Enter).

If you don't have Homebrew yet, install it first:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Then:

```bash
brew install git python@3.12
```

*Apple Silicon (M1/M2/M3/M4) and Intel Macs both work.*

### Step 2 — Install ShortsAI

```bash
curl -fsSL https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.sh | bash
```

It installs ffmpeg via Homebrew along the way. ShortsAI installs to `~/shortsai`.

### Step 3 — Done

Your browser opens automatically, and **ShortsAI.command** is placed in
`~/Applications`.

### Opening it next time

Open Finder → **Applications** (your home one, under your user) → double-click
**ShortsAI**.

> **First time only:** macOS blocks apps it didn't download itself. Right-click
> **ShortsAI** → **Open** → **Open**. After that, double-clicking works normally.

A Terminal window opens with it — **leave it open while you work**; closing it stops
the app.

Now continue below.

---

# Step 2 — Get your free API key

ShortsAI needs one key to work. It's free and takes about 30 seconds.

1. Go to **[console.groq.com/keys](https://console.groq.com/keys)**
2. Sign in with Google or GitHub
3. Click **Create API Key**, give it any name
4. **Copy the key** — it starts with `gsk_`

> Copy it immediately. Groq only shows the key once; if you lose it, just create
> another.

Now in the ShortsAI page in your browser:

1. Click **Setup** in the top-right
2. Paste the key into the **Groq** box
3. Click **Save**

The dot at the top-right turns **green ● Ready**. You're done — no files to edit, no
restart needed.

### Optional extra keys

You never need these, but they make ShortsAI better. Add them the same way, in the
same Setup panel.

| Key | What it adds | Where |
|---|---|---|
| **Deepgram** | Noticeably better Hindi caption accuracy | [console.deepgram.com](https://console.deepgram.com/signup) |
| **Gemini** | Backup brain if Groq is busy or rate-limited | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **OpenRouter** | Last-resort backup, one key covers many models | [openrouter.ai/keys](https://openrouter.ai/keys) |

Adding Gemini is the single best thing you can do for reliability — Groq's free tier
has hourly limits, and when it hits one, ShortsAI switches to Gemini automatically
instead of failing.

---

# Step 3 — Make your first clips

1. **Add your video** — paste a YouTube link, or drag a video file onto the page
2. **Describe what you want** (optional) — type things like:
   - `only the parts about money and business`
   - `funny moments and reactions`
   - `where he tells a personal story`

   Leave it blank and the AI just picks the most engaging moments on its own.
3. **Choose your captions** — language, position and style, with a live preview
4. Click **Generate**

Clips appear on the page one by one as they finish. Download them individually, or
grab everything as a ZIP.

### Start from where you're posting

At the top of **How should it be cut?**, pick **YouTube Shorts**, **Instagram Reels** or
**Instagram Feed**. Everything — shape, clip length, caption style — is set correctly
for that platform straight away.

It's only a starting point: change anything you like and the choice quietly switches to
**Custom**, so the label never claims a setting you've since changed.

### The four ways to cut

| Mode | What you get |
|---|---|
| **Many clips** | Roughly one clip per minute — the widest net |
| **Best moments only** | Fewer, stronger clips (about one per two minutes) |
| **Hook first** | Opens on the clip's best line, then plays it in full |
| **Podcast / interview** | A question plus the whole answer — never cuts into the next question |
| **Sequential parts** | The whole video split end to end into Part 1, Part 2, Part 3… |

**Podcast / interview** is the one for two-person shows. It works out who is the host
and who is the guest from the audio, and cuts on speaker turns — so a clip ends when
the guest finishes answering, instead of running into the next question. You can also
give the host and guest different caption colours. It needs a Deepgram key; without
one it quietly falls back to normal clip picking.

**Hook first** is the one to try if clips feel slow to start. It fixes clips at 40s–1:00,
uses Gemini to pick them, and guarantees each clip is a complete thought — it will never
stop you mid-sentence.

### Cutting a video into Part 1, Part 2, Part 3…

Instead of letting the AI pick highlights, you can split the **whole** video into
equal pieces — the format people use for long stories told across many Shorts.

1. In **What should the AI look for?**, choose **Sequential parts**
2. Set **Length of each part** (30 seconds is the usual choice)
3. Optionally add a **title on every part**, e.g. `Motivation Series`
4. Click **Generate**

You get `part_1.mp4`, `part_2.mp4`, `part_3.mp4`… where each one picks up exactly
where the last ended. A 10-minute video at 30s gives 20 parts; a 1-hour video gives
120.

> Turn captions **off** for this mode and it's much faster — with nothing to
> transcribe, ShortsAI only has to cut and re-encode.

### The Piksy watermark

Every clip gets the Piksy logo in the corner at 40% opacity — faint enough not to
distract, clear enough that anyone who reposts your clip shows where it came from.

Don't want it? **Headline & logo → Piksy watermark → off.** Uploading your own logo
replaces it automatically.

### Moving the captions, title and Part badge

In the **Live preview**, drag any of them — the caption, the title, the "PART 1"
badge — wherever you want on the frame. Where you drop it is exactly where it burns
into the video. Press **Reset** to put everything back.

> **The first video is the slow one.** A 10-minute video takes a few minutes on a
> normal laptop. Your clips are saved in the `output` folder inside the ShortsAI
> directory and stay there until you delete them.

---

# Troubleshooting

### "Python 3.10+ is required" — but I just installed it

You're in the PowerShell/terminal window that was open *before* the install. Close it
completely, open a new one, and run the installer again.

### "git is not recognized" / "command not found: git"

Same cause as above — close the window and open a new one. If it still fails, Git
didn't install; run the Git command from Step 1 again and read its output.

### The page doesn't open by itself

The app is probably still running fine. Open your browser and go to:
**http://localhost:8000**

If that doesn't load, look at the terminal window — the real address is printed there
as `Starting ShortsAI → http://...`. It may be on port 8001 or higher if 8000 was
already taken.

### The shortcut wasn't created

Open a terminal in the ShortsAI folder and run:

```bash
# Windows
cd $HOME\shortsai ; python run.py --shortcut

# Linux / macOS
cd ~/shortsai && python3 run.py --shortcut
```

It prints the actual error if it can't create one.

### Hindi captions show as empty boxes

A font didn't download. Just run ShortsAI again — it re-fetches missing fonts on every
start. If it persists, the terminal window will show which font failed.

### It says my API key is invalid

Check you copied the whole key including the `gsk_` at the start, with no spaces. Keys
can also be deleted from the Groq console — make a fresh one if unsure.

### A video won't download

Some YouTube videos are age-restricted or region-locked and can't be downloaded
without signing in. Try a different video first to confirm everything else works. For
private or restricted videos, see the "Private / age-restricted videos" section in
`README.md`.

### Something else went wrong

Every job writes a detailed log. Look in the ShortsAI folder under
`output/<job-id>/DIAGNOSTIC_REPORT.txt` — it records each step and exactly where
things failed.

---

# Updating

Run the same install command again. It updates ShortsAI to the latest version and
keeps your API keys.

```powershell
# Windows
irm https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.ps1 | iex
```
```bash
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.sh | bash
```

> **Careful:** if you've edited any of ShortsAI's files yourself, the update refuses to
> run rather than overwriting your changes. Save copies of your edits elsewhere first.

---

# Uninstalling

ShortsAI keeps everything in one folder and doesn't touch the rest of your system.

1. Delete the folder: `C:\Users\<name>\shortsai` (Windows) or `~/shortsai` (Linux/macOS)
2. Delete the shortcut:
   - **Windows** — right-click the Start Menu / Desktop icon → Delete
   - **macOS** — delete `~/Applications/ShortsAI.command`
   - **Linux** — `rm ~/.local/share/applications/shortsai.desktop`

ffmpeg, Git and Python stay installed — they're normal software other programs use
too. Remove them separately if you want.

---

# Quick reference

| I want to… | Do this |
|---|---|
| Open ShortsAI | Start Menu / Applications / apps menu → **ShortsAI** |
| Stop ShortsAI | Close its terminal window (or press `Ctrl+C` in it) |
| Change my API keys | **Setup** button, top-right of the page |
| Find my finished clips | The `output` folder inside the ShortsAI directory |
| Use it from my phone | Start it with `--host 0.0.0.0`, then visit this PC's IP address |
| Update to the latest | Re-run the install command |
| See what went wrong | `output/<job-id>/DIAGNOSTIC_REPORT.txt` |

More detail on caption styles, the API and advanced settings is in
**[README.md](README.md)**.
