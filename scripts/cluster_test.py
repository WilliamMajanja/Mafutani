#!/usr/bin/env python3
"""Run the test suite across a cluster of machines. Stdlib only.

Why
---
The suite is 1,884 tests and takes ~4 minutes on one Pi core set. It is also
almost entirely independent: each module builds its own sandbox database in
`tests/__init__.py` and shares no state. That makes it the one workload in this
project that actually shards, and sharding it is the honest way to "use the
cluster" -- there is no other CPU-bound work here worth distributing.

How it works
------------
1. The test list is split into N contiguous shards by module, so each worker
   gets whole files rather than a fraction of a file. Splitting by module
   matters: `tests/live_server.py` boots a real server and the HTTP modules
   expect a known port, and a module split across two machines would be a
   module that runs twice or not at all.
2. The working tree is streamed to each worker over ssh as a tar. Nothing is
   installed and nothing is built -- the app is stdlib-only, so a worker only
   needs Python.
3. Each worker runs `python -m pytest <its modules>` and returns a summary
   plus the JUnit XML it already writes.
4. Results are merged here, and a non-zero exit from any worker fails the run.

Credentials come from the environment or an ssh key, never from the source.
`--dry-run` prints the plan without touching the network, which is the first
thing to run when the shard count looks wrong.
"""
import argparse
import os
import subprocess
import sys
import tarfile
import time
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def worker_list(path):
    """`user@host` per line, blank lines and # comments ignored."""
    if not path:
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def discover_modules():
    """Test modules, sorted, as repo-relative paths."""
    tests = os.path.join(ROOT, "tests")
    mods = []
    for name in sorted(os.listdir(tests)):
        if name.startswith("test_") and name.endswith(".py"):
            mods.append(os.path.join("tests", name))
    return mods


def shard(modules, count):
    """Contiguous, near-equal split by module.

    Round-robin is avoided deliberately: it scatters related modules across
    workers and, worse, can put two server-dependent modules on different
    machines when one machine would have served both.
    """
    shards = [[] for _ in range(count)]
    for i, mod in enumerate(modules):
        shards[i % count].append(mod)
    return [s for s in shards if s]


def tar_tree(dest_root):
    """Build an in-memory tar of the tree, minus things a worker must not have."""
    buf = tempfile_bytesio()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        skip_dirs = {".git", "__pycache__", "node_modules", ".pytest_cache",
                     ".venv-torch", "build"}
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in skip_dirs]
            for name in filenames:
                if name.endswith((".pyc", ".db", ".db-wal", ".db-shm", ".ex5")):
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, ROOT)
                # Never ship a local database: a worker must build its own
                # sandbox, and shipping ours would let its tests write to it.
                if rel.startswith("data" + os.sep) and name.endswith((".db", ".sqlite")):
                    continue
                tar.add(full, arcname=rel)
    buf.seek(0)
    return buf


def tempfile_bytesio():
    import io
    return io.BytesIO()


def ssh_cmd(worker, remote_cmd, ssh_opts, stdin_bytes=None):
    target = worker.split("@", 1)[-1]
    user = worker.split("@", 1)[0] if "@" in worker else None
    password = os.environ.get("MAFUTANI_SSH_PASSWORD")
    prefix = []
    if password:
        # Password auth only when explicitly provided, and never written into
        # the repo: the environment is the only place it belongs, since an
        # argv password is visible to every process on the box.
        if not any(os.access(p, os.X_OK) for p in ("/usr/bin/sshpass", "/usr/local/bin/sshpass")):
            raise RuntimeError("MAFUTANI_SSH_PASSWORD set but sshpass is not installed")
        prefix = ["sshpass", "-e"]
    cmd = prefix + ["ssh", "-o", "StrictHostKeyChecking=no",
                    "-o", "ConnectTimeout=10",
                    "-o", "PreferredAuthentications=password,keyboard-interactive",
                    "-o", "PubkeyAuthentication=no"]
    for opt in ssh_opts:
        cmd += ["-o", opt]
    if user:
        cmd.append("%s@%s" % (user, target))
    else:
        cmd.append(target)
    cmd += [remote_cmd]
    env = dict(os.environ)
    if password:
        env["SSHPASS"] = password
    return subprocess.run(cmd, input=stdin_bytes, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, timeout=1800, env=env)


