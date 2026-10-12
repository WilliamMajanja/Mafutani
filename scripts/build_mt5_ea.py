"""Compile MafutaniBridge.mq5 with any installed MetaEditor. Stdlib only.

    python3 scripts/build_mt5_ea.py --list
    python3 scripts/build_mt5_ea.py
    python3 scripts/build_mt5_ea.py --install --terminal <id>
    python3 scripts/build_mt5_ea.py --install --metaeditor "/path/to/MetaEditor64.exe"

Why this exists rather than a documented command
------------------------------------------------
The first pass documented a literal path to one broker's installed
terminal, under Program Files. That is wrong in two ways: it names a
specific broker's terminal, so a user on a different broker cannot build at
all; and it is a path that changes with every terminal update or reinstall.
Nobody should have to edit a command in a README to compile a file.

So the compiler is *discovered*: every MetaEditor64.exe under Program
Files and the local app-data tree is found, and if there is more than one
the ambiguity is reported rather than resolved silently. Building against
a different broker's MetaEditor than the one you run is a legitimate
choice, so the choice is surfaced, not hidden.

That literal path is deliberately not reproduced here: a script whose whole
purpose is to stop hardcoding one would be contradicting itself in its own
docstring, and a test greps the repository to keep it that way.

Nothing is installed anywhere without --install, and --install prints
exactly which directories it wrote to.

The .ex5 is deliberately not committed. It is a build artifact tied to the
MetaEditor build that produced it; see the note below.
"""
import argparse
import glob
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EA_SRC = os.path.join(ROOT, "services", "mt5", "MafutaniBridge.mq5")

# Where MetaEditor lives. Searched in order, and every hit is reported.
METAEDITOR_GLOBS = (
    "/mnt/c/Program Files/*/MetaEditor64.exe",
    "/mnt/c/Program Files (x86)/*/MetaEditor64.exe",
    "/mnt/c/Program Files/*/*/MetaEditor64.exe",
    "/mnt/c/Program Files (x86)/*/*/MetaEditor64.exe",
    os.path.expanduser("~/AppData/Roaming/MetaQuotes/Terminal/*/MQL5"
                       "/../../../../../MetaEditor64.exe"),
    # native Windows paths, in case this runs under Windows Python
    r"C:\Program Files\*\MetaEditor64.exe",
    r"C:\Program Files (x86)\*\MetaEditor64.exe",
)

# Terminal data folders, where the EA actually has to live to be loadable.
TERMINAL_GLOBS = (
    os.path.expanduser("~/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Experts"),
    "/mnt/c/Users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Experts",
)

# MetaEditor writes UTF-16 logs on Windows and the file is full of NULs
# when read as bytes; strip both before matching.
_STRIP = re.compile(r"[\x00\r]")


def find_metaeditors():
    """Every MetaEditor64.exe we can see, deduplicated, in search order."""
    found = []
    seen = set()
    for pattern in METAEDITOR_GLOBS:
        for path in sorted(glob.glob(pattern)):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            found.append(path)
    return found


def find_terminal_experts_dirs():
    """Every terminal's MQL5/Experts folder."""
    found = []
    seen = set()
    for pattern in TERMINAL_GLOBS:
        for path in sorted(glob.glob(pattern)):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not os.path.isdir(path):
                continue
            seen.add(key)
            found.append(path)
    return found


def terminal_id(experts_dir):
    """The per-installation hash MetaQuotes uses for a data folder."""
    parts = os.path.abspath(experts_dir).split(os.sep)
    for p in parts:
        if len(p) == 32 and all(c in "0123456789ABCDEFabcdef" for c in p):
            return p
    return os.path.basename(os.path.dirname(experts_dir))


