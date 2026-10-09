"""Broker registry: name -> adapter factory.

One place that knows which brokers exist, so the API layer, the terminal
and the UI can all resolve a broker by name without importing adapter
classes directly.
"""

from .base import (Broker, BrokerError, BrokerNotSupported, Credentials,
                   BrokerValidationError, live_trading_enabled)
from . import profiles as _profiles
from .binance import BinanceBroker
from .paper import PaperBroker
from .rest import RestBroker

# name -> (builder, description)
_REGISTRY = {}


def register(name, builder, description=""):
    """Register a broker factory under `name`."""
    _REGISTRY[str(name)] = (builder, description)
    return builder


def _build_paper(**kw):
    kw.pop("credentials", None)
    kw.pop("profile", None)
    # The paper broker is simulated by definition. Refusing `live=True`
    # here means a caller cannot construct a "live paper" broker and
    # mistake simulated fills for real ones.
    if kw.pop("live", False):
        raise BrokerValidationError(
            "the paper broker is simulated and cannot be live; "
            "choose a venue profile for live trading")
    return PaperBroker(**kw)


def _build_rest(profile=None, **kw):
    profile = profile or "bearer_style"
    creds = kw.pop("credentials", None)
    live = bool(kw.pop("live", False))
    confirm = bool(kw.pop("confirm", False))
    tier_live = kw.pop("tier_live_enabled", None)
    transport = kw.pop("transport", None)
    return RestBroker(profile=profile, credentials=creds, transport=transport,
                      live=live, confirm=confirm,
                      tier_live_enabled=tier_live, **kw)


def _rest_builder_for(profile_name):
    """Bind a profile name into a builder.

    Without this the registry name is never seen by the builder, so every
    REST broker silently built the *default* profile -- asking for
    `binance_style` and quietly getting `bearer_style`.
    """
    def _builder(**kw):
        kw.pop("profile", None)
        return _build_rest(profile=profile_name, **kw)
    return _builder


def _build_binance(**kw):
    # credentials arrive already resolved by get_broker() from
    # BROKER_BINANCE_API_KEY / _SECRET, so they are passed through rather
    # than dropped -- popping them here would silently build an adapter
    # that can never sign, and the failure would surface as a confusing
    # "no credentials" at order time instead of at setup.
    kw.pop("profile", None)
    return BinanceBroker(**kw)


register("paper", _build_paper,
         "Simulated execution against the local database (default)")
# The one venue that can actually execute. Registered under its real name
# rather than as another "style" profile because it needs per-venue signing
# logic that endpoint data cannot describe -- see brokers/binance.py.
register("binance", _build_binance,
         "Binance spot: HMAC-signed live execution (set BROKER_BINANCE_API_KEY/SECRET)")
for _p in _profiles.PROFILES:
    register(_p, _rest_builder_for(_p),
             "REST adapter for the '{}' venue shape".format(_p))


def available():
    return sorted(_REGISTRY)


def describe(name=None):
    """Names + descriptions, for an API/UI listing."""
    if name:
        b, d = _lookup(name)
        return {"name": name, "description": d}
    return [{"name": n, "description": d} for n, (b, d) in
            sorted(_REGISTRY.items())]


def _lookup(name):
    key = str(name or "paper").strip().lower()
    if key not in _REGISTRY:
        raise BrokerNotSupported(
            "unknown broker '{}'; have: {}".format(name, ", ".join(available())))
    return _REGISTRY[key]


def env_prefix_for(name):
    """Environment prefix holding a venue's credentials.

    `binance_style` -> `BROKER_BINANCE_STYLE_`, so a deployment can hold
    several venues at once. Normalised defensively: venue names may be
    written with dashes, dots or spaces, all of which become `_`.

    Uppercase, as this docstring has always claimed. The implementation
    used to emit `BROKER_binance_style_`, which meant following the
    documented setup silently produced no credentials at all -- the
    adapter came up unconfigured and the real failure only appeared at
    order time as "no credentials". `resolve_credentials` still accepts
    the old lowercase form so a deployment that adapted to the bug is not
    stranded.
    """
    key = _norm(name)
    return "BROKER_{}_".format(key.upper()) if key else "BROKER_"


def _norm(name):
    return "".join(ch if ch.isalnum() else "_"
                   for ch in str(name or "").strip().lower()).strip("_")


def resolve_credentials(name=None, explicit=None):
    """Credentials for a venue: explicit, then per-venue env, then generic.

    Resolution order for venue `binance_style`:
      1. an explicit `Credentials` passed by the caller
      2. `BROKER_BINANCE_STYLE_API_KEY` / `_SECRET` / `_PASSPHRASE`
         / `_EXTRA_*`
      3. the same names lowercased (`BROKER_binance_style_...`), which is
         what `env_prefix_for` used to emit before it was corrected
      4. the generic `BROKER_API_KEY` / ... (single-venue deployments)

    Per-venue wins over generic so a multi-venue setup cannot have one
    venue silently borrow another's key. Returns None when nothing is
    configured, which callers must treat as "unconfigured" rather than
    "empty credentials" -- a venue with no key is not a venue that
    authenticated as nobody.
    """
    if explicit is not None:
        return explicit
    if _norm(name):
        key = _norm(name)
        for prefix in ("BROKER_{}_".format(key.upper()),
                       "BROKER_{}_".format(key)):
            creds = Credentials.from_env(prefix)
            if creds is not None:
                return creds
    return Credentials.from_env("BROKER_")


def credential_status(name):
    """Non-secret description of a venue's credential configuration.

    Reports only whether credentials are present, which env prefix they
    came from, and a non-reversible fingerprint -- enough for an operator
    to confirm *which* key is wired up without the value ever leaving the
    process. `fingerprint()` is a truncated SHA-256, not the key.
    """
    creds = resolve_credentials(name)
    return {
        "venue": str(name),
        "configured": creds is not None,
        "env_prefix": env_prefix_for(name),
        # Truncated SHA-256 of the key material; identifies, never discloses.
        "fingerprint": creds.fingerprint() if creds is not None else None,
        "has_api_key": bool(creds and creds.get("api_key")),
        "has_secret": bool(creds and creds.get("secret")),
        "has_passphrase": bool(creds and creds.get("passphrase")),
    }


def get_broker(name="paper", credentials=None, **config):
    """Build a broker by name.

    Credentials are resolved per venue (see :func:`resolve_credentials`),
    so a deployment can point several venues at several keys at once.
    """
    builder, _desc = _lookup(name)
    if str(name).strip().lower() == "paper":
        credentials = None
    elif credentials is None:
        credentials = resolve_credentials(name)
    return builder(credentials=credentials, **config)
