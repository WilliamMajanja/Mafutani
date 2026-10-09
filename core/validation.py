"""HTTP validation/JSON helpers for BaseHTTPRequestHandler. Stdlib only.

Functions use duck-typing on `handler` (no http.server import) to stay
import-safe for core/.
"""
import json


def send_json(handler, obj, status=200, extra_headers=None):
    body = json.dumps(obj).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    for k, v in (extra_headers or {}).items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


def bad_request(handler, msg="bad request", status=400):
    return send_json(handler, {"error": str(msg)}, status=status)


def unauthorized(handler, msg="unauthorized"):
    return send_json(handler, {"error": str(msg)}, status=401)


def forbidden(handler, msg="forbidden"):
    return send_json(handler, {"error": str(msg)}, status=403)


def get_json_body(handler, max_bytes=1_000_000):
    """Parse JSON body. Returns (data, error). data None on error/empty."""
    try:
        length = int(handler.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        length = 0
    if length <= 0:
        return None, "empty body"
    if length > max_bytes:
        return None, "body too large"
    try:
        raw = handler.rfile.read(length)
    except Exception as e:
        return None, "read error: {}".format(e)
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    except Exception:
        return None, "invalid encoding"
    try:
        data = json.loads(text) if text.strip() else None
    except Exception:
        return None, "invalid json"
    if data is None:
        return None, "empty body"
    if not isinstance(data, dict):
        return None, "json body must be an object"
    return data, None


def require_json(handler, *fields):
    """Parse body and require fields. Sends 400 on failure, returns dict|None."""
    data, err = get_json_body(handler)
    if err:
        bad_request(handler, err)
        return None
    missing = [f for f in fields if f not in data or data[f] in (None, "")]
    if missing:
        bad_request(handler, "missing fields: {}".format(", ".join(missing)))
        return None
    return data
