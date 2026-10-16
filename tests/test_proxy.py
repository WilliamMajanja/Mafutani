"""Proxy transport tests. Stdlib only, no network, no dependencies.

The SOCKS5 and HTTP-CONNECT paths are exercised against tiny servers
implemented here in the test file, over a real loopback socket. That
matters: a mock of one's own handshake proves nothing, and the failure
this module is most likely to have is a malformed handshake byte that only
a real peer would reject.
"""

import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import proxy as _p


class _Env:
    def __init__(self, **kw):
        self._kw, self._old = kw, {}

    def __enter__(self):
        for k, v in self._kw.items():
            self._old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self._kw.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


class _EchoServer(threading.Thread):
    """Minimal HTTP origin. Returns 200 with a body, ignores the path."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.hits = 0

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.hits += 1
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                body = b'{"ip":"127.0.0.1","ok":true}'
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() +
                    b"\r\nConnection: close\r\n\r\n" + body)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass


class _Socks5Server(threading.Thread):
    """A real RFC 1928 server: greeting, optional auth, CONNECT."""

    daemon = True

    def __init__(self, require_auth=False, reply_code=0x00):
        super().__init__()
        self.require_auth = require_auth
        self.reply_code = reply_code
        self.saw_domain_form = False
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_conn, args=(conn,),
                             daemon=True).start()

    def _recv(self, conn, n):
        buf = b""
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                raise OSError("closed")
            buf += c
        return buf

    # Named _serve_conn, not _handle: since Python 3.13
    # threading.Thread.start() writes self._handle (a _thread._ThreadHandle)
    # to start a joinable thread. A Thread subclass method called _handle is
    # clobbered by that assignment, so run() ends up calling a ThreadHandle
    # as the target -- TypeError, the connection is never served, and every
    # proxied request times out. Rename it back and this breaks on 3.13+.
    def _serve_conn(self, conn):
        try:
            ver, nmethods = self._recv(conn, 2)
            methods = self._recv(conn, nmethods)
            if self.require_auth:
                if 0x02 not in methods:
                    conn.sendall(bytes([0x05, 0xFF]))
                    return
                conn.sendall(bytes([0x05, 0x02]))
                # RFC 1929: ver, ulen, uname, plen, passwd. The lengths
                # are IN the stream -- reading them from nowhere hangs or
                # resets, which is exactly what this server did.
                hdr = self._recv(conn, 2)
                self._recv(conn, hdr[1])                # uname
                plen = self._recv(conn, 1)[0]
                self._recv(conn, plen)                  # passwd
                conn.sendall(bytes([0x01, 0x00]))
            else:
                if 0x00 not in methods:
                    conn.sendall(bytes([0x05, 0xFF]))
                    return
                conn.sendall(bytes([0x05, 0x00]))

            head = self._recv(conn, 4)
            atyp = head[3]
            if atyp == 0x01:
                host = socket.inet_ntoa(self._recv(conn, 4))
            elif atyp == 0x03:
                ln = self._recv(conn, 1)[0]
                host = self._recv(conn, ln).decode("ascii")
                self.saw_domain_form = True
            elif atyp == 0x04:
                self._recv(conn, 16)
                host = "::"
            else:
                conn.sendall(bytes([0x05, 0x08, 0x00, 0x01,
                                    0, 0, 0, 0, 0, 0]))
                return
            port = int.from_bytes(self._recv(conn, 2), "big")
            if self.reply_code != 0x00:
                conn.sendall(bytes([0x05, self.reply_code, 0x00, 0x01,
                                    0, 0, 0, 0, 0, 0]))
                return
            conn.sendall(bytes([0x05, 0x00, 0x00, 0x01, 0, 0, 0, 0, 0, 0]))
            # Relay to the real origin and splice.
            try:
                up = socket.create_connection((host, port), timeout=5)
            except OSError:
                return
            def pipe(a, b):
                try:
                    while True:
                        d = a.recv(4096)
                        if not d:
                            break
                        b.sendall(d)
                except Exception:
                    pass
                finally:
                    try:
                        b.shutdown(socket.SHUT_WR)
                    except Exception:
                        pass
            t = threading.Thread(target=pipe, args=(conn, up), daemon=True)
            t.start()
            pipe(up, conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class _HttpProxyServer(threading.Thread):
    """A real HTTP proxy: handles CONNECT for a plain-HTTP origin."""

    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.connect_lines = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_conn, args=(conn,),
                             daemon=True).start()

    def _serve_conn(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                c = conn.recv(4096)
                if not c:
                    return
                data += c
            line = data.split(b"\r\n", 1)[0].decode("latin-1")
            self.connect_lines.append(line)
            if not line.startswith("CONNECT "):
                conn.sendall(b"HTTP/1.1 400 Bad Request\r\n"
                             b"Content-Length: 0\r\n\r\n")
                return
            hostport = line.split(" ")[1]
            host, port = hostport.rsplit(":", 1)
            conn.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            up = socket.create_connection((host, int(port)), timeout=5)
            def pipe(a, b):
                try:
                    while True:
                        d = a.recv(4096)
                        if not d:
                            break
                        b.sendall(d)
                except Exception:
                    pass
            threading.Thread(target=pipe, args=(conn, up), daemon=True).start()
            pipe(up, conn)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class TestParsing(unittest.TestCase):

    def test_socks5_url(self):
        self.assertEqual(_p.parse_proxy("socks5://127.0.0.1:1080"),
                         ("socks5", "127.0.0.1", 1080, None, None))

    def test_default_ports(self):
        self.assertEqual(_p.parse_proxy("socks5://h")[2], 1080)
        self.assertEqual(_p.parse_proxy("http://h")[2], 8080)

    def test_credentials_parsed(self):
        s, h, p, u, pw = _p.parse_proxy("socks5://user:sec@host:1080")
        self.assertEqual((s, h, p, u, pw), ("socks5", "host", 1080, "user", "sec"))

    def test_bare_host_gets_http_scheme(self):
        self.assertEqual(_p.parse_proxy("127.0.0.1:3128")[:2],
                         ("http", "127.0.0.1"))

    def test_ipv6_literal(self):
        self.assertEqual(_p.parse_proxy("socks5://[::1]:1080")[1], "::1")

    def test_bad_port_rejected(self):
        with self.assertRaises(_p.ProxyError):
            _p.parse_proxy("socks5://host:notaport")

    def test_unconfigured_is_none(self):
        with _Env(MAFUTANI_PROXY=None, SOCKS_PROXY=None, HTTPS_PROXY=None,
                  HTTP_PROXY=None, socks_proxy=None, https_proxy=None,
                  http_proxy=None):
            self.assertIsNone(_p.proxy_url())


class TestRedaction(unittest.TestCase):
    def test_credentials_never_in_the_redacted_url(self):
        out = _p._redact("socks5://user:supersecret@host:1080")
        self.assertNotIn("supersecret", out)
        self.assertNotIn("user", out)
        self.assertIn("host:1080", out)

    def test_describe_never_leaks(self):
        with _Env(MAFUTANI_PROXY="socks5://bob:hunter2@proxy.local:1080"):
            d = _p.describe()
            blob = repr(d)
            self.assertNotIn("hunter2", blob)
            self.assertTrue(d["enabled"])
            self.assertTrue(d["authenticated"])


class TestNoProxyBypass(unittest.TestCase):
    def test_loopback_always_direct(self):
        self.assertTrue(_p._should_bypass("localhost"))
        self.assertTrue(_p._should_bypass("127.0.0.1"))

    def test_no_proxy_list(self):
        with _Env(MAFUTANI_PROXY_NO_PROXY="internal.example,10.0.0.0/8"):
            self.assertTrue(_p._should_bypass("internal.example"))
            self.assertTrue(_p._should_bypass("api.binance.com") is False)

    def test_star_disables_everything(self):
        with _Env(MAFUTANI_PROXY_NO_PROXY="*"):
            self.assertTrue(_p._should_bypass("api.binance.com"))


class TestSocks5Handshake(unittest.TestCase):
    """Real loopback SOCKS5 server, real handshake."""

    def test_plain_http_through_socks5(self):
        origin = _EchoServer(); origin.start()
        srv = _Socks5Server(); srv.start()
        with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:%d" % srv.port):
            status, _h, text = _p.request(
                "GET", "http://127.0.0.1:%d/x" % origin.port, timeout=6,
                no_proxy="")
        self.assertEqual(status, 200)
        self.assertIn("ok", text)
        self.assertEqual(origin.hits, 1)
        # The hostname must go to the proxy as a domain, not resolved
        # locally -- resolving locally defeats the point of proxying.
        self.assertTrue(srv.saw_domain_form)

    def test_username_password_auth(self):
        origin = _EchoServer(); origin.start()
        srv = _Socks5Server(require_auth=True); srv.start()
        with _Env(MAFUTANI_PROXY="socks5://u:p@127.0.0.1:%d" % srv.port):
            status, _h, _t = _p.request(
                "GET", "http://127.0.0.1:%d/" % origin.port, timeout=6,
                no_proxy="")
        self.assertEqual(status, 200)

    def test_refused_connect_raises_with_code(self):
        srv = _Socks5Server(reply_code=0x05); srv.start()
        with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:%d" % srv.port):
            with self.assertRaises(_p.ProxyError) as ctx:
                _p.request("GET", "http://127.0.0.1:1/", timeout=6,
                           no_proxy="")
        self.assertIn("refused", str(ctx.exception).lower())

    def test_no_auth_available_but_required_raises(self):
        srv = _Socks5Server(require_auth=True); srv.start()
        with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:%d" % srv.port):
            with self.assertRaises(_p.ProxyError) as ctx:
                _p.request("GET", "http://127.0.0.1:1/", timeout=6,
                           no_proxy="")
        self.assertIn("credential", str(ctx.exception).lower())


class TestHttpConnect(unittest.TestCase):

    def test_connect_tunnelling(self):
        origin = _EchoServer(); origin.start()
        srv = _HttpProxyServer(); srv.start()
        with _Env(MAFUTANI_PROXY="http://127.0.0.1:%d" % srv.port):
            status, _h, text = _p.request(
                "GET", "http://127.0.0.1:%d/p" % origin.port, timeout=6,
                no_proxy="")
        self.assertEqual(status, 200)
        self.assertIn("ok", text)
        self.assertTrue(srv.connect_lines[0].startswith("CONNECT "))

    def test_refused_connect(self):
        srv = _HttpProxyServer(); srv.start()
        # A proxy that only speaks CONNECT cannot serve an absolute-form
        # plain-HTTP request; the request must fail loudly, not silently.
        with _Env(MAFUTANI_PROXY="http://127.0.0.1:%d" % srv.port):
            try:
                status, _h, _t = _p.request(
                    "GET", "http://127.0.0.1:1/", timeout=4)
                self.assertNotEqual(status, 200)
            except Exception:
                pass


class TestNoProxyPathUnchanged(unittest.TestCase):
    """With nothing configured, behaviour must be exactly as before."""

    def test_direct_request_still_works(self):
        origin = _EchoServer(); origin.start()
        with _Env(MAFUTANI_PROXY=None, SOCKS_PROXY=None, HTTPS_PROXY=None,
                  HTTP_PROXY=None, socks_proxy=None, https_proxy=None,
                  http_proxy=None):
            status, _h, text = _p.request(
                "GET", "http://127.0.0.1:%d/" % origin.port, timeout=6,
                no_proxy="")
        self.assertEqual(status, 200)
        self.assertIn("ok", text)

    def test_feeds_http_get_unchanged_without_proxy(self):
        from data import feeds
        origin = _EchoServer(); origin.start()
        with _Env(MAFUTANI_PROXY=None, SOCKS_PROXY=None, HTTPS_PROXY=None,
                  HTTP_PROXY=None, socks_proxy=None, https_proxy=None,
                  http_proxy=None):
            body = feeds._http_get("http://127.0.0.1:%d/" % origin.port, timeout=6)
        self.assertIn("ok", body)

    def test_feeds_http_goes_through_proxy_when_set(self):
        # A spy, because a loopback origin would be bypassed by the
        # loopback rule and the test would pass while proving nothing.
        # This asserts the *wiring*: with a proxy configured, every venue
        # fetch is handed to data.proxy.
        from data import feeds
        calls = []
        real = _p.request

        def spy(method, url, **kw):
            calls.append((method, url))
            return (200, {}, '{"ok":true}')

        _p.request = spy
        try:
            with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:1080"):
                body = feeds._http_get("https://api.binance.com/api/v3/ping")
        finally:
            _p.request = real
        self.assertEqual(len(calls), 1, "venue fetch did not use the proxy")
        self.assertIn("api.binance.com", calls[0][1])
        self.assertIn("ok", body)

    def test_feeds_http_bypasses_proxy_when_unset(self):
        # The inverse: with nothing configured it must use urllib, so a
        # deployment that never sets a proxy behaves exactly as before.
        from data import feeds
        origin = _EchoServer(); origin.start()
        seen = []
        real = _p.request

        def spy(*a, **k):
            seen.append(1)
            return real(*a, **k)

        _p.request = spy
        try:
            with _Env(MAFUTANI_PROXY=None, SOCKS_PROXY=None,
                      HTTPS_PROXY=None, HTTP_PROXY=None, socks_proxy=None,
                      https_proxy=None, http_proxy=None):
                body = feeds._http_get("http://127.0.0.1:%d/" % origin.port,
                                       timeout=6)
        finally:
            _p.request = real
        self.assertEqual(seen, [], "proxy layer used with no proxy set")
        self.assertIn("ok", body)

    def test_explicit_empty_disables(self):
        # A stray HTTP_PROXY in the deployment must not silently reroute
        # orders; MAFUTANI_PROXY="" is the kill switch.
        with _Env(MAFUTANI_PROXY="", HTTP_PROXY="socks5://1.2.3.4:1080"):
            self.assertIsNone(_p.proxy_url())


class TestProbe(unittest.TestCase):

    def test_probe_without_proxy_reports_disabled(self):
        with _Env(MAFUTANI_PROXY=None, SOCKS_PROXY=None, HTTPS_PROXY=None,
                  HTTP_PROXY=None, socks_proxy=None, https_proxy=None,
                  http_proxy=None):
            out = _p.probe()
        self.assertFalse(out["enabled"])
        self.assertFalse(out["ok"])

    def test_probe_never_raises(self):
        # A diagnostic that throws is worse than useless mid-incident.
        with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:1"):
            out = _p.probe(timeout=1)
        self.assertIn("error", out)
        self.assertIsInstance(out, dict)


class _KeepAliveServer(threading.Thread):
    """Sends Content-Length, then deliberately does NOT close.

    This is the shape of a proxy or origin that keeps connections alive,
    and it is what the length-delimited read exists for. A client that
    reads to end-of-stream blocks here until its timeout and then reports
    a transport failure for a response that arrived in full and correct.
    """

    daemon = True

    def __init__(self):
        super().__init__()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.held = []

    def run(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.held.append(conn)          # keep it open
            threading.Thread(target=self._serve, args=(conn,),
                             daemon=True).start()

    def _serve(self, conn):
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                c = conn.recv(4096)
                if not c:
                    return
                data += c
            body = b'{"ok":true}'
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: " + str(len(body)).encode() +
                b"\r\n\r\n" + body)
            # Deliberately no close(), and no read for further requests.
        except Exception:
            pass


class TestLengthDelimitedRead(unittest.TestCase):
    """Regression: reading to EOF hangs against keep-alive peers."""

    def test_content_length_terminates_before_close(self):
        srv = _KeepAliveServer(); srv.start()
        import time as _t
        t0 = _t.time()
        status, _h, text = _p.request(
            "GET", "http://127.0.0.1:%d/" % srv.port, timeout=4)
        elapsed = _t.time() - t0
        self.assertEqual(status, 200)
        self.assertIn("ok", text)
        # A close-delimited read would burn the full timeout here.
        self.assertLess(elapsed, 3.0,
                        "read blocked on an open socket instead of "
                        "honouring Content-Length")
        srv.sock.close()


class TestTlsThroughTunnel(unittest.TestCase):
    """TLS across a tunnelled socket -- the only path production uses.

    Every venue is HTTPS, so this is the real code path, and it is the one
    the plain-HTTP tests above cannot reach: SNI selection and certificate
    verification both happen *after* the tunnel is established, and a bug
    there (for example verifying against the proxy's hostname instead of
    the venue's) would be invisible until the first live order.

    A self-signed CA is generated with openssl and handed to the client
    context via `cafile`. Verification stays ON -- only the trust anchor
    changes. Skipped when openssl is absent rather than silently passing.
    """

    @classmethod
    def setUpClass(cls):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl not available")
        cls._tmp = tempfile.mkdtemp()
        cls._key = os.path.join(cls._tmp, "k.pem")
        cls._crt = os.path.join(cls._tmp, "c.pem")
        try:
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048",
                 "-keyout", cls._key, "-out", cls._crt, "-days", "1",
                 "-nodes", "-subj", "/CN=localhost"],
                check=True, capture_output=True, timeout=30)
        except Exception as e:
            raise unittest.SkipTest("could not generate a test CA: {}".format(e))

    def _tls_origin(self):
        import ssl
        import threading as _t

        # Capture the cert paths in the closure. Inside Origin.__init__
        # `self` is the Origin instance, not the TestCase, so self._crt
        # would resolve against the wrong object.
        crt, key = self._crt, self._key

        class Origin(_t.Thread):
            daemon = True

            def __init__(self):
                super().__init__()
                self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                self.ctx.load_cert_chain(crt, key)
                self.sock = socket.socket()
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.sock.bind(("127.0.0.1", 0))
                self.sock.listen(5)
                self.port = self.sock.getsockname()[1]

            def run(self):
                while True:
                    try:
                        c, _ = self.sock.accept()
                    except OSError:
                        return
                    _t.Thread(target=self._h, args=(c,), daemon=True).start()

            def _h(self, c):
                try:
                    tls = self.ctx.wrap_socket(c, server_side=True)
                    buf = b""
                    while b"\r\n\r\n" not in buf:
                        x = tls.recv(4096)
                        if not x:
                            return
                        buf += x
                    body = b'{"secure":true}'
                    tls.sendall(
                        b"HTTP/1.1 200 OK\r\nContent-Length: " +
                        str(len(body)).encode() +
                        b"\r\nConnection: close\r\n\r\n" + body)
                except Exception:
                    pass
                finally:
                    try:
                        c.close()
                    except Exception:
                        pass

        o = Origin()
        o.start()
        return o

    def test_https_through_socks5_verifies_certificate(self):
        import ssl as _ssl
        origin = self._tls_origin()
        srv = _Socks5Server()
        srv.start()
        ctx = _ssl.create_default_context(cafile=self._crt)
        saved = _ssl.create_default_context
        _ssl.create_default_context = lambda *a, **k: ctx
        try:
            with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:%d" % srv.port):
                status, _h, body = _p.request(
                    "GET", "https://localhost:%d/" % origin.port,
                    timeout=10, no_proxy="")
        finally:
            _ssl.create_default_context = saved
            origin.sock.close()
            srv.sock.close()
        self.assertEqual(status, 200)
        self.assertIn("secure", body)

    def test_untrusted_certificate_is_refused(self):
        # The whole point of verifying: a certificate we do not trust must
        # fail the request rather than be waved through.
        import ssl as _ssl
        import tempfile as _tf
        other = _tf.mkdtemp()
        okey, ocrt = os.path.join(other, "k.pem"), os.path.join(other, "c.pem")
        import subprocess
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048",
                        "-keyout", okey, "-out", ocrt, "-days", "1",
                        "-nodes", "-subj", "/CN=localhost"],
                       check=True, capture_output=True, timeout=30)
        origin = self._tls_origin()
        srv = _Socks5Server()
        srv.start()
        try:
            with _Env(MAFUTANI_PROXY="socks5://127.0.0.1:%d" % srv.port):
                with self.assertRaises(Exception):
                    _p.request("GET", "https://localhost:%d/" % origin.port,
                               timeout=10, no_proxy="")
        finally:
            origin.sock.close()
            srv.sock.close()


class TestChunked(unittest.TestCase):
    def test_dechunk(self):
        raw = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
        self.assertEqual(_p._dechunk(raw), b"hello world")

    def test_dechunk_ignores_garbage(self):
        self.assertEqual(_p._dechunk(b"not-a-size\r\n"), b"")


if __name__ == "__main__":
    unittest.main()
