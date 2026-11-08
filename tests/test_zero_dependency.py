"""The app is stdlib-only, and this proves it app-wide.

Why this file exists
--------------------
`tests/test_ml_bridge.py` enforced this by importing the application and
asserting `torch` was unreachable. When the torch ML service was removed on
2026-10-16 that file went with it, and with it the only check that swept the
*whole* tree. What remained were per-module import scans
(`test_gods.py`, `test_chart_context.py`, `test_macro_regime.py`, ...), each
guarding one file it already knew about. That is the shape of test that
cannot catch the dependency nobody thought to look for.

So this is the app-wide sweep, restored. It walks the application tree,
parses every file with `ast` (never imports it -- importing is what would
drag the dependency in), and fails if any absolute import is neither a
stdlib module nor one of this project's own.

What "zero dependency" means here
---------------------------------
No `requirements.txt`, no `setup.py`, no `pyproject.toml`, no `package.json`.
The Dockerfile installs nothing. Every module the app loads at runtime is
either in the Python standard library or in this repository. The one
historically-optional exception -- the torch ML service -- is gone, so the
claim is now unconditional.

The positive control
--------------------
`test_the_detector_flags_a_third_party_import` runs the same checker over a
source string that imports `numpy` and asserts it is caught. A sweep that
cannot fail is not a guard; it is a comment. This is the same discipline the
suite applies elsewhere (`test_db_isolation`, the feed-prune wiring test).
"""
import ast
import os
import sys
import unittest

WORKDIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The application tree as documented in the README header. `services/` is
# deliberately excluded: it is the optional, separately-run MT5 bridge, and
# conflating "the app has no dependencies" with "an auxiliary service might"
# is how the old exception crept in. `tests/` is excluded because a test
# runner is not shipped.
#
# `minting/` and `chain/` are imported/used by application code and are part of
# the shipped app, not auxiliary scripts. A guard that only covers directories
# someone remembered is the exact failure mode this file exists to prevent --
# so the list is asserted against the on-disk tree by the test below.
APP_DIRS = ("core", "data", "api", "engines", "brokers", "scripts", "minting", "chain", "node", "compositor")
SKIP = {".git", "__pycache__", ".venv-torch", "node_modules", ".mypy_cache"}


def _project_names():
    """Every module root this project owns.

    Includes both directory packages (`api`, `data`, ...) and the bare
    module names used by the fallback imports some files carry for direct
    execution (`from venuebooks import ...`). Missing one of these would
    make a real module look third-party, so the set is built by scanning
    rather than hand-listed.
    """
    names = set()
    for dirpath, dirs, files in os.walk(WORKDIR):
        dirs[:] = [d for d in dirs if d not in SKIP]
        for f in files:
            if f.endswith(".py"):
                names.add(f[:-3])
    # Top-level directories that contain Python are packages even without
    # an __init__.py (PEP 420 namespace packages), which `core/` and
    # `data/` are.
    for entry in os.listdir(WORKDIR):
        full = os.path.join(WORKDIR, entry)
        if not os.path.isdir(full) or entry in SKIP:
            continue
        for dirpath, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d not in SKIP]
            if any(f.endswith(".py") for f in files):
                names.add(entry)
                break
    return names


def _app_files():
    files = [os.path.join(WORKDIR, "app.py")]
    for d in APP_DIRS:
        for dirpath, dirs, names in os.walk(os.path.join(WORKDIR, d)):
            dirs[:] = [x for x in dirs if x not in SKIP]
            files += [os.path.join(dirpath, n) for n in names
                      if n.endswith(".py")]
    return sorted(files)


def _import_roots(source):
    """Top-level module names imported by `source`, stdlib filtering aside.

    Relative imports (`from .x import y`) are skipped: they cannot reach a
    third-party package. Only `level == 0` absolute imports are considered.
    """
    roots = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return roots


class ZeroDependency(unittest.TestCase):

    def test_the_app_tree_imports_nothing_third_party(self):
        project = _project_names()
        offenders = []
        for path in _app_files():
            with open(path, encoding="utf-8") as fh:
                roots = _import_roots(fh.read())
            for root in sorted(roots):
                if root in sys.stdlib_module_names or root in project:
                    continue
                offenders.append("%s: %s" % (
                    os.path.relpath(path, WORKDIR), root))
        self.assertEqual(offenders, [],
                         "third-party import(s) in the app tree: %s"
                         % offenders)

    def test_the_sweep_actually_reads_the_app(self):
        """A scan that reads nothing passes trivially.

        Asserts the sweep reaches the entry point and a healthy fraction of
        the tree, so a path typo that turns this into a no-op fails loudly
        instead of staying green.
        """
        files = _app_files()
        self.assertIn(os.path.join(WORKDIR, "app.py"), files)
        self.assertGreater(len(files), 40,
                           "only %d app files found -- sweep is blind"
                           % len(files))

    def test_every_python_package_in_the_tree_is_swept(self):
        """A directory left out of APP_DIRS is unguarded code.

        `minting/` shipped after this sweep was written and imports
        `http.cookiejar`, `ssl` and friends. Had it been forgotten here it
        could have grown a third-party import with nothing noticing, which is
        the whole reason the sweep walks the tree rather than trusting a
        hand-kept list. So the list itself is checked against disk.
        """
        swept = set(APP_DIRS)
        on_disk = set()
        for entry in os.listdir(WORKDIR):
            full = os.path.join(WORKDIR, entry)
            if not os.path.isdir(full) or entry in SKIP:
                continue
            for dirpath, dirs, files in os.walk(full):
                dirs[:] = [d for d in dirs if d not in SKIP]
                if any(f.endswith(".py") for f in files):
                    on_disk.add(entry)
                    break
        # `services/` holds the optional, separately-run MT5 bridge and `web/`
        # is static assets: both are excluded on purpose, documented above.
        intentional = {"services", "web", "docs", "rfcs", "rffs", "tests",
                       "data", "__pycache__"}
        missing = on_disk - swept - intentional
        self.assertEqual(missing, set(),
                         "python package(s) not covered by the zero-dependency"
                         " sweep: %s" % sorted(missing))

    def test_the_detector_flags_a_third_party_import(self):
        """The positive control: prove the checker can fail."""
        project = _project_names()
        roots = _import_roots("import numpy\nimport json\n")
        outside = [r for r in roots
                   if r not in sys.stdlib_module_names and r not in project]
        self.assertEqual(outside, ["numpy"])

    def test_no_python_dependency_manifests(self):
        """Zero-dependency is a fact about the repository, not a promise.

        A `requirements.txt` with a single line would contradict the claim
        just as surely as an import would.
        """
        manifests = ("requirements.txt", "requirements-dev.txt", "setup.py",
                     "pyproject.toml", "Pipfile", "poetry.lock",
                     "package.json", "environment.yml", "conda.yml")
        present = [m for m in manifests
                   if os.path.exists(os.path.join(WORKDIR, m))]
        self.assertEqual(present, [],
                         "dependency manifest(s) present: %s" % present)


if __name__ == "__main__":
    unittest.main(verbosity=2)