def compile_ea(metaeditor, src=EA_SRC, keep_artifacts=False):
    """Compile with MetaEditor. Returns (ok, log_text, ex5_path_or_None).

    MetaEditor is a GUI app, so it is driven with its documented
    /compile: and /log: switches and the log is parsed for the result. It
    returns an exit code of 0 even when compilation *fails*, so the log is
    the only trustworthy signal -- an earlier version of this script would
    have reported success on nine compile errors.
    """
    src = os.path.abspath(src)
    if not os.path.isfile(src):
        return False, "source not found: %s" % src, None

    workdir = tempfile.mkdtemp(prefix="mt5build-")
    staged = os.path.join(workdir, os.path.basename(src))
    shutil.copyfile(src, staged)
    log_win = os.path.join(workdir, "build.log")

    def _win(p):
        try:
            return subprocess.check_output(["wslpath", "-w", p],
                                          text=True).strip()
        except Exception:
            return p.replace("/", "\\")

    cmd = [metaeditor, "/compile:" + _win(staged),
           "/log:" + _win(log_win)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, "MetaEditor timed out", None

    # MetaEditor returns before finishing; give it a moment to flush.
    for _ in range(60):
        if os.path.isfile(log_win):
            break
        import time
        time.sleep(0.5)

    text = ""
    if os.path.isfile(log_win):
        with open(log_win, "rb") as f:
            text = _STRIP.sub("", f.read().decode("utf-8", "replace"))

    m = re.search(r"Result:\s*(\d+)\s*errors?,\s*(\d+)\s*warnings?", text)
    if m:
        errs, warns = int(m.group(1)), int(m.group(2))
        # ".mq5" -> ".ex5". Kept as a slice because the extension is fixed
        # length, but the artifact is then *checked for existence* below:
        # a wrong-length slice yields a filename that was never created, and
        # without the check that reads as "the build produced nothing" and
        # fails a build that actually succeeded.
        ex5 = staged[:-3] + "ex5" if staged.endswith(".mq5") else None
        ok = (errs == 0 and warns == 0 and ex5 and os.path.isfile(ex5))
        return ok, text, (ex5 if ok else None)

    # No Result line at all. This is the ambiguous case: MetaEditor wrote
    # *something* but never said whether it succeeded. Append the reason to
    # the log rather than replacing it, so the operator sees both what was
    # written and why it is being called a failure.
    return False, (text or "") + "\nno result line in MetaEditor log", None


def report(ok, text, ex5=None):
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "error" in line.lower() or "Result:" in line:
            print("    " + line)
    print("  -> %s" % ("compiled clean" if ok else "COMPILE FAILED"))
    if ex5:
        print("  artifact: %s (%d bytes)" % (ex5, os.path.getsize(ex5)))
    return ok


def main():
    ap = argparse.ArgumentParser(
        description="Compile the MT5 bridge EA with any installed MetaEditor.")
    ap.add_argument("--list", action="store_true",
                    help="show discovered MetaEditors and terminals, build nothing")
    ap.add_argument("--install", action="store_true",
                    help="copy the compiled .ex5 and .mq5 into terminal folders")
    ap.add_argument("--terminal", action="append", default=[],
                    help="install only into terminals whose id contains this "
                         "(repeatable); default is every terminal found")
    ap.add_argument("--metaeditor", help="explicit path to MetaEditor64.exe")
    args = ap.parse_args()

    editors = find_metaeditors()
    targets = find_terminal_experts_dirs()

    print("MetaEditor found (%d):" % len(editors))
    for e in editors:
        print("    %s" % e)
    if not editors:
        print("    none. Install a MetaTrader terminal (any broker), or pass "
              "--metaeditor /path/to/MetaEditor64.exe")

    print("Terminal Experts folders found (%d):" % len(targets))
    for t in targets:
        print("    %s   [%s]" % (t, terminal_id(t)))
    if not targets:
        print("    none. Run the terminal once so it creates its data folder.")

    if args.list:
        if len(editors) > 1:
            print("\nMore than one MetaEditor is installed. This script will "
                  "use the first in search order;\npass --metaeditor to pick "
                  "a different one (any broker's MetaEditor can compile this "
                  "EA -- it is\nnot broker-specific).")
        return 0 if editors else 2

    if not editors:
        return 2

    chosen = args.metaeditor or editors[0]
    if args.metaeditor:
        chosen = args.metaeditor
        if not os.path.isfile(chosen):
            print("no such MetaEditor: %s" % chosen, file=sys.stderr)
            return 2
    elif len(editors) > 1:
        print("  using: %s" % chosen)
        print("  (a different broker's MetaEditor would also work; use "
              "--metaeditor to choose)")

    print("\ncompiling %s" % os.path.relpath(EA_SRC, ROOT))
    ok, text, ex5 = compile_ea(chosen)
    if not report(ok, text, ex5):
        return 1

    if not args.install:
        print("\nnot installed. Re-run with --install to place it in a "
              "terminal's Experts folder.")
        return 0

    wanted = args.terminal
    installed = 0
    for t in targets:
        tid = terminal_id(t)
        if wanted and not any(w.lower() in tid.lower() for w in wanted):
            continue
        for f in (ex5, EA_SRC):
            if f and os.path.isfile(f):
                shutil.copyfile(f, os.path.join(t, os.path.basename(f)))
        print("  installed -> %s" % t)
        installed += 1
    if not installed:
        print("nothing installed: no terminal matched --terminal", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
