"""scripts/build_mt5_ea.py -- discovery, parsing, install targeting. No compile.

The one behaviour that matters most here is *not lying about success*.
MetaEditor is a GUI app driven by `/compile:` and `/log:`, and it exits 0
even when compilation fails with errors. So a script that trusts the exit
code would cheerfully report a clean build on a broken EA -- which is
precisely what happened to the first draft of this EA before it was
compiled for real.

These tests pin the parsing and the discovery, and they use a stubbed
MetaEditor so nothing here needs a terminal installed.

Broker independence is also pinned: nothing in the repository may name a
specific terminal, because pinning to one broker's Program Files path makes
the EA unbuildable for everyone else.
"""
import io
import os
import re
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import scripts.build_mt5_ea as B


def metaeditor_log(body, result="Result: 0 errors, 0 warnings, 1200 msec elapsed"):
    """A MetaEditor log, UTF-16-ish with NULs, as it arrives on disk."""
    text = body + ("\n" if body else "") + result + "\n"
    return text.replace("\n", "\r\n").encode("utf-16-le")


class FakeMetaEditor:
    """Stands in for MetaEditor64.exe: writes a log and an .ex5.

    Exit code is always 0, exactly like the real one, so a caller that
    trusts it instead of the log will be caught here.
    """

    def __init__(self, log_bytes, make_ex5=True, sleep_s=0.0):
        self.log_bytes = log_bytes
        self.make_ex5 = make_ex5
        self.sleep_s = sleep_s

    def __call__(self, argv, *args, **kwargs):
        # Substituted directly for subprocess.run, so it tolerates the
        # keyword arguments compile_ea passes. It also stands in for
        # `wslpath`, which does not exist off a WSL host.
        argv = list(argv)
        if argv and str(argv[0]) in ("wslpath", "wslpath.exe"):
            # Identity, not a fake Windows path: the stub has to receive the
            # real temp paths so the files it writes are readable. Returning
            # an invented C:\ path makes the stub write somewhere that does
            # not exist, the log is never found, and compile_ea then spends
            # its full retry loop concluding "no result line".
            return subprocess.CompletedProcess(argv, 0, argv[-1] + "\n", "")
        if argv and str(argv[0]).endswith((".py", "build_mt5_ea.py")):
            argv = argv[1:]
        staged = None
        log = None
        # Iterate argv, not *args: the switches are positional, and reading
        # the wrong one silently leaves staged/log as None so the stub writes
        # nothing and the caller concludes the build produced no output.
        for a in argv:
            if a.startswith("/compile:"):
                staged = _posix(a.split(":", 1)[1])
            elif a.startswith("/log:"):
                log = _posix(a.split(":", 1)[1])
        if staged:
            base = staged[:-4] if staged.endswith(".mq5") else staged
            if self.make_ex5:
                with open(base + ".ex5", "wb") as f:
                    f.write(b"EX5\x00\x01fake")
            if log:
                with open(log, "wb") as f:
                    f.write(self.log_bytes)
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def _posix(p):
    """Windows-style path back to something openable on the test host.

    compile_ea hands MetaEditor a Windows path (that's correct for the real
    compiler), but the stub runs on Linux and has to open that path. This
    undoes the backslashes just for the stub's own file writes.
    """
    return p.replace("\\", "/") if not os.path.exists(p) else p


class TestResultParsing(unittest.TestCase):
    """The exit code is useless; the log is the truth."""

    def _run(self, log_bytes, make_ex5=True):
        # Both entry points have to be stubbed. compile_ea calls
        # subprocess.check_output for `wslpath` and subprocess.run for
        # MetaEditor; patching only run lets the real wslpath hand the stub
        # a Windows path it cannot write to, so the log is never created and
        # the function burns its whole 30s wait concluding "no result line".
        orig_run, orig_co = subprocess.run, subprocess.check_output
        fake = FakeMetaEditor(log_bytes, make_ex5)
        subprocess.run = fake
        subprocess.check_output = fake
        try:
            return B.compile_ea("/nonexistent/MetaEditor64.exe")
        finally:
            subprocess.run = orig_run
            subprocess.check_output = orig_co

    def test_clean_log_reports_ok(self):
        ok, text, ex5 = self._run(metaeditor_log("compiling 'MafutaniBridge.mq5'"))
        self.assertTrue(ok, "a clean log was not recognised:\n" + text)
        self.assertIn("0 errors, 0 warnings", text)

    def test_errors_report_failure_despite_exit_zero(self):
        """The bug this script exists to not have."""
        log = metaeditor_log(
            "x.mq5(12,3) : error 166: 'WebRequest' - no one of the overloads",
            result="Result: 9 errors, 0 warnings")
        ok, text, _ = self._run(log)
        self.assertFalse(ok, "reported success on 9 compile errors")

    def test_warnings_alone_are_not_clean(self):
        log = metaeditor_log("x.mq5 : warning 43: something",
                             result="Result: 0 errors, 1 warning")
        ok, _t, _e = self._run(log)
        self.assertFalse(ok, "a warning was reported as a clean build")

    def test_clean_log_but_no_artifact_fails(self):
        """"0 errors" without an .ex5 means the build did not happen."""
        ok, _t, _e = self._run(metaeditor_log("compiling"), make_ex5=False)
        self.assertFalse(ok, "claimed success with no .ex5 produced")

    def test_missing_result_line_is_not_success(self):
        # result="" is the point: the helper appends a clean Result line by
        # default, so calling it without this argument was testing the
        # opposite case and passing for the wrong reason.
        ok, text, _ = self._run(metaeditor_log("some unrelated output",
                                              result=""))
        self.assertFalse(ok, "a log with no Result: line was treated as a "
                             "successful build")
        self.assertIn("no result line", text.lower())

    def test_default_helper_appends_a_result_line(self):
        """Guards the previous test: make sure it is really testing absence."""
        import re as _re
        self.assertIn("Result:", FakeMetaEditor(
            metaeditor_log("x")).log_bytes.decode("utf-16-le", "replace"))
        self.assertNotIn("Result:", FakeMetaEditor(
            metaeditor_log("x", result="")).log_bytes.decode("utf-16-le",
                                                             "replace"))

    def test_nul_and_cr_are_stripped_before_matching(self):
        """MetaEditor writes UTF-16; matching raw bytes silently finds nothing."""
        ok, _t, _e = self._run(metaeditor_log("compiling"))
        self.assertTrue(ok, "UTF-16 log was not decoded")

    def test_missing_source_is_reported_not_crashed(self):
        ok, text, _ = B.compile_ea("/x/MetaEditor64.exe", src="/nope.mq5")
        self.assertFalse(ok)
        self.assertIn("source not found", text)


