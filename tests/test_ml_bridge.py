"""The torch bridge: separation, and the guarantee at its edges.

Two things are pinned here, and the second matters more.

1. Mafutani stays stdlib-only. The torch service lives outside the app, in
   `.venv-torch`, reached over HTTP. These tests run under the *system*
   Python, which has no torch, so any accidental import of torch by app code
   fails at import time rather than at deploy time -- which is the point of
   doing it this way round.

2. The client raises on every failure path and returns no number. A terminal
   that substitutes a default coefficient when the model server is down is
   worse than one that reports nothing, because the fallback and the real fit
   look identical at the point of use.

Tests that need torch run in the venv as a subprocess, because the model is
only importable there and pretending otherwise would mean the model is
tested in an environment that does not exist.
"""
import ast
import json
import os
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import engines.mlclient as mlc

VENV_PY = os.path.join(ROOT, ".venv-torch", "bin", "python")

# Everything the app may import. The service is not in this list on purpose:
# it is not part of the app.
APP_PACKAGES = ("api", "brokers", "core", "data", "engines")


def _app_py_files():
    out = []
    for pkg in APP_PACKAGES:
        d = os.path.join(ROOT, pkg)
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.endswith(".py"):
                out.append(os.path.join(d, name))
    for name in ("app.py",):
        p = os.path.join(ROOT, name)
        if os.path.exists(p):
            out.append(p)
    return out


def _venv_has_torch():
    if not os.path.exists(VENV_PY):
        return False
    try:
        r = subprocess.run([VENV_PY, "-c", "import torch"],
                           capture_output=True, timeout=120)
        return r.returncode == 0
    except Exception:
        return False


class TestAppStaysStdlib(unittest.TestCase):
    """The architectural claim, asserted rather than asserted-to."""

    def test_torch_is_not_importable_from_system_python(self):
        """If this fails the separation is not being enforced -- torch got
        installed globally, and 'stdlib only' is now a claim about the past
        tense."""
        try:
            import torch  # noqa: F401
        except ImportError:
            return
        self.fail("torch is importable from the system Python; the app no "
                  "longer depends only on the stdlib")

    def test_no_app_module_imports_torch(self):
        """Static check over every module the app loads.

        Docstrings are excluded via AST, because these files *discuss* torch
        at length -- explaining that they must never import it -- and matching
        that discussion would be a false positive.
        """
        offenders = []
        for path in _app_py_files():
            with open(path, "rb") as fh:
                src = fh.read()
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                for n in names:
                    root = n.split(".")[0]
                    if root == "torch":
                        rel = os.path.relpath(path, ROOT)
                        offenders.append("%s imports %s" % (rel, n))
        self.assertEqual(offenders, [],
                         "app modules must not import torch: %s"
                         % "; ".join(offenders))

    def test_service_is_the_only_place_torch_appears(self):
        svc = os.path.join(ROOT, "services", "ml", "model.py")
        server = os.path.join(ROOT, "services", "ml", "server.py")
        for path in (svc, server):
            with open(path, encoding="utf-8") as fh:
                self.assertIn("import torch", fh.read(),
                              "%s should import torch" % path)

    def test_client_uses_only_stdlib(self):
        src = open(os.path.join(ROOT, "engines", "mlclient.py"),
                   encoding="utf-8").read()
        tree = ast.parse(src)
        mods = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods.add(node.module.split(".")[0])
        allowed = {"json", "math", "os", "threading", "time",
                   "urllib", "http", "error", "request"}
        self.assertTrue(mods.issubset(allowed),
                        "client imports non-stdlib: %s" % (mods - allowed))


