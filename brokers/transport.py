"""Zero-dependency REST transport for venue adapters.

Uses only `urllib.request` from the stdlib, so the project keeps its
no-third-party-packages guarantee while still being able to talk to a real
exchange over HTTPS.

What this layer is responsible for, and nothing else:

  * HTTPS requests with a hard scheme check (an `http://` base URL for a
    credentialed request is refused, not silently downgraded),
  * auth styles: bearer, api-key header, api-key query, HMAC signing,
  * retries with exponential backoff **only** for retryable failures,
  * `Retry-After` / rate-limit handling,
  * typed errors via `BrokerError` subclasses,
  * redacted logging -- credentials and signed payloads never hit a log.

Venue *semantics* (endpoint paths, field names, status dialects) live in
`brokers.profiles`; this module knows nothing about any venue.
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import (BrokerAuthError, BrokerRateLimitError, BrokerUnavailableError,
                   BrokerValidationError, BrokerError)

USER_AGENT = "MafutaniCTA-Broker/1.0 (zero-dep)"

# How many times to retry, and the backoff schedule.
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5
DEFAULT_TIMEOUT = 15.0

# Refuse to put credentials on a plaintext connection.
_ALLOWED_SCHEMES = ("https",)


class Response:
    __slots__ = ("status", "headers", "body", "text", "url")

    def __init__(self, status, headers, body, text, url):
        self.status = status
        self.headers = headers
        self.body = body
        self.text = text
        self.url = url

    def json(self):
        """Parsed JSON body, or {} when the body is empty/not JSON."""
        if self.body is None:
            return {}
        return self.body


def _redact(text):
    try:
        from core.logsafe import scrub
        return scrub(text)
    except Exception:
        return "<unloggable>"


class Transport:
    """Performs HTTP for a venue. Injectable so tests never touch network."""

    def __init__(self, timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES,
                 backoff=DEFAULT_BACKOFF, logger=None, sleep=time.sleep):
        self.timeout = float(timeout)
        self.retries = int(retries)
        self.backoff = float(backoff)
        self.logger = logger
        # injectable for deterministic tests
        self._sleep = sleep

    # -- public ---------------------------------------------------------
    def request(self, method, url, params=None, body=None, headers=None,
                auth=None, sign=None):
        """Perform one logical request, with retries.

        `auth`  -- callable(headers, query) -> None, mutates in place.
        `sign`  -- callable(method, path, query, body, timestamp) -> dict
                   of extra headers (e.g. HMAC signature).
        """
        query = dict(params or {})
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update({str(k): str(v) for k, v in headers.items()})
        if sign is not None:
            ts = str(int(time.time() * 1000))
            hdrs.update(sign(method, url, query, body, ts))
        if auth is not None:
            auth(hdrs, query)

        full = self._build_url(url, query)
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")

        last_error = None
        for attempt in range(self.retries + 1):
            try:
                resp = self._once(method, full, payload, hdrs)
                self._log("{} {} -> {}".format(method, _redact(full), resp.status))
                return resp
            except BrokerRateLimitError as e:
                # Not retried blindly: a throttled venue needs the wait.
                last_error = e
                if attempt >= self.retries:
                    break
                self._wait(max(e.retry_after, self.backoff * (2 ** attempt)))
            except BrokerUnavailableError as e:
                last_error = e
                if attempt >= self.retries:
                    break
                self._wait(self.backoff * (2 ** attempt))
            except (BrokerAuthError, BrokerValidationError):
                # Never retried: retrying bad credentials is how accounts
                # get locked out, and retrying bad input is pointless.
                raise
        if last_error is not None:
            raise last_error
        raise BrokerUnavailableError("request failed: no attempt succeeded")

    def get(self, url, **kw):
        return self.request("GET", url, **kw)

    def post(self, url, **kw):
        return self.request("POST", url, **kw)

    def delete(self, url, **kw):
        return self.request("DELETE", url, **kw)

    # -- internals ------------------------------------------------------
    def _build_url(self, url, query):
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "").lower()
        if not scheme:
            raise BrokerValidationError("base_url must be absolute")
        if scheme not in _ALLOWED_SCHEMES:
            raise BrokerValidationError(
                "refusing to use scheme '{}': credentials require https"
                .format(scheme))
        if not parts.netloc:
            raise BrokerValidationError("base_url missing host")
        qs = urllib.parse.urlencode(query, doseq=True) if query else ""
        path = parts.path or "/"
        if parts.query:
            qs = (qs + "&" + parts.query) if qs else parts.query
        return urllib.parse.urlunsplit(
            (scheme, parts.netloc, path, qs, ""))

    def _once(self, method, url, payload, headers):
        req = urllib.request.Request(url, data=payload, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as raw:
                return self._parse(raw.getcode(), raw.headers, raw.read(), url)
        except urllib.error.HTTPError as e:
            body_bytes = b""
            try:
                body_bytes = e.read()
            except Exception:
                pass
            return self._parse(e.code, e.headers, body_bytes, url, http_error=True)
        except urllib.error.URLError as e:
            raise BrokerUnavailableError(
                "cannot reach {}: {}".format(urllib.parse.urlsplit(url).netloc,
                                             e.reason))
        except (TimeoutError, OSError) as e:
            raise BrokerUnavailableError("transport error: {}".format(e))

    def _parse(self, status, headers, raw, url, http_error=False):
        text = ""
        parsed = None
        if raw:
            try:
                text = raw.decode("utf-8", "replace")
            except Exception:
                text = ""
            try:
                parsed = json.loads(text)
            except Exception:
                parsed = None
        hdrs = {k.lower(): v for k, v in dict(headers or {}).items()}

        if 200 <= status < 300:
            return Response(status, hdrs, parsed, text, url)

        # ---- error mapping ------------------------------------------
        retry_after = 0.0
        ra = hdrs.get("retry-after")
        if ra:
            try:
                retry_after = float(ra)
            except ValueError:
                retry_after = 0.0
        detail = _extract_error(parsed, text)

        if status == 401 or status == 403:
            # 403 from a trading venue is ambiguous (auth vs. scope), so
            # it maps to auth either way -- both need operator action.
            raise BrokerAuthError(
                "{} rejected credentials ({}){}".format(
                    url, status, ": " + detail if detail else ""),
                status=status)
        if status == 429:
            raise BrokerRateLimitError(
                "{} rate limited{}".format(url, ": " + detail if detail else ""),
                status=status, retry_after=retry_after)
        if status == 400 or status == 422:
            raise BrokerValidationError(
                "{} bad request ({}){}".format(
                    url, status, ": " + detail if detail else ""), status=status)
        if status == 404:
            raise BrokerError("{} not found ({}){}".format(
                url, status, ": " + detail if detail else ""), status=status)
        if status >= 500:
            raise BrokerUnavailableError(
                "{} server error ({}){}".format(
                    url, status, ": " + detail if detail else ""),
                status=status, retryable=True)
        raise BrokerError("{} unexpected status {}{}".format(
            url, status, ": " + detail if detail else ""), status=status)

    def _wait(self, seconds):
        try:
            self._sleep(min(float(seconds), 30.0))
        except Exception:
            pass

    def _log(self, message):
        if self.logger is None:
            return
        try:
            self.logger(_redact(message))
        except Exception:
            pass


def _extract_error(parsed, text):
    """Pull a human-usable reason out of a venue's error body."""
    if isinstance(parsed, dict):
        for key in ("error", "error_message", "message", "msg", "reason",
                    "detail", "errors"):
            if key in parsed:
                val = parsed[key]
                if isinstance(val, (list, tuple)):
                    val = ", ".join(str(v) for v in val)
                return str(val)[:300]
    if isinstance(parsed, str) and parsed:
        return parsed[:300]
    if not parsed and text:
        return text[:300]
    return ""


