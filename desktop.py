"""
desktop.py — Piksy as a desktop application.

What this changes, and what it does NOT
---------------------------------------
It changes the WINDOW, not the work. All the processing — ffmpeg, transcription,
clip selection — already runs as Python on this machine; the browser was only ever
a viewport onto localhost. This replaces that viewport with a native window that
has its own taskbar entry, its own icon, and no address bar, and quits the server
when you close it.

Backends
--------
pywebview needs a platform webview:
  Windows  WebView2 (part of Edge; present on Win11 and most Win10)
  macOS    WKWebView via pyobjc
  Linux    PyGObject + WebKit2GTK, or Qt

On Linux the GTK bindings are a SYSTEM package, which a virtualenv cannot see by
default — the venv has its own site-packages and the distro's `gi` lives outside
it. Rather than demand `--system-site-packages` at venv creation (too late for
anyone who already installed), this bridges the matching system path at runtime.

If no backend can be found at all, this falls back to opening a browser rather
than failing. A missing GUI toolkit should cost you the window, not the app.
"""

import os
import sys
import time
import socket
import threading
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ICON = os.path.join(HERE, "assets", "piksy.ico")
ICON_PNG = os.path.join(HERE, "assets", "piksy.png")

WINDOW_TITLE = "Piksy — AI Video Clips"
MIN_SIZE = (980, 680)
DEFAULT_SIZE = (1360, 900)


# ─────────────────────────────────────────────────────────────
# Backend discovery
# ─────────────────────────────────────────────────────────────

def _bridge_system_site_packages():
    """Let a virtualenv see the distro's PyGObject.

    Only paths for the SAME interpreter version are added, so the C extensions
    are ABI-compatible. Returns True if a path was added.
    """
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    added = False
    for base in ("/usr/lib64", "/usr/lib"):
        path = os.path.join(base, ver, "site-packages")
        if os.path.isdir(path) and path not in sys.path:
            sys.path.append(path)
            added = True
    return added


def _ensure_linux_backend_path():
    """Make the distro's PyGObject visible, once, at import time.

    Doing this inside find_backend() alone was too late for any caller that
    imports webview directly — the bridge has to happen before the first
    `webview.start()`, wherever that comes from. Only bridges when `gi` is
    genuinely missing, and APPENDS so venv packages still win.
    """
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return
    try:
        import gi  # noqa: F401
        return                      # already visible; leave sys.path alone
    except Exception:
        _bridge_system_site_packages()


_ensure_linux_backend_path()


def find_backend():
    """Return (ok, name, hint). Never raises."""
    try:
        import webview  # noqa: F401
    except Exception:
        return False, "", ("pywebview is not installed. "
                           "Install it with:  pip install pywebview")

    if sys.platform.startswith("win"):
        try:
            import webview.platforms.edgechromium  # noqa: F401
            return True, "WebView2 (Edge)", ""
        except Exception:
            return False, "", ("Windows needs the WebView2 runtime (it ships with "
                               "Edge on Windows 10/11). Install it from "
                               "https://developer.microsoft.com/microsoft-edge/webview2/")

    if sys.platform == "darwin":
        try:
            import webview.platforms.cocoa  # noqa: F401
            return True, "WKWebView", ""
        except Exception:
            return False, "", "macOS needs pyobjc:  pip install pyobjc"

    # Linux: GTK first (what distros ship), then Qt.
    for attempt in (0, 1):
        try:
            import webview.platforms.gtk  # noqa: F401
            return True, "WebKit2GTK", ""
        except Exception:
            if attempt == 0 and _bridge_system_site_packages():
                continue        # retry once, now that the system path is visible
            break
    try:
        import webview.platforms.qt  # noqa: F401
        return True, "Qt WebEngine", ""
    except Exception:
        pass
    return False, "", (
        "No desktop webview found. Install ONE of:\n"
        "    Fedora        sudo dnf install python3-gobject webkit2gtk4.1\n"
        "    Debian/Ubuntu sudo apt install python3-gi gir1.2-webkit2-4.1\n"
        "    Any distro    pip install PySide6")