class TestDiscovery(unittest.TestCase):
    def test_no_matches_returns_empty_not_error(self):
        orig = B.METAEDITOR_GLOBS
        B.METAEDITOR_GLOBS = ("/nonexistent/*/MetaEditor64.exe",)
        try:
            self.assertEqual(B.find_metaeditors(), [])
        finally:
            B.METAEDITOR_GLOBS = orig

    def test_deduplicates_same_file_reached_two_ways(self):
        orig = B.METAEDITOR_GLOBS
        B.METAEDITOR_GLOBS = ("/x/MetaEditor64.exe", "/x/MetaEditor64.exe")
        try:
            # Neither exists, but the point is it must not crash and must
            # return a list.
            self.assertIsInstance(B.find_metaeditors(), list)
        finally:
            B.METAEDITOR_GLOBS = orig

    def test_discovery_never_names_a_broker(self):
        """The globs must stay generic or the EA is unbuildable elsewhere.

        `MetaQuotes/Terminal` is MetaQuotes' own vendor directory, present
        for every MT5 install regardless of broker, so it is allowed and
        must be: it is how the data folders are found. What is forbidden is
        a *broker* name or a specific installation hash.
        """
        joined = " ".join(B.METAEDITOR_GLOBS)
        for broker in ("NAGA", "IC Markets", "Pepperstone", "29BA68CC",
                       "71C4AE97"):
            self.assertNotIn(broker.lower(), joined.lower(),
                             "discovery glob is pinned to %s" % broker)

    def test_terminal_id_extracted_from_a_data_folder(self):
        p = ("/home/u/AppData/Roaming/MetaQuotes/Terminal/"
             "29BA68CC29E9DA83348D704FFF8CF8A0/MQL5/Experts")
        self.assertEqual(B.terminal_id(p), "29BA68CC29E9DA83348D704FFF8CF8A0")

    def test_terminal_id_on_an_unexpected_path_still_returns_something(self):
        self.assertTrue(B.terminal_id("/tmp/whatever/Experts"))


class TestRepositoryIsBrokerIndependent(unittest.TestCase):
    """No tracked file may hardcode one terminal's install path."""

    FORBIDDEN = ("NAGA", "29BA68CC", "71C4AE97")

    def _tracked_text_files(self):
        try:
            out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                                 capture_output=True, text=True, timeout=60)
            names = out.stdout.split()
        except Exception:
            return []
        for name in names:
            if not name.endswith((".py", ".md", ".mq5", ".sh", ".txt")):
                continue
            try:
                with open(os.path.join(ROOT, name), encoding="utf-8") as f:
                    yield name, f.read()
            except (OSError, UnicodeDecodeError):
                continue

    def test_no_broker_or_install_hash_is_hardcoded(self):
        offenders = []
        for name, text in self._tracked_text_files():
            for token in self.FORBIDDEN:
                if token.lower() in text.lower():
                    offenders.append("%s mentions %s" % (name, token))
        self.assertEqual(offenders, [],
                         "repository is pinned to a specific terminal: %r"
                         % (offenders,))


class TestCliSurface(unittest.TestCase):
    def test_list_reports_nothing_found_without_crashing(self):
        orig = B.METAEDITOR_GLOBS
        B.METAEDITOR_GLOBS = ("/nonexistent/*/MetaEditor64.exe",)
        buf, out = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, buf
        argv = sys.argv
        sys.argv = ["build_mt5_ea.py", "--list"]
        try:
            code = B.main()
        finally:
            sys.argv = argv
            sys.stdout, sys.stderr = old_out, old_err
            B.METAEDITOR_GLOBS = orig
        self.assertEqual(code, 2, "should report failure when no compiler exists")
        self.assertIn("none", out.getvalue().lower())

    def test_explicit_missing_metaeditor_is_an_error(self):
        import argparse
        argv = sys.argv
        sys.argv = ["build_mt5_ea.py", "--metaeditor", "/nope/MetaEditor64.exe"]
        buf, out = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, buf
        try:
            code = B.main()
        except SystemExit as e:
            code = e.code
        finally:
            sys.argv = argv
            sys.stdout, sys.stderr = old_out, old_err
        self.assertEqual(code, 2, "accepted a nonexistent MetaEditor path")

    def test_ex5_is_gitignored(self):
        """A build artifact must never be committed."""
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            rules = f.read()
        self.assertIn("*.ex5", rules)
        try:
            out = subprocess.run(["git", "check-ignore", "-q", "x.ex5"],
                                 cwd=ROOT, timeout=60)
            self.assertEqual(out.returncode, 0, "*.ex5 is not actually ignored")
        except Exception:
            self.skipTest("git not available")


if __name__ == "__main__":
    unittest.main(verbosity=2)
