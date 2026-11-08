"""Boot a real app server for the HTTP tests, so they stop skipping.

The problem this fixes
----------------------
Sixteen tests across `test_api_candles_tf.py`, `test_chart_context.py`,
`test_chart_context_resolution.py` and `test_chart_position.py` are decorated
`@unittest.skipUnless(server_is_up(), ...)`. With no server running they are
skipped -- silently, in every ordinary `pytest tests/` invocation. They are
the only tests that drive the router over real HTTP, which is precisely the
layer where the bugs they were written for lived (`tf=1m` being ignored,
`tf` reaching SQL, chart context resolution). A guard that quietly disables the
only end-to-end coverage is not a guard.

So the server becomes a fixture. If something already answers on
`MAFUTANI_TEST_BASE` (default `127.0.0.1:8000`) that one is used, so a
developer running `python3 app.py` still tests their own process. Otherwise a
server is started on an ephemeral port for the duration of the session.

Why the database is copied
---------------------------
These tests only issue GETs, but booting `app.py` starts its background feed
loop, which writes `quotes_l1`. Pointing that at `data/trading.db` would have
the suite mutate the live database -- the exact failure `tests/__init__.py`
exists to prevent. So the live file is copied to a temp path and the server
runs against the copy: real market data in, no writes out.
"""
import os
import shutil
import socket
import sys
import tempfile
import threading
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SERVER = {}
_LOCK = threading.Lock()


def _free_port():
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _answers(base, timeout=1.0):
    try:
        with urllib.request.urlopen(base + "/api/status", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def live_db_copy():
    """A writable copy of the live database, created once per session.

    Falls back to an empty database when the live file is absent, so a fresh
    clone still gets a running server; the tests that need data assert on
    what they asked for rather than silently passing on an empty set.
    """
    with _LOCK:
        if _SERVER.get("db"):
            return _SERVER["db"]
    live = os.path.join(_ROOT, "data", "trading.db")
    fd, path = tempfile.mkstemp(prefix="mafutani-liveserver-", suffix=".db")
    os.close(fd)
    if os.path.exists(live):
        shutil.copyfile(live, path)
    else:
        try:
            from data.db import init_db
            init_db(path).close()
        except Exception:
            pass
    with _LOCK:
        _SERVER["db"] = path
    return path


def ensure_live_server():
    """Return a base URL with a working app server behind it."""
    configured = os.environ.get("MAFUTANI_TEST_BASE")
    if configured:
        return configured.rstrip("/")

    with _LOCK:
        if _SERVER.get("base"):
            return _SERVER["base"]
        default = "http://127.0.0.1:8000"
        if _answers(default):
            _SERVER["base"] = default
            return default

        db_path = live_db_copy()
        # The child server reads DB_PATH at import time, so it has to be set
        # before `app` is imported, not just before the handler runs.
        os.environ["DB_PATH"] = db_path
        if _ROOT not in sys.path:
            sys.path.insert(0, _ROOT)
        import app as app_module

        server = app_module.ThreadingHTTPServer(("127.0.0.1", 0),
                                                app_module.Handler)
        port = server.server_address[1]
        base = "http://127.0.0.1:%d" % port
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        for _ in range(50):
            if _answers(base, timeout=0.5):
                break
            threading.Event().wait(0.1)
        else:
            server.shutdown()
            raise RuntimeError("test app server did not come up on %s" % base)
        _SERVER["base"] = base
        _SERVER["server"] = server
        return base