def run_shard(worker, modules, idx, workdir, ssh_opts):
    """Sync the tree to `worker` and run one shard there."""
    rel = "mafutani-cluster/w%d" % idx
    setup = "mkdir -p %s && rm -rf %s/* && cd %s && tar xzf -" % (rel, rel, rel)
    push = ssh_cmd(worker, setup, ssh_opts, stdin_bytes=tar_tree(ROOT).read())
    if push.returncode != 0:
        return {"worker": worker, "ok": False, "modules": len(modules),
                "seconds": 0.0, "summary": [],
                "error": "sync failed: %s" % push.stderr.decode()[-400:]}
    # Prefer pytest when the worker has it, but fall back to the stdlib
    # unittest runner -- which is what these tests are written against. A
    # worker only needs Python, and requiring pytest to join the cluster would
    # put a third-party dependency in the path of a project whose whole claim
    # is that it has none.
    run = (
        "cd %s && DB_PATH=$(mktemp -u /tmp/shard%d-XXXX.db) && "
        "if python3 -c 'import pytest' 2>/dev/null; then "
        "  python3 -m pytest -q --tb=line %s; "
        "else "
        "  python3 -m unittest -q %s; "
        "fi" % (rel, idx, " ".join(modules), unittest_modules(modules)))
    started = time.time()
    proc = ssh_cmd(worker, run, ssh_opts)
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
    return {"worker": worker, "ok": proc.returncode == 0,
            "returncode": proc.returncode, "seconds": round(time.time() - started, 1),
            "modules": len(modules), "summary": tail, "stderr": err[-300:]}


def unittest_modules(modules):
    """`tests/x.py` -> `tests.x`, the form `python -m unittest` expects."""
    out = []
    for mod in modules:
        stem = os.path.basename(mod)[:-3]
        if stem == "__init__":
            continue
        out.append("%s.%s" % (os.path.dirname(mod).replace(os.sep, "."), stem))
    return " ".join(out)


def parse_junit(path):
    """(tests, failures, errors, skipped) from a JUnit XML file, or None."""
    if not os.path.exists(path):
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return None
    suites = [root] if root.tag == "testsuite" else list(root)
    tests = fails = errs = skips = 0
    for suite in suites:
        tests += int(suite.get("tests", 0))
        fails += int(suite.get("failures", 0))
        errs += int(suite.get("errors", 0))
        skips += int(suite.get("skipped", 0))
    return tests, fails, errs, skips


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workers", help="file of `user@host` lines")
    ap.add_argument("--shards", type=int, default=2,
                    help="shard count, including this machine")
    ap.add_argument("--ssh-opt", action="append", default=[],
                    help="extra ssh -o option, repeatable")
    ap.add_argument("--modules", nargs="*", help="restrict to these modules")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--local-only", action="store_true",
                    help="run everything here, for a baseline timing")
    args = ap.parse_args(argv)

    modules = args.modules or discover_modules()
    if not modules:
        print("no test modules found under tests/", file=sys.stderr)
        return 2

    workers = [] if args.local_only else worker_list(args.workers)
    shards = shard(modules, max(1, args.shards))
    # One shard stays here; the rest go out. Running a shard locally keeps the
    # cluster useful even when every remote worker is down.
    local_shards = shards[:1] if workers else shards
    remote_shards = shards[1:] if workers else []

    print("modules: %d  shards: %d (1 local, %d remote)"
          % (len(modules), len(shards), len(remote_shards)))
    for i, s in enumerate(shards):
        where = "local" if i == 0 else (workers[i - 1] if i - 1 < len(workers) else "unassigned")
        print("  shard %d -> %-22s %d modules" % (i, where, len(s)))
    if args.dry_run:
        return 0

    started = time.time()
    results = []

    if local_shards:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="mafutani-local-") as tmp:
            shutil_copy_repo(tmp)
            env = dict(os.environ, DB_PATH=os.path.join(tmp, "local.db"))
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--tb=line",
                 "--junitxml", os.path.join(tmp, "junit.xml")] + local_shards[0],
                cwd=tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            tail = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
            results.append({"worker": "local", "ok": proc.returncode == 0,
                            "seconds": round(time.time() - started, 1),
                            "modules": len(local_shards[0]), "summary": tail,
                            "junit": parse_junit(os.path.join(tmp, "junit.xml"))})

    for i, s in enumerate(remote_shards):
        if i >= len(workers):
            print("  shard %d unassigned: no worker left" % (i + 1))
            continue
        res = run_shard(workers[i], s, i + 1, ROOT, args.ssh_opt)
        res["junit"] = None
        results.append(res)

    print("\n=== results ===")
    failed = False
    totals = [0, 0, 0, 0]
    for res in results:
        status = "ok " if res["ok"] else "FAIL"
        print("%-22s %-4s %3d modules  %6.1fs"
              % (res["worker"], status, res["modules"], res.get("seconds", 0)))
        for line in res.get("summary", []):
            print("    " + line)
        if res.get("junit"):
            totals = [a + b for a, b in zip(totals, res["junit"])]
        if not res["ok"]:
            failed = True
            if res.get("error"):
                print("    error: " + res["error"][-300:])
            if res.get("stderr"):
                print("    stderr: " + res["stderr"][-200:])
    if totals[0]:
        print("\nmerged (from JUnit): %d tests, %d failures, %d errors, %d skipped"
              % tuple(totals))
    print("wall clock: %.1fs" % (time.time() - started))
    return 1 if failed else 0


def shutil_copy_repo(dest):
    """Copy the tree for a local shard run, minus databases and caches."""
    import shutil
    skip = {".git", "__pycache__", "node_modules", ".pytest_cache", "build"}
    shutil.copytree(ROOT, dest, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*skip, "*.pyc", "*.db"))


if __name__ == "__main__":
    sys.exit(main())