# ─────────────────────────────────────────────────────────────
# Server
# ─────────────────────────────────────────────────────────────

def _port_is_free(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) != 0


def pick_port(start=8000, span=25):
    for p in range(start, start + span):
        if _port_is_free(p):
            return p
    return start


def wait_until_up(port, host="127.0.0.1", timeout=40.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            if s.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.2)
    return False


def start_server(port, host="127.0.0.1"):
    """Run uvicorn in this process, on a daemon thread.

    In-process rather than a subprocess so closing the window really does end the
    server — an orphaned uvicorn holding the port is exactly the bug that makes a
    desktop app feel broken on second launch.
    """
    import uvicorn
    import app as app_module

    config = uvicorn.Config(app_module.app, host=host, port=port,
                            log_level="warning", access_log=False)
    server = uvicorn.Server(config)

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    return server


# ─────────────────────────────────────────────────────────────
# Window
# ─────────────────────────────────────────────────────────────

class Bridge:
    """Methods the page can call as window.pywebview.api.*

    Only what a browser genuinely cannot do belongs here — a native file picker
    (no upload copy, no 2GB POST) and revealing a finished clip in the file
    manager. Everything else stays plain HTTP so the same UI works in a browser.
    """

    def pick_video(self):
        import webview
        types = ("Video (*.mp4;*.mkv;*.mov;*.avi;*.webm;*.m4v)", "All files (*.*)")
        try:
            res = webview.windows[0].create_file_dialog(
                webview.OPEN_DIALOG, allow_multiple=False, file_types=types)
        except Exception:
            return ""
        if not res:
            return ""
        return res[0] if isinstance(res, (list, tuple)) else str(res)

    def reveal(self, path):
        """Show a file in the OS file manager."""
        path = os.path.abspath(path or "")
        if not os.path.exists(path):
            return False
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", "/select,", path])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
            return True
        except Exception:
            return False

    def is_desktop(self):
        return True


def launch(port=None, host="127.0.0.1", debug=False):
    """Start the server and open the native window. Returns an exit code."""
    ok, backend, hint = find_backend()
    if not ok:
        print("! No desktop window available.")
        for line in (hint or "").splitlines():
            print("  " + line)
        print("\n  Falling back to your browser — the app still works exactly the same.")
        return run_in_browser(port, host)

    import webview

    port = port or pick_port()
    print(f"• Starting Piksy  (window: {backend})")
    start_server(port, host)
    if not wait_until_up(port, host):
        print("✗ The server did not start. Run 'python3 run.py' to see the error.")
        return 1

    url = f"http://{host}:{port}"
    window = webview.create_window(
        WINDOW_TITLE, url,
        width=DEFAULT_SIZE[0], height=DEFAULT_SIZE[1],
        min_size=MIN_SIZE, js_api=Bridge(),
        background_color="#0B0B0C",     # matches the UI, so no white flash on open
    )
    # gui=None lets pywebview choose; it has already been proven importable above.
    webview.start(debug=debug, private_mode=False)
    print("• Window closed — Piksy has stopped.")
    return 0


def run_in_browser(port=None, host="127.0.0.1"):
    """Fallback: same server, opened in the default browser."""
    import webbrowser
    port = port or pick_port()
    start_server(port, host)
    if not wait_until_up(port, host):
        print("✗ The server did not start.")
        return 1
    url = f"http://{host}:{port}"
    print(f"• Piksy is running at {url}")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print("  Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n• Stopped.")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Run Piksy as a desktop application.")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--debug", action="store_true", help="open the webview inspector")
    ap.add_argument("--browser", action="store_true", help="force browser mode")
    a = ap.parse_args()
    os.chdir(HERE)
    sys.exit(run_in_browser(a.port, a.host) if a.browser
             else launch(a.port, a.host, a.debug))
