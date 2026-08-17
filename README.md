# ShortsAI — Hindi Viral Clip Studio

Turn a long Hindi video (file or link) into ready-to-post **9:16 vertical Shorts**.
Describe the clips you want in plain English, and the AI finds those moments, cuts
them clean at full sentences, and burns in captions exactly how you like them.

---

## Install — one command

Everything (Python packages, ffmpeg, 18 caption fonts) is installed for you, and the
web page opens automatically.

**Linux · macOS · WSL**
```bash
curl -fsSL https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.sh | bash
```

**Windows (PowerShell)**
```powershell
irm https://raw.githubusercontent.com/ashishprajapat0604/short_aiv2/version2/install.ps1 | iex
```

You only need **git** and **Python 3.10+** beforehand; the installer tells you exactly
how to get them if they're missing.

> **New to this?** [**GUIDE.md**](GUIDE.md) walks through it step by step for Windows,
> Linux and macOS — including how to get the free API key and what to do when
> something goes wrong.

Then paste your **Groq API key** (free, 30 seconds) into the Setup panel that opens in
your browser — and you're done. Nothing else to configure, no files to edit.

### Opening it again later

The installer adds a **ShortsAI** shortcut, so there's no command to remember:

| | Where to find it |
|---|---|
| **Windows** | **Start Menu → ShortsAI**, or the ShortsAI icon on your Desktop |
| **macOS** | `~/Applications → ShortsAI` (first time: right-click → Open) |
| **Linux** | Your applications menu → **ShortsAI** |

Click it and the app starts and opens your browser by itself. A small terminal window
stays open while it runs — **closing that window stops ShortsAI.**

Shortcut missing or the folder moved? Rebuild it:
```bash
python3 run.py --shortcut
```

### Desktop app or browser?

```bash
python3 run.py --app     # native window
python3 run.py           # browser tab
```

The shortcut uses `--app`, so clicking **Piksy** opens a real window with its own
taskbar entry and no address bar.

**This changes the window, not the work.** All the processing — ffmpeg,
transcription, clip selection — has always run as Python on your machine; the
browser was only ever a viewport onto `localhost`. Nothing gets faster or more
local by switching. What you do gain:

- a native **file picker** — a 2 GB source is read where it sits instead of being
  copied through an HTTP upload into `uploads/`
- closing the window stops the server (no orphaned process holding the port)
- no browser tab, no address bar, its own icon

Windows and macOS get their webview automatically. Linux needs a system package:

```bash
sudo dnf install python3-gobject webkit2gtk4.1      # Fedora
sudo apt install python3-gi gir1.2-webkit2-4.1      # Debian/Ubuntu
```

Without one, `--app` says so and opens the browser instead — a missing GUI
toolkit costs you the window, not the app.

### Already have the repo?

```bash
python3 run.py
```

That single command sets up anything missing and starts the app. Re-run it any time.

| Flag | What it does |
|---|---|
| `--port 9000` | Use a different port (busy ports are skipped automatically) |
| `--host 0.0.0.0` | Reach it from your phone on the same Wi-Fi |
| `--no-browser` | Don't open a browser |
| `--reload` | Dev mode: restart on code changes |
| `--setup-only` | Install everything but don't start |
| `--reinstall` | Force-refresh the Python packages |
| `--skip-fonts` | Skip font downloads on re-runs |
| `--shortcut` | Rebuild the desktop / Start Menu shortcut and exit |
| `--no-shortcut` | Don't create a shortcut |
| `--app` | Open in a native desktop window instead of a browser tab |

---

## API keys

Click **Setup** in the top-right of the page. Keys are written to a local `.env` file
(owner-only permissions) and take effect immediately — no restart.

| Key | Needed? | What it does |
|---|---|---|
| **Groq** | **Yes** (free) | Transcription + AI clip selection |
| Deepgram | Optional | Best Hindi caption accuracy |
| Google Gemini | Optional | Fallback when Groq is rate-limited |
| OpenRouter | Optional | Last-resort text fallback |

Only Groq is required. The others are **fallbacks** — every extra key you add makes the
pipeline harder to break. You can also copy `.env.example` to `.env` and edit it by hand.

> For safety, the Setup panel only accepts key changes from the machine running
> ShortsAI, even when you serve it on `0.0.0.0`.

---

## Describe the clips you want

The **"Describe the clips you want"** box is the fastest way to get clips you'll
actually post. Write it like you'd brief an editor:

> *Only the parts where he talks about failure and bouncing back — skip the intro and any promo talk.*

> *Funny moments and audience reactions. No serious advice.*

> *Anything about pricing, revenue, or how much things cost.*

When a brief is set, the AI returns **only** matching moments and explains the match in
each clip's reason — so you may get fewer clips than requested. That's deliberate: a
brief means "these clips or none", not "pad it out". Leave it blank to let the AI pick
the most viral moments generally.

