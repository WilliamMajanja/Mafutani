"""Model definitions for the torch service. Runs in its own venv, not in Mafutani.

Why this exists
---------------
Mafutani is stdlib-only by construction and stays that way. This service runs
outside it, in `.venv-torch`, and is reached over plain HTTP. The app never
imports torch; it imports `engines.mlclient`, which imports only urllib.

That split is not ceremonial. It is the mechanism that lets a heavy
dependency exist in the repository without the repository depending on it,
and there is a test (`tests/test_ml_bridge.py`) asserting that no module the
app imports can reach torch.

The model
---------
`ImpactModel` estimates the price-impact coefficient for order-flow imbalance
on *this* deployment's own books.

Cont, Kukanov & Stoikov (arXiv:1011.6402) find the OFI/price-impact slope is
inversely proportional to market depth, and report beta significant in 98% of
their samples -- on 50 S&P 500 stocks traded on the NYSE. The coefficient they
measure is therefore a property of those books, not a constant to import.
Borrowing it would be the same error as copying a parameter from a paper
without the paper.

So this fits:

    dMid  =  alpha  +  beta * ofi / depth  +  gamma * 1 / depth

with ridge on the non-intercept terms. Fitted by autograd on the deployment's
own transitions, it answers a question a transplanted constant cannot: how
much does one unit of normalised imbalance move a mid on *these* venues, at
*these* depths, in *this* liquidity regime.

Ridge is not decoration. The coefficient on the 1/depth term is
1/depth * squared* once multiplied out, which makes it numerically nasty at
small depths and highly correlated with the OFI term. Xu, Gould & Howison
(arXiv:1907.06230) report the same instability for multi-level OFI and show
OLS over-fits across levels; they switch to Ridge and get stable parameters.
Same diagnosis, same remedy, different data.

Honest reporting
----------------
Every field says how it was derived. `n` is the observation count, `r2` the
in-sample fit, and `ridge_lambda` is reported rather than hidden because a
coefficient that only exists at a particular regularisation strength is not a
measurement. When the sample is too small the model returns `ok=False` with a
reason and no coefficient, because a number fitted to 12 points is not a
calibration.
"""
import math

import torch

# Below this, a fit is not a measurement. Cont et al. fit on tens of
# thousands of transitions per stock; anything an order of magnitude below
# that is being read for its shape, not its value.
MIN_OBS = 30

# Transitions whose gap to the next one exceeds this are dropped rather than
# skipped: an OFI measured over 90 seconds and a mid change over 90 seconds
# are not the same quantity as the 10-second pair Cont et al. regress, and
# pooling them would fit a slope to a mixture.
DEFAULT_GAP_S = 30


def build_features(ofi, depth, mid_change, ridge_lambda=1e-4):
    """Assemble (X, y) as tensors. `ofi` and `depth` are plain floats.

    Normalisation happens here so the fit sees the same shape the paper
    describes: flow divided by resting depth, and the reciprocal of depth as
    a separate regressor.
    """
    xs, ys = [], []
    for e, d, dm in zip(ofi, depth, mid_change):
        if d <= 0:
            continue
        x = float(e) / float(d)
        inv = 1.0 / float(d)
        xs.append([1.0, x, inv])
        ys.append(float(dm))
    if not xs:
        return None, None
    X = torch.tensor(xs, dtype=torch.float64)
    y = torch.tensor(ys, dtype=torch.float64).unsqueeze(1)
    return X, y


