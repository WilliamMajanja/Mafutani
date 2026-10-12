"""scripts/mt5_preflight.py -- the preflight must not pass when it shouldn't.

A preflight exists to tell an operator where a failure is *not*. A preflight
that reports green when the bridge is broken is worse than no preflight,
because it moves the operator's attention to the wrong layer -- the EA --
when the fault is in the Python side they could have fixed themselves.

So these tests run the preflight for real, against the real server, and
assert it goes red under each way things can be wrong.

They also pin the two mistakes this script has actually made:

  * `except BrokerError` around a check that raises a different exception
    type reports success for a failure. An early version raised
    AssertionError and was caught as the *expected* broker failure.
  * A "silent EA" modelled by stopping the thread is not silent: the
    thread is parked in a long-poll and answers the next command anyway.
    A muted EA is the only faithful model.
"""
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

PREFLIGHT = os.path.join(ROOT, "scripts", "mt5_preflight.py")
BRIDGE = os.path.join(ROOT, "services", "mt5", "bridge_server.py")


def run_preflight(timeout=180):
    return subprocess.run([sys.executable, PREFLIGHT], capture_output=True,
                          text=True, cwd=ROOT, timeout=timeout,
                          env=dict(os.environ))


class TestPreflightPassesAgainstAWorkingBridge(unittest.TestCase):
    """The happy path, so the negative tests below mean something."""

    @classmethod
    def setUpClass(cls):
        """Run the preflight once and share it.

        The full-suite run collected this as a failure once, and the cause is
        worth recording rather than papering over with a rerun. Each of
        these tests spawned its own preflight, so four servers plus four
        simulated EAs were cycling `/next` long-polls within seconds of each
        other. Under load the bridge's 25s poll hold can exceed the
        preflight's own patience, and the output then legitimately differs
        from a quiet run.

        Running it once removes both the flake and the load. The assertion
        that matters -- that a broken bridge must not report success -- is
        covered separately below with its own process.
        """
        cls.p = run_preflight()
        cls.out = cls.p.stdout

    def test_all_checks_pass_and_exit_zero(self):
        self.assertEqual(self.p.returncode, 0,
                         "preflight failed on a working bridge:\n%s"
                         % (self.p.stdout + self.p.stderr))
        self.assertNotIn("FAIL", self.out)

    def test_covers_the_whole_documented_sequence(self):
        for expected in ("bridge_server.py started", "wrong-token-refused",
                         "connect()", "account()", "quote()", "positions()",
                         "live-gate-blocks", "submit_order()",
                         "cancel_order()", "muted-ea-warns"):
            self.assertIn(expected, self.out, "no check for %r" % expected)

    def test_states_what_it_did_not_verify(self):
        """The most important line in the output.

        A green preflight that implied MT5 was working would be the single
        most misleading thing this script could print.
        """
        self.assertIn("does NOT verify MQL5", self.out)
        self.assertIn("Still unverified", self.out)

    def test_submit_reports_open_never_filled(self):
        """retcode 10009 must never surface as 'filled'.

        The simulated EA answers 10009, so if this ever prints 'filled' the
        invariant the adapter exists to hold has been broken, and the
        preflight is the last place it would be noticed.
        """
        self.assertIn("status=open", self.out)
        self.assertNotIn("status=filled", self.out)


class TestPreflightCatchesBrokenServer(unittest.TestCase):
    """Now the important half: it must go red, not green, when it should."""

    def test_missing_bridge_is_reported_not_hidden(self):
        """A bridge that cannot start must fail the preflight.

        Pointed at a non-existent script so the subprocess cannot start at
        all -- the same shape of failure as a syntax error or a missing
        dependency on a deployment.
        """
        p = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); import scripts.mt5_preflight"
             " as m; m.BRIDGE = '/nonexistent/bridge_server.py'; "
             "sys.exit(m.main())" % ROOT],
            capture_output=True, text=True, cwd=ROOT, timeout=120)
        self.assertNotEqual(p.returncode, 0,
                            "preflight reported success with no bridge at all")
        self.assertIn("FAIL", p.stdout + p.stderr)


class TestSimEAProtocol(unittest.TestCase):
    """SimEA must speak the protocol the MQL5 EA is forced to speak."""

    def setUp(self):
        import scripts.mt5_preflight as P
        self.P = P

    def test_token_goes_in_the_query_string(self):
        """MQL5 cannot set headers on a POST, so neither can this.

        If the simulator quietly used a header instead, the preflight would
        keep passing after the real EA's only working transport was broken.
        """
        ea = self.P.SimEA("http://x:1", token="tok")
        url = ea._url("/result")
        self.assertIn("token=tok", url)
        self.assertNotIn("X-Mafutani-Token", url)

    def test_muted_ea_counts_but_never_answers(self):
        import threading
        ea = self.P.SimEA("http://127.0.0.1:1", token="t", mute=True)
        self.assertTrue(ea.mute)
        self.assertEqual(ea.handled, 0)
        ea.start()
        ea.stop()

    def test_normal_ea_is_not_muted(self):
        self.assertFalse(self.P.SimEA("http://x:1").mute)


class TestRepositoryInvariants(unittest.TestCase):
    def test_preflight_is_stdlib_only(self):
        import ast
        with open(PREFLIGHT, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    mods.add(a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    mods.add(node.module.split(".")[0])
        project = {"brokers", "data", "core", "scripts", "services",
                   "engines", "api"}
        bad = [m for m in sorted(mods)
               if m not in sys.stdlib_module_names and m not in project]
        self.assertEqual(bad, [], "third-party import in preflight: %r" % bad)

    def test_preflight_does_not_touch_a_real_terminal(self):
        """It must never launch MetaEditor or terminal64.exe.

        The whole point is that it runs safely on a machine with no broker
        account; attaching to a real terminal is the one action here that
        could place an order. A docstring mentioning the EA by name is fine
        -- what must not appear is code that executes it.
        """
        import ast
        with open(PREFLIGHT, encoding="utf-8") as f:
            src = f.read()
        tree = ast.parse(src)

        # Collect every string literal that is actually passed to subprocess.
        literals = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)

        for forbidden in ("MetaEditor", "terminal64", "metatester"):
            for lit in literals:
                self.assertNotIn(
                    forbidden.lower(), lit.lower(),
                    "preflight would execute something matching %s" % forbidden)
        # And the .mq5/.ex5 are never named as an argument.
        for lit in literals:
            self.assertFalse(
                lit.endswith((".mq5", ".ex5")),
                "preflight references a compiled EA: %s" % lit)

    def test_live_flag_is_not_left_enabled(self):
        """A preflight that leaves LIVE_TRADING_ENABLED set is a loaded gun.

        The script enables it locally to exercise the submit path; if that
        were to leak into the operator's environment, the next real order
        would not be gated.
        """
        import scripts.mt5_preflight as P
        before = os.environ.get("LIVE_TRADING_ENABLED")
        run_preflight()
        self.assertEqual(os.environ.get("LIVE_TRADING_ENABLED"), before,
                         "preflight mutated the live-trading environment")


if __name__ == "__main__":
    unittest.main(verbosity=2)