---

## Logs

Two files per run, for two different questions.

**`logs/shortsailogs/<time>_<job>_<stage>.log`** — *what happened?* One line per
step, one line per AI call, and a verdict. Read this first:

```
  STEPS
    [OK  ]    0.6s  Extract audio
    [OK  ]   10.3s  Transcribe full video
    [OK  ]   54.9s  Pick clips              2 clip(s)

  AI CALLS   (which model was asked, and what it said)
    [FAIL] transcribe  deepgram nova-3            HTTP 401
    [OK  ] transcribe  groq whisper-large-v3      17 segments
    [FAIL] chat        gemini gemini-3.7-flash    HTTP 503
    [OK  ] chat        gemini gemini-flash-latest

  RESULT  : 2 clip(s) selected
  FAILURES: 0 step(s), 2 model call(s)
```

That run **succeeded** despite two model failures — which is the point of the
fallback chains, and the reason failed calls are listed separately from failed
steps. A failed call that was covered is information; a failed *step* is a problem.

**`output/<job>/DIAGNOSTIC_REPORT.txt`** — *why, exactly?* Every ffmpeg command,
every prompt, every retry. Reach for it only when the short log is not enough.

---

## Built to not break

Every step that can fail has somewhere to fall back to, so one outage never sinks a job.

| Step | Fallback chain |
|---|---|
| Text / AI selection | Gemini → Groq → OpenRouter |
| Transcription | Deepgram → Groq Whisper → local faster-whisper (offline) |
| Per-clip captions | Deepgram → Groq → local → slice the full transcript |
| Video download | cookies → no cookies → android client → any format |
| Audio extraction | 16 kHz → 44.1 kHz → forgiving re-encode |
| Video encoding | NVENC → AMF → QSV → CPU (always works) |
| Caption rendering | captions → CPU + captions → **render without captions** |
| No transcript at all | evenly spaced time-based clips, captions off |
| Translation fails | falls back to Hindi captions rather than blank ones |

Plus: every external command has a **timeout** and retries, so a hung `ffmpeg` or
`yt-dlp` can't freeze a job. Jobs are **saved to disk**, so finished clips survive a
server restart (jobs interrupted mid-run are marked failed instead of spinning forever).

Order is configurable — see `CHAT_ORDER` / `TRANSCRIBE_ORDER` in `.env.example`.

### Long videos

A transcript too large for one call is split into chunks, each chunk is analysed
separately, and the picks are then ranked **against each other** so clips from
different parts of the video compete fairly (a chunk only ever sees its own slice, so
its scores mean nothing on their own).

Chunk size is chosen per model, which is why the picker matters on long uploads.
Gemini Flash takes a 60-minute transcript in **one** call; Groq's free tier allows
12,000 tokens *per minute* in total, and Hindi costs about 0.5 tokens per character,
so the same transcript becomes a dozen calls that throttle each other. Gemini is the
default for exactly this reason — a rate-limited pick still falls back to Groq.

### Offline transcription (optional)

```bash
pip install faster-whisper          # offline transcription, never rate-limited
```

---

## What you can control

| Feature | Options | Default |
|---|---|---|
| Clip brief | free text — describe what you want | *(blank)* |
| Platform preset | YouTube Shorts · Instagram Reels · Instagram Feed · Custom | Custom |
| Clip mode | `multi` (~1/min, 20–40s) · `best` (fewer, 40–60s) · `hook` (cold open, 40–60s) · `podcast` (speaker turns) · `sequential` (whole video → parts) | multi |
| Podcast clip shape | `both` · `qa` (question + answer) · `guest` (guest only) | both |
| Speaker caption colours | one colour per speaker (host / guest) | off |
| Number of clips | auto (1 per minute) or 5–60 | auto |
| Part length *(sequential)* | 10s – 5min | 30s |
| Series title *(sequential)* | free text, burned on every part | *(blank)* |
| "Part 1" badge *(sequential)* | on / off | on |
| Burn subtitles | on / off | on |
| Layout | `single` · `dual` (Hindi ↑ / English ↓) | single |
| Language | `hindi` · `english` · `hinglish` | hindi |
| Position | `top` · `middle` · `bottom` · `below`, or **drag anywhere** | bottom |
| Position *(dual)* | Hindi and English placed **independently** | classic stack |
| Caption size | 60%–180% of the style's own size | 100% |
| Caption look | outline · box · white_box · bold_yellow · karaoke · neon · retro · shadow · fire · fade | outline |
| Fonts | 8 Devanagari · 10 Latin | Noto / Poppins |
| AI title line | on / off | off |
| Piksy watermark | on / off (40% opacity) | on |

### Sequential parts mode

Splits the **whole** video into back-to-back clips instead of hunting for highlights:

```
Part 1 = 0–30s   Part 2 = 30–60s   Part 3 = 60–90s   …
```

Every part begins exactly where the previous one ended, so playing them in order
reproduces the original with nothing missing and nothing repeated. Files are named
`part_1.mp4`, `part_2.mp4`, … and each can carry a **Part N** badge plus a fixed
series title.

A trailing remainder becomes its own part when it's at least a third of a full part
(minimum 5s); anything shorter is absorbed into the last part, so you never get a
2-second orphan clip.

Unlike highlight mode, these cuts are **not** snapped to sentence boundaries — the
promise is gapless coverage, and nudging a boundary would either overlap or drop
audio between parts.

With captions switched off this mode needs no transcription at all, so splitting a
long video is near-instant and costs nothing in API calls.

### Hook-first mode

Was a checkbox, now a mode — because it is not a garnish on a clip, it changes what
a good clip *is*. Picking it fixes three things together:

- **40s – 1:00 clips.** A cold open plus the context that pays it off does not fit in
  less, so the length control is hidden rather than shown-but-overridden.
- **Gemini picks.** It reads the whole transcript in one pass; a hook chosen from a
  partial view is guesswork. Other models stay available for the other modes.
- **Complete thoughts only.** A clip must open a sentence and close one. Picks with no
  complete window inside the length bounds are dropped rather than cut mid-thought.

The clip's strongest line is spliced onto the front as a ~3s cold open, then the clip
plays in full.

> If a transcript has almost no punctuation, strict enforcement would return nothing —
> so it falls back to best-effort snapping and says so in the log. A job never returns
> zero clips because the rule was too strict.

### Podcast / interview mode

In a two-person show the meaningful unit is **a question and the answer it gets**. A
selector that picks free timestamps has no idea where an answer ends, so it stops
part-way into the next question and the clip trails off into a change of subject.
Sentence boundaries do not fix this — a new question *is* a new sentence.

The fix is structural rather than a better prompt:

1. Deepgram labels every word with a speaker (`diarize`).
2. Words become **turns**; short back-channels ("haan", "right") are absorbed rather
   than treated as turn changes that would chop an answer in half.
3. The host is identified by behaviour — asks more, says less per turn — not by talk
   time alone, which a chatty host would break.
4. **Candidates** are assembled whole: a host question plus the guest's complete
   answer, or a standalone guest point.
5. The model then **chooses among those candidates** instead of inventing timestamps.

That last step is the point. A wrong pick now costs one mediocre clip; it can no
longer produce a clip that stops mid-answer. Clips open on the question itself, not
on the "welcome back to the show" that preceded it.

Falls back to normal highlight selection when the transcript has no speaker labels
(no Deepgram key, or a solo video) — the mode never empties a job.

### Two-colour captions

With a host and guest colour set, each caption takes the colour of whoever is
speaking, so a viewer can tell the two apart at a glance. A single caption line never
mixes two speakers, and the colour is sampled at the cue's midpoint — sampling the
leading edge lands exactly on a turn boundary and colours every turn's first line as
the person who just *stopped* talking.

Podcast jobs slice the diarised full transcript for captions instead of
re-transcribing each clip, because a fresh per-clip call comes back with no speaker
labels at all.

### Sentence-accurate cutting

Some providers return the **entire video as one segment** — Deepgram nova-3 routinely
does, e.g. 150s of speech as a single entry with 689 words. Sentence snapping then had
no boundaries to snap to, and clips were cut wherever the clock landed, mid-sentence.

The punctuation is still there, inside the text. ShortsAI now re-cuts segments into
real sentences using the word-level timings, so every mode cuts on genuine boundaries.
On a typical Hindi clip that turns 1 segment into 43.

### Platform presets

A preset is a starting point, never a lock. Pick where you are posting and the shape,
length, captions and mode are already right; change anything and the choice flips to
**Custom**, so the label never claims settings you have since edited.

| Preset | Shape | Length | Mode | Captions |
|---|---|---|---|---|
| YouTube Shorts | 9:16 | 30–60s | Best moments | Bold, headline on |
| Instagram Reels | 9:16 | 40–60s | Hook first | Karaoke |
| Instagram Feed | 4:5 | 20–45s | Best moments | Outline, raised clear of IG's caption bar |

### Piksy watermark

Every clip carries the Piksy mark at **40% opacity** by default, so a reposted clip
shows where it was made. It is a default, not a lock:

- **Upload your own logo** and it replaces the Piksy mark — the two never stack.
- **Turn it off** with the toggle in *Headline & logo*, or `piksy_watermark: false`.
- Position, size and opacity use the same drag-and-drop controls as any logo.