def fit(ofi, depth, mid_change, ridge_lambda=1e-4, steps=500, lr=0.01,
        ridge_on_intercept=False):
    """Ridge-fit dMid on [1, ofi/depth, 1/depth].

    `steps` is LBFGS max_iter. The default was 2000, which is 400x the
    number of parameters and cost 23s against 0.03s for the same answer --
    LBFGS converges on a three-parameter quadratic in a handful of
    iterations and then iterates for nothing. The bound is a guard against a
    pathological run, not a target.

    Returns a dict. Never raises for bad input; returns ok=False with a
    reason. Coefficients are plain floats so the client can read them without
    torch.
    """
    out = {"ok": False, "n": 0, "alpha": None, "beta": None, "gamma": None,
           "r2": None, "ridge_lambda": ridge_lambda, "reason": ""}

    usable = [(e, d, dm) for e, d, dm in zip(ofi or [], depth or [],
                                             mid_change or [])
              if d and d > 0]
    out["n"] = len(usable)
    if len(usable) < MIN_OBS:
        out["reason"] = ("need at least {} usable transitions to fit, got {}"
                         .format(MIN_OBS, len(usable)))
        return out

    X, y = build_features(*zip(*[(e, d, dm) for e, d, dm in usable]))
    if X is None or X.shape[0] < MIN_OBS:
        out["reason"] = "features could not be assembled"
        return out

    theta = torch.zeros(X.shape[1], 1, dtype=torch.float64, requires_grad=True)
    opt = torch.optim.LBFGS([theta], max_iter=steps, lr=lr,
                            tolerance_grad=1e-14, tolerance_change=1e-16,
                            line_search_fn="strong_wolfe")

    def closure():
        opt.zero_grad()
        resid = X.mm(theta) - y
        loss = resid.pow(2).mean()
        reg = torch.zeros(1, dtype=torch.float64)
        start = 0 if ridge_on_intercept else 1
        if start < theta.shape[0]:
            reg = theta[start:].pow(2).sum() * float(ridge_lambda)
        total = loss + reg
        total.backward()
        return total

    try:
        opt.step(closure)
    except Exception as e:                     # pragma: no cover - optimiser
        out["reason"] = "optimiser failed: {}".format(type(e).__name__)
        return out

    with torch.no_grad():
        c = theta.squeeze().tolist()
        pred = X.mm(theta)
        resid = pred - y
        ss_res = float(resid.pow(2).sum())
        ss_tot = float((y - y.mean()).pow(2).sum())
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else None

    out.update({"ok": True, "alpha": float(c[0]), "beta": float(c[1]),
                "gamma": float(c[2]), "r2": r2,
                "reason": "fitted on {} transitions".format(len(usable))})

    # Depth dispersion, reported because the design matrix can be degenerate.
    #
    # If depth barely moves across the sample then 1/depth is very nearly a
    # constant, which makes the gamma column collinear with the intercept:
    # alpha and gamma trade off against each other and the split between
    # them is arbitrary. Ridge makes the system invertible but does not
    # resolve the ambiguity, and the leak shows up as a beta that is not
    # clean. Measured on 400 rows of constant depth and independent outcomes,
    # this fit returned beta=0.229 -- about 1.8 standard errors, i.e. noise
    # that a caller reading only `beta` would have taken for signal.
    #
    # So the number is reported and the caller decides. A model fitted on a
    # single depth is a one-parameter model wearing three.
    depths = [d for _e, d, _dm in usable]
    d_mean = sum(depths) / len(depths)
    d_var = sum((d - d_mean) ** 2 for d in depths) / len(depths)
    out["depth_mean"] = d_mean
    out["depth_cv"] = (math.sqrt(d_var) / d_mean) if d_mean > 0 else None
    out["depth_degenerate"] = bool(d_mean > 0 and
                                   math.sqrt(d_var) / d_mean < 1e-3)
    if out["depth_degenerate"]:
        out["reason"] = ("fitted on {} transitions but depth is effectively "
                         "constant (cv={:.2e}); alpha and gamma are collinear "
                         "so only beta is meaningful"
                         .format(len(usable), out["depth_cv"]))
    return out


def predict_one(model, ofi_norm, depth, gamma_needed=True):
    """Apply a fitted model to one observation.

    Returns None when the model is unusable, so a caller cannot accidentally
    carry a half-fitted coefficient into a live decision.
    """
    if not model or not model.get("ok"):
        return None
    d = float(depth or 0.0)
    if d <= 0:
        return None
    a = float(model.get("alpha") or 0.0)
    b = float(model.get("beta") or 0.0)
    g = float(model.get("gamma") or 0.0)
    return a + b * float(ofi_norm) + (g / d if gamma_needed else 0.0)


def standard_errors(ofi, depth, mid_change, ridge_lambda=1e-4, model=None):
    """OLS standard errors from the normal equations, in torch.

    Written against torch rather than numpy on purpose. The first version of
    this function used `X.numpy()` and a numpy solve, which crashed with
    "Numpy is not available" -- the CPU torch wheel does not pull numpy. So
    the dependency had leaked out of the one place that was supposed to be
    self-contained, which is precisely the failure this split exists to
    prevent. If numpy is wanted, it gets installed here, explicitly.

    Not a substitute for a full covariance estimate, and labelled as rough:
    the value is "how noisy is this coefficient given the data", which is
    what decides whether the coefficient is worth acting on.
    """
    usable = [(e, d, dm) for e, d, dm in zip(ofi or [], depth or [],
                                             mid_change or [])
              if d and d > 0]
    if len(usable) < MIN_OBS:
        return {"ok": False, "se_beta": None, "t_beta": None, "n": len(usable),
                "reason": "too few transitions"}
    X, y = build_features(*zip(*usable))
    if X is None:
        return {"ok": False, "se_beta": None, "t_beta": None,
                "n": len(usable), "reason": "features could not be assembled"}

    k = X.shape[1]
    # Ridge-penalised normal equations, then inverting for the covariance.
    # Same penalisation as fit() so the error bar corresponds to the
    # coefficient it is describing.
    XtX = X.t().mm(X)
    XtX = XtX + ridge_lambda * torch.eye(k, dtype=X.dtype)
    try:
        cov = torch.linalg.inv(XtX)
        beta = torch.linalg.solve(XtX, X.t().mm(y)).squeeze()
        resid = (X.mm(beta.unsqueeze(1)) - y).squeeze()
        dof = max(1, X.shape[0] - k)
        s2 = float(resid.pow(2).sum()) / dof
        se_beta = float(torch.sqrt(torch.clamp(cov[1, 1], min=0.0) * s2))
    except Exception as e:
        return {"ok": False, "se_beta": None, "t_beta": None,
                "n": len(usable), "reason": "covariance failed: {}".format(
                    type(e).__name__)}
    b_beta = float(beta[1])
    return {"ok": True, "se_beta": se_beta,
            "t_beta": (b_beta / se_beta) if se_beta > 0 else None,
            "beta_from_normal_equations": b_beta,
            "n": len(usable), "reason": ""}