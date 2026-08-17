"""
joblog.py — the short version of what happened.

DIAGNOSTIC_REPORT.txt records everything, which is what you want when debugging a
render and useless when you just want to know why a job went wrong. This writes the
other file: one line per step, one line per AI call, and a verdict.

    logs/shortsailogs/2026-08-17_164002_73bfbe84_select.log

Design notes
------------
- A module-level "current run" rather than threading a log object through every
  call: providers.py is called from selection, from the burn stage, and from inside
  the parallel render workers, and passing a handle down all of those would touch
  every signature for no benefit.
- Thread-safe: the render stage runs clips in parallel, so events arrive from
  several threads at once.
- NOTHING here may break a job. Every entry point is wrapped — a logging bug must
  not cost a render.
"""

import os
import threading
import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs", "shortsailogs")

_LOCK = threading.Lock()
_CURRENT = None


class RunLog:
    """Collects steps and AI calls for one run, then writes them as a flat report."""

    def __init__(self, job_id, kind="run", source=""):
        self.job_id = str(job_id or "unknown")
        self.kind = kind
        self.source = source
        self.started = datetime.datetime.now()
        self.steps = []      # (name, status, seconds, detail)
        self.calls = []      # (stage, provider, model, status, detail)
        self.facts = []      # (label, value)
        self.result = ""
        self._open = {}      # name -> start time

    # ── steps ──────────────────────────────────────────────────────────────
    def begin(self, name):
        self._open[name] = datetime.datetime.now()

    def end(self, name, ok=True, detail=""):
        t0 = self._open.pop(name, None)
        secs = (datetime.datetime.now() - t0).total_seconds() if t0 else 0.0
        with _LOCK:
            self.steps.append((name, "OK" if ok else "FAIL", secs, detail))

    def step(self, name, ok=True, detail="", secs=0.0):
        """Record an already-finished step."""
        with _LOCK:
            self.steps.append((name, "OK" if ok else "FAIL", secs, detail))

    def skip(self, name, why=""):
        with _LOCK:
            self.steps.append((name, "SKIP", 0.0, why))

    # ── AI calls ───────────────────────────────────────────────────────────
    def ai(self, stage, provider, model="", ok=True, detail=""):
        with _LOCK:
            self.calls.append((stage, provider, model or "", "OK" if ok else "FAIL", detail))

    def fact(self, label, value):
        with _LOCK:
            self.facts.append((label, str(value)))

    # ── output ─────────────────────────────────────────────────────────────
    def _body(self):
        end = datetime.datetime.now()
        total = (end - self.started).total_seconds()
        w = max([len(s[0]) for s in self.steps] + [22])

        L = []
        L.append("=" * 72)
        L.append(f"  PIKSY RUN LOG — {self.kind}")
        L.append("=" * 72)
        L.append(f"  job     : {self.job_id}")
        L.append(f"  started : {self.started:%Y-%m-%d %H:%M:%S}")
        L.append(f"  took    : {total:.1f}s")
        if self.source:
            L.append(f"  source  : {self.source}")
        for k, v in self.facts:
            L.append(f"  {k:<8}: {v}")

        L.append("")
        L.append("  STEPS")
        L.append("  " + "-" * 70)
        if not self.steps:
            L.append("    (none recorded)")
        for name, status, secs, detail in self.steps:
            mark = {"OK": "OK  ", "FAIL": "FAIL", "SKIP": "SKIP"}.get(status, status)
            line = f"    [{mark}] {secs:6.1f}s  {name:<{w}}"
            if detail:
                line += f"  {detail}"
            L.append(line.rstrip())

        L.append("")
        L.append("  AI CALLS   (which model was asked, and what it said)")
        L.append("  " + "-" * 70)
        if not self.calls:
            L.append("    (no AI calls — nothing needed one)")
        for stage, provider, model, status, detail in self.calls:
            who = f"{provider}" + (f" {model}" if model else "")
            line = f"    [{status:<4}] {stage:<11} {who:<34}"
            if detail:
                line += f"  {detail}"
            L.append(line.rstrip())

        failed = [s for s in self.steps if s[1] == "FAIL"]
        bad_calls = [c for c in self.calls if c[3] == "FAIL"]
        L.append("")
        L.append("  " + "-" * 70)
        if self.result:
            L.append(f"  RESULT  : {self.result}")
        L.append(f"  FAILURES: {len(failed)} step(s), {len(bad_calls)} model call(s)")
        if bad_calls:
            L.append("  Models that failed (the job may still have recovered via a fallback):")
            for stage, provider, model, _s, detail in bad_calls:
                L.append(f"    - {provider} {model} during {stage}: {detail}")
        L.append("=" * 72)
        return "\n".join(L) + "\n"

    def write(self):
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            name = (f"{self.started:%Y-%m-%d_%H%M%S}_"
                    f"{self.job_id[:8]}_{self.kind}.log")
            path = os.path.join(LOG_DIR, name)
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._body())
            return path
        except Exception:
            return None


# ── module-level current run ───────────────────────────────────────────────

def start(job_id, kind="run", source=""):
    global _CURRENT
    try:
        _CURRENT = RunLog(job_id, kind, source)
    except Exception:
        _CURRENT = None
    return _CURRENT


def current():
    return _CURRENT


def finish(result=""):
    """Write the current run's log and clear it. Returns the path, or None."""
    global _CURRENT
    run = _CURRENT
    _CURRENT = None
    if run is None:
        return None
    try:
        if result:
            run.result = result
        return run.write()
    except Exception:
        return None


# Convenience wrappers so callers never have to null-check.
def _safe(fn):
    def inner(*a, **kw):
        run = _CURRENT
        if run is None:
            return None
        try:
            return getattr(run, fn)(*a, **kw)
        except Exception:
            return None
    return inner


begin = _safe("begin")
end = _safe("end")
step = _safe("step")
skip = _safe("skip")
ai = _safe("ai")
fact = _safe("fact")