A logo path that no longer exists on disk falls back to the Piksy mark rather than
rendering nothing.

### Drag-and-drop placement

In the live preview, drag the **caption**, **title** or **Part N** badge anywhere on
the 9:16 frame. The position you see is where it burns in: the UI stores the centre
point as a fraction of the frame and the renderer pins it there with the same
coordinates. **Reset** returns everything to the preset positions.

`english` = translation of the Hindi audio. `hinglish` = the Hindi romanised into Latin
letters. Everything is previewed live in the page before you render.

---

## How it works

1. **Select** (`select_clips.py`) — downloads the source, transcribes it, asks the LLM
   for the best moments (guided by your brief), snaps each to sentence boundaries, and
   writes `clips_manifest.json`.
2. **Burn** (`burn_subtitles.py`) — per clip: transcribes it, runs only the language
   pass your captions need, then a single ffmpeg pass that seeks into the source,
   scales to 1080×1920 and burns the captions.

Options travel: **web page → `app.py` → `select_clips.py` → manifest → `burn_subtitles.py`.**

`providers.py` holds every fallback chain — add a provider there and the whole pipeline
picks it up.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Web UI |
| GET | `/api/status` | Provider/key/ffmpeg readiness |
| POST | `/api/settings` | Save API keys (localhost only) |
| POST | `/process/url` · `/process/upload` | Full pipeline |
| POST | `/select-clips/url` · `/select-clips/upload` | Selection only |
| POST | `/burn-subtitles/{job_id}` | Burn an already-selected job |
| GET | `/jobs/{job_id}` | Job status |
| GET | `/jobs/{job_id}/clips` · `/clips.zip` · `/clips/{filename}` | Results |
| GET | `/jobs/{job_id}/highlights` | Selected segments |
| DELETE | `/jobs/{job_id}` | Remove a job (`?keep_files=true` to keep files) |
| GET | `/health` | Health check |

Example `options` payload:

```json
{
  "clip_prompt": "Only moments about failure and bouncing back",
  "num_clips": "auto",
  "clip_mode": "multi",
  "burn_subtitles": true,
  "subtitle_layout": "single",
  "subtitle_language": "hinglish",
  "subtitle_position": "bottom",
  "caption_style": "karaoke",
  "caption_accent": "#FFE600"
}
```

Sequential parts, with free overlay placement:

```json
{
  "clip_mode": "sequential",
  "chunk_len": 30,
  "series_title": "Motivation Series",
  "show_part_label": true,
  "burn_subtitles": false,
  "part_xy":    {"x": 0.22, "y": 0.06},
  "title_xy":   {"x": 0.50, "y": 0.88},
  "caption_xy": {"x": 0.50, "y": 0.42}
}
```

`*_xy` are fractions of the 1080×1920 frame (`{"x":0.5,"y":0.5}` = dead centre) and
mark the **centre** of the overlay. Omit them, or send `null`, to use the preset
position. Out-of-range or malformed values fall back to the preset rather than
failing the render.

---

## Caption CLI (optional)

Re-render a job's clips without re-selecting:

```bash
python burn_subtitles.py output/<job_id> --style karaoke --accent "#2EE640"
python burn_subtitles.py output/<job_id> --layout dual --title
python burn_subtitles.py output/<job_id> --no-burn        # clean clips, no captions
```

Preview caption styles on any local clip without API keys or the server:

```bash
python test_captions.py myclip.mp4 --styles karaoke,word_pop
```

Writes a labelled contact sheet to `caption_preview/` so you can compare looks at a glance.

---

## Private / age-restricted videos

Some YouTube links need your session cookies. Export them with a
"Get cookies.txt" browser extension and save the file as `cookies.txt` in the project
folder — it's picked up automatically and is git-ignored.

**Treat that file like a password**: it grants access to your YouTube account. Never
commit or share it.

---

## Troubleshooting

- **Hindi shows as boxes** → no Devanagari font. Re-run `python3 run.py` to fetch fonts.
- **"Sign in to confirm you're not a bot"** → add a fresh `cookies.txt` (above).
- **Everything rate-limited** → add a Gemini or OpenRouter key in Setup; they take over
  automatically.
- **Captions came out blank** → the translation provider was down; the job falls back to
  Hindi captions. Check `SUBTITLE_DIAGNOSTIC_REPORT.txt` in the job folder.
- **Per-job logs** → every job writes `DIAGNOSTIC_REPORT.txt` and
  `SUBTITLE_DIAGNOSTIC_REPORT.txt` into `output/<job_id>/`.

---

## Notes

- The pipeline assumes **Hindi source audio**. English is a translation of it;
  Hinglish is a romanisation of it.
- Clips and downloads are kept until you delete a job from the page — nothing is
  cleaned up behind your back.