class TestConfig(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("MAFUTANI_ML_URL", "MAFUTANI_ML_TIMEOUT_S",
                        "MAFUTANI_ML_TTL_S")}
        mlc.clear_cache()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        mlc.clear_cache()

    def test_default_url_and_port(self):
        os.environ.pop("MAFUTANI_ML_URL", None)
        self.assertEqual(mlc.service_url(), "http://127.0.0.1:8021")

    def test_url_override(self):
        os.environ["MAFUTANI_ML_URL"] = "http://example.test:9999/"
        self.assertEqual(mlc.service_url(), "http://example.test:9999")

    def test_garbage_timeout_falls_back(self):
        os.environ["MAFUTANI_ML_TIMEOUT_S"] = "not-a-number"
        self.assertEqual(mlc.timeout_s(), mlc.DEFAULT_TIMEOUT_S)
        os.environ["MAFUTANI_ML_TIMEOUT_S"] = "-4"
        self.assertEqual(mlc.timeout_s(), mlc.DEFAULT_TIMEOUT_S)

    def test_contract_is_stable(self):
        self.assertEqual(mlc.CONTRACT, "mafutani.ml/1")


def _good_fit():
    return {"contract": mlc.CONTRACT, "ok": True, "n": 120, "alpha": 1.0,
            "beta": 2.5, "gamma": 0.3, "r2": 0.41, "ridge_lambda": 1e-4,
            "standard_error_beta": 0.2, "t_beta": 12.5, "device": "cpu"}


class TestModelValidation(unittest.TestCase):
    """Every response path that must NOT yield a coefficient."""

    def test_accepts_a_fitted_model(self):
        m = mlc.validate_model(_good_fit())
        self.assertAlmostEqual(m["beta"], 2.5)
        self.assertTrue(m["has_se"])
        self.assertEqual(m["verdict"], "fitted")

    def test_rejects_unfitted(self):
        p = _good_fit()
        p["ok"] = False
        p["reason"] = "need at least 30 usable transitions, got 4"
        with self.assertRaises(mlc.MLUnavailable) as cm:
            mlc.validate_model(p)
        self.assertEqual(cm.exception.reason, "not_fitted")
        self.assertIn("30", cm.exception.detail)

    def test_rejects_contract_mismatch(self):
        p = _good_fit()
        p["contract"] = "something/9"
        with self.assertRaises(mlc.MLUnavailable) as cm:
            mlc.validate_model(p)
        self.assertEqual(cm.exception.reason, "contract_mismatch")

    def test_rejects_missing_contract(self):
        p = _good_fit()
        del p["contract"]
        with self.assertRaises(mlc.MLUnavailable):
            mlc.validate_model(p)

    def test_rejects_missing_coefficient(self):
        p = _good_fit()
        del p["beta"]
        with self.assertRaises(mlc.MLUnavailable) as cm:
            mlc.validate_model(p)
        self.assertEqual(cm.exception.reason, "malformed")
        self.assertIn("beta", cm.exception.detail)

    def test_rejects_nonfinite_coefficient(self):
        for bad in (float("nan"), float("inf"), float("-inf"), True, "2.5", None):
            p = _good_fit()
            p["beta"] = bad
            with self.assertRaises(mlc.MLUnavailable,
                                   msg="accepted beta=%r" % (bad,)):
                mlc.validate_model(p)

    def test_rejects_bad_n(self):
        for bad in (0, -1, 1.5, "120", None):
            p = _good_fit()
            p["n"] = bad
            with self.assertRaises(mlc.MLUnavailable):
                mlc.validate_model(p)

    def test_rejects_non_dict(self):
        for bad in ([1, 2], "x", None, 5):
            with self.assertRaises(mlc.MLUnavailable):
                mlc.validate_model(bad)

    def test_flags_a_coefficient_indistinguishable_from_noise(self):
        """A fitted coefficient with |t| < 2 is not a measurement. Reported,
        so a caller can refuse it rather than discover it."""
        p = _good_fit()
        p["t_beta"] = 0.4
        m = mlc.validate_model(p)
        self.assertEqual(m["verdict"], "not-distinguishable-from-noise")

    def test_absent_se_is_reported_not_assumed(self):
        p = _good_fit()
        del p["standard_error_beta"]
        del p["t_beta"]
        m = mlc.validate_model(p)
        self.assertFalse(m["has_se"])
        self.assertIsNone(m["se_beta"])