# ------------------------------------------------------------------ auth styles
def bearer_auth(credentials):
    """`Authorization: Bearer <token>`."""
    token = credentials.get("api_key") if credentials else ""

    def _apply(headers, query):
        if not token:
            raise BrokerAuthError("bearer auth requires an API key/token")
        headers["Authorization"] = "Bearer " + str(token)
    return _apply


def header_auth(credentials, key="X-API-Key", value_field="api_key",
                extra=None):
    """`X-API-Key: <key>` plus optional secondary headers (secret, etc.)."""
    def _apply(headers, query):
        if not credentials:
            raise BrokerAuthError("header auth requires credentials")
        headers[key] = str(credentials.get(value_field))
        for hk, field in (extra or {}).items():
            headers[hk] = str(credentials.get(field))
    return _apply


def query_auth(credentials, key="api_key", value_field="api_key",
               secret_key="api_secret", secret_field="secret"):
    """Credentials in the query string.

    Discouraged: query strings land in proxy logs. Supported because some
    venues offer nothing else. `core.logsafe` masks them in our logs.
    """
    def _apply(headers, query):
        if not credentials:
            raise BrokerAuthError("query auth requires credentials")
        query[key] = str(credentials.get(value_field))
        sec = credentials.get(secret_field)
        if sec:
            query[secret_key] = str(sec)
    return _apply


def hmac_signer(credentials, secret_field="secret"):
    """HMAC-SHA256 of `timestamp + method + path + body`.

    The classic exchange signing scheme. Returns a headers dict carrying
    the key, timestamp and signature.
    """
    import base64
    import hashlib
    import hmac

    def _sign(method, url, query, body, timestamp):
        if not credentials:
            raise BrokerAuthError("hmac signing requires credentials")
        parts = urllib.parse.urlsplit(url)
        path = parts.path or "/"
        qs = urllib.parse.urlencode(sorted((query or {}).items()))
        payload = "{}{}{}{}".format(timestamp, str(method).upper(), path,
                                   body if body is not None else "")
        if qs:
            payload += "?" + qs
        secret = str(credentials.get(secret_field) or "")
        digest = hmac.new(secret.encode(), payload.encode(),
                          hashlib.sha256).digest()
        sig = base64.b64encode(digest).decode()
        return {
            "X-API-KEY": str(credentials.get("api_key")),
            "X-TIMESTAMP": timestamp,
            "X-SIGNATURE": sig,
        }
    return _sign