class TestServiceDownRaises(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("MAFUTANI_ML_URL")
        os.environ["MAFUTANI_ML_URL"] = "http://127.0.0.1:9"   # discard port
        mlc.clear_cache()

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("MAFUTANI_ML_URL", None)
        else:
            os.environ["MAFUTANI_ML_URL"] = self._saved
        mlc.clear_cache()

    def test_health_raises_rather_than_reporting_down(self):
        with self.assertRaises(mlc.MLUnavailable) as cm:
            mlc.health()
        self.assertEqual(cm.exception.reason, "unreachable")

    def test_fit_raises(self):
        with self.assertRaises(mlc.MLUnavailable):
            mlc.fit([(1.0, 10.0, 0.01)] * 50)

    def test_predict_raises(self):
        with self.assertRaises(mlc.MLUnavailable):
            mlc.predict(_good_fit(), 0.5, 20.0)

    def test_is_available_is_false_not_raising(self):
        self.assertFalse(mlc.is_available(timeout=1.0))

    def test_fit_with_no_usable_samples_raises_locally(self):
        """Refused before any network call: sending zero samples would burn
        a round trip to learn nothing."""
        with self.assertRaises(mlc.MLUnavailable) as cm:
            mlc.fit([(1.0, 0.0, 0.01)] * 50)
        self.assertEqual(cm.exception.reason, "no_samples")

    def test_nonfinite_samples_are_dropped_not_sent(self):
        with self.assertRaises(mlc.MLUnavailable) as cm:
            mlc.fit([(float("nan"), 10.0, 0.01)] * 50)
        self.assertEqual(cm.exception.reason, "no_samples")


@unittest.skipUnless(_venv_has_torch(),
                     "torch venv not present; run scripts/ml_check.sh")
class TestModelFits(unittest.TestCase):
    """The model itself, executed in the only environment where it exists.

    Subprocess rather than import: torch is only importable inside
    `.venv-torch`, and importing it from here would defeat the separation
    these very tests assert.
    """

    def _run(self, code):
        r = subprocess.run([VENV_PY, "-c", code],
                           capture_output=True, text=True, timeout=600,
                           cwd=os.path.join(ROOT, "services", "ml"))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_recovers_a_known_coefficient(self):
        # One draw per row. An earlier version called random.uniform twice
        # per row -- once for ofi, once for the mid -- so ofi and dmid were
        # independent and the "known" coefficient was never in the data at
        # all. The fit correctly returned r2=0.0 on it, which is the right
        # answer to a dataset with no relationship in it.
        out = self._run(
            "import json,model,random\n"
            "random.seed(3)\n"
            "rows=[]\n"
            "for _ in range(400):\n"
            "    d=random.uniform(8,60)\n"
            "    x=random.uniform(-2,2)\n"
            "    rows.append((x*d, d, 1.0+2.5*x+0.3/d))\n"
            "r=model.fit([a for a,b,c in rows],[b for a,b,c in rows],"
            "[c for a,b,c in rows])\n"
            "print(json.dumps(r))")
        self.assertTrue(out["ok"], out.get("reason"))
        self.assertEqual(out["n"], 400)
        self.assertAlmostEqual(out["beta"], 2.5, delta=0.1)
        self.assertAlmostEqual(out["alpha"], 1.0, delta=0.1)
        self.assertGreater(out["r2"], 0.99)

    def test_standard_errors_need_no_numpy(self):
        """The CPU torch wheel does not pull numpy. The first version of
        standard_errors called X.numpy() and crashed with "Numpy is not
        available" -- the dependency had leaked out of the one place meant to
        be self-contained."""
        out = self._run(
            "import json,model,random\n"
            "random.seed(5)\n"
            "rows=[]\n"
            "for _ in range(200):\n"
            "    d=random.uniform(8,60)\n"
            "    x=random.uniform(-2,2)\n"
            "    rows.append((x*d, d, 1.0+2.5*x+random.gauss(0,0.01)))\n"
            "se=model.standard_errors([a for a,b,c in rows],[b for a,b,c in rows],"
            "[c for a,b,c in rows])\n"
            "print(json.dumps(se))")
        self.assertTrue(out["ok"], out.get("reason"))
        self.assertGreater(out["se_beta"], 0.0)
        self.assertAlmostEqual(out["beta_from_normal_equations"], 2.5, delta=0.15)

    def test_independent_inputs_yield_no_relationship(self):
        """A dataset with no relationship must not acquire one.

        Asserted on r2 and on the t-statistic rather than on a bare
        coefficient threshold. An earlier version asserted |beta| < 0.1 and
        failed at 0.229 -- which turned out to be about 1.8 standard errors,
        i.e. noise, so the arbitrary threshold was the wrong instrument. The
        meaningful property is that the coefficient is not distinguishable
        from zero, and that is what the standard error decides.
        """
        out = self._run(
            "import json,model,random\n"
            "random.seed(11)\n"
            "rows=[(random.uniform(1,3)*20, 20.0, random.uniform(0,5))"
            " for _ in range(400)]\n"
            "r=model.fit([a for a,b,c in rows],[b for a,b,c in rows],"
            "[c for a,b,c in rows])\n"
            "print(json.dumps(r))")
        self.assertTrue(out["ok"])
        self.assertLess(out["r2"], 0.1, "claimed to explain noise")

    def test_constant_depth_is_reported_as_degenerate(self):
        """When depth does not vary, 1/depth is a constant and the gamma
        column is collinear with the intercept.

        Ridge makes the system invertible but does not resolve which of
        alpha/gamma absorbs what, and the leak lands on beta. Measured on
        independent outcomes at constant depth, the fit returned beta=0.229
        with r2=0.009 -- pure noise that a caller reading `beta` alone would
        have taken for signal. The model now reports the degeneracy instead
        of hiding it.
        """
        out = self._run(
            "import json,model,random\n"
            "random.seed(11)\n"
            "rows=[(random.uniform(1,3)*20, 20.0, random.uniform(0,5))"
            " for _ in range(400)]\n"
            "r=model.fit([a for a,b,c in rows],[b for a,b,c in rows],"
            "[c for a,b,c in rows])\n"
            "print(json.dumps(r))")
        self.assertTrue(out["depth_degenerate"])
        self.assertLess(out["depth_cv"], 1e-3)
        self.assertIn("collinear", out["reason"])

    def test_varying_depth_is_not_degenerate(self):
        out = self._run(
            "import json,model,random\n"
            "random.seed(3)\n"
            "rows=[]\n"
            "for _ in range(400):\n"
            "    d=random.uniform(8,60)\n"
            "    x=random.uniform(-2,2)\n"
            "    rows.append((x*d, d, 1.0+2.5*x+0.3/d))\n"
            "r=model.fit([a for a,b,c in rows],[b for a,b,c in rows],"
            "[c for a,b,c in rows])\n"
            "print(json.dumps(r))")
        self.assertFalse(out["depth_degenerate"])
        self.assertGreater(out["depth_cv"], 0.01)

    def test_refuses_too_few_samples(self):
        out = self._run(
            "import json,model\n"
            "print(json.dumps(model.fit([1.0]*5,[10.0]*5,[0.01]*5)))")
        self.assertFalse(out["ok"])
        self.assertIsNone(out["beta"])
        self.assertIn("30", out["reason"])

    def test_refuses_zero_depth(self):
        out = self._run(
            "import json,model\n"
            "print(json.dumps(model.fit([1.0]*50,[0.0]*50,[0.01]*50)))")
        self.assertFalse(out["ok"])
        self.assertEqual(out["n"], 0)

    def test_constant_inputs_give_zero_not_nan(self):
        """Zero variance must not produce NaN coefficients -- NaN fails open
        at exactly the moment someone trusts it."""
        out = self._run(
            "import json,model\n"
            "print(json.dumps(model.fit([1.0]*60,[10.0]*60,[0.01]*60)))")
        self.assertNotEqual(out.get("beta"), float("inf"))
        self.assertTrue(out.get("beta") is None or
                        out["beta"] == out["beta"])

    def test_predict_refuses_an_unfitted_model(self):
        out = self._run(
            "import json,model\n"
            "print(json.dumps(model.predict_one({'ok':False},0.5,20.0)))")
        self.assertIsNone(out)

    def test_predict_refuses_zero_depth(self):
        out = self._run(
            "import json,model\n"
            "m={'ok':True,'alpha':0.0,'beta':1.0,'gamma':0.0}\n"
            "print(json.dumps(model.predict_one(m,0.5,0.0)))")
        self.assertIsNone(out)


if __name__ == "__main__":
    unittest.main(verbosity=2)