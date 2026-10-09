"""Agentic trading runtime. Stdlib only, no RNG, fail-closed.

The design question this module answers is not "how do I let a bot
trade?" but "what is the minimum an autonomous actor needs before its
proposal is allowed to become an order?"

An agent here is deliberately *not* an auto-trader. The loop is:

    observe -> form a signal -> adversarial review -> gates -> propose

and a proposal stops there. Executing it is a separate, explicit,
per-mandate decision (`auto_execute`), which is off by default and is
still subject to the platform's own risk engine at submission time.

Three properties are load-bearing:

1.  **The dissenter is independent of the proposer.** A signal that was
    going to fire anyway is not re-litigated by the code that produced
    it. The dissenter runs adversarial checks the proposer structurally
    cannot perform on itself -- it can see that a rule fired on thin
    support, but not that the features are stale, because staleness is
    invisible from inside a rule evaluation. Objections are recorded and
    they *demote* the proposal; they do not silently rewrite it.

2.  **Gates fail closed.** A gate that raises, or that is missing, blocks
    the action. An exception in a risk check is not permission.

3.  **Restraint is logged, not just prevented.** Every suppressed action
    is recorded with a reason. A system that only logs what it did
    cannot be evaluated: the refusals are the evidence that the gates
    are real rather than decorative.

Determinism: no RNG, no wall-clock in scoring decisions, no network.
`tick()` on the same candles yields the same proposals.
"""

import time

try:
    from brokers.gods import Dissenter, VetoGate, RestraintLog
    from brokers.godmode import compute_god_features
    from brokers.gods import GodsError
except ImportError:  # pragma: no cover - direct-path execution
    from gods import Dissenter, VetoGate, RestraintLog
    from godmode import compute_god_features
    from gods import GodsError

# Agent lifecycle. A mandate is created stopped; nothing runs until an
# operator starts it, so importing this module never moves a market.
STATES = ("draft", "running", "paused", "halted", "retired")

DEFAULT_MAX_ACTIONS_PER_HOUR = 12
DEFAULT_MAX_NOTIONAL = 5000.0
# How many bars of history an agent needs before it may propose at all.
# Below this the god features are not converged and any signal is noise.
MIN_BARS = 60


class MandateError(Exception):
    """Raised for a structurally invalid mandate."""


class Mandate(object):
    """The bounded authority granted to one agent.

    Everything an agent is permitted to do is enumerated here. Nothing
    is implied: a symbol that is not in `symbols` cannot be traded, a
    mandate with no `auto_execute` cannot place an order regardless of
    how confident the signal is.
    """

    def __init__(self, name, strategy="ema_trend", symbols=None,
                 max_notional=DEFAULT_MAX_NOTIONAL,
                 max_actions_per_hour=DEFAULT_MAX_ACTIONS_PER_HOUR,
                 auto_execute=False, mode="paper", params=None,
                 min_dissent_support=1, agent_id=None):
        self.agent_id = str(agent_id or name)
        self.name = str(name or "agent")
        self.strategy = str(strategy or "ema_trend")
        syms = symbols or []
        if isinstance(syms, str):
            syms = [s.strip() for s in syms.split(",") if s.strip()]
        self.symbols = [str(s).strip().upper() for s in syms if str(s).strip()]
        self.max_notional = _positive(max_notional, "max_notional")
        self.max_actions_per_hour = int(
            _positive(max_actions_per_hour, "max_actions_per_hour"))
        self.mode = str(mode or "paper").strip().lower()
        if self.mode not in ("paper", "live"):
            raise MandateError("mode must be paper or live")
        # auto_execute defaults False and can only be set explicitly.
        self.auto_execute = bool(auto_execute)
        self.params = dict(params or {})
        # How many dissenter objections demote a proposal to 'held'.
        # 0 means any objection holds it; higher tolerates more.
        self.min_dissent_support = int(min_dissent_support)
        self.state = "draft"
        self.created_at = int(time.time())
        self._action_times = []

    # -- authority checks ------------------------------------------------
    def may_trade(self, symbol):
        """True only if this mandate covers the symbol at all."""
        return str(symbol or "").strip().upper() in self.symbols

    def notional_ok(self, qty, price):
        try:
            n = abs(float(qty) * float(price))
        except (TypeError, ValueError):
            return False
        return n <= self.max_notional

    def rate_ok(self, now=None):
        """Sliding one-hour action budget."""
        now = float(now if now is not None else time.time())
        cutoff = now - 3600.0
        self._action_times = [t for t in self._action_times if t > cutoff]
        return len(self._action_times) < self.max_actions_per_hour

    def record_action(self, now=None):
        self._action_times.append(
            float(now if now is not None else time.time()))

    def to_dict(self):
        return {
            "agent_id": self.agent_id, "name": self.name,
            "strategy": self.strategy, "symbols": list(self.symbols),
            "max_notional": self.max_notional,
            "max_actions_per_hour": self.max_actions_per_hour,
            "actions_last_hour": len(self._action_times),
            "auto_execute": self.auto_execute, "mode": self.mode,
            "params": dict(self.params),
            "min_dissent_support": self.min_dissent_support,
            "state": self.state, "created_at": self.created_at,
        }


def _positive(v, field):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise MandateError("{} must be numeric".format(field))
    if f <= 0:
        raise MandateError("{} must be > 0".format(field))
    return f


class _FaultProxy(object):
    """Minimal stand-in used to log a tick fault in the restraint log.

    RestraintLog.record reads .ts/.symbol/.rule_id off its argument, so a
    bare dict raised AttributeError -- and because that happened inside
    the except handler, it took down the whole tick loop. Anything handed
    to the restraint log must be attribute-shaped.
    """

    __slots__ = ("ts", "symbol", "rule_id", "side", "qty", "price")

    def __init__(self, symbol, rule_id="agent.fault"):
        self.ts = int(time.time())
        self.symbol = str(symbol or "")
        self.rule_id = rule_id
        self.side = None
        self.qty = 0.0
        self.price = 0.0


class Proposal(object):
    """A proposed action plus the full reasoning chain behind it.

    The chain is retained so an operator can see *why* an agent wanted
    to trade, not just that it did. `outcome` is one of:
      proposed  survived review and gates, awaiting ratification
      held      dissenter objected, demoted for a human
      blocked   a gate refused it
      executed  an order was submitted (only when auto_execute)
    """

    __slots__ = ("agent_id", "symbol", "side", "qty", "price", "reason",
                 "outcome", "objections", "gate", "features", "ts",
                 "order_id", "notes", "rule_id")

    def __init__(self, agent_id, symbol, side, qty, price, reason,
                 rule_id="agent.proposal"):
        self.agent_id = agent_id
        self.symbol = symbol
        self.side = side
        self.qty = qty
        self.price = price
        self.reason = reason
        self.outcome = "proposed"
        self.objections = []
        self.gate = ""
        self.features = {}
        self.ts = int(time.time())
        self.order_id = None
        self.notes = []
        # RestraintLog.record reads signal.rule_id off the object, so it
        # has to exist. Passing a dict instead made every restraint entry
        # raise AttributeError, which then took down the tick loop too.
        self.rule_id = rule_id

    def to_dict(self):
        return {
            "agent_id": self.agent_id, "symbol": self.symbol,
            "side": self.side, "qty": self.qty, "price": self.price,
            "reason": self.reason, "outcome": self.outcome,
            "objections": list(self.objections), "gate": self.gate,
            "features": self.features, "ts": self.ts,
            "order_id": self.order_id, "notes": list(self.notes),
        }


class TradingAgent(object):
    """One bounded actor: a mandate plus the review chain it must pass."""

    def __init__(self, mandate, executor=None, dissenter=None,
                 risk_gate=None, compliance_gate=None):
        if mandate is None:
            raise MandateError("agent requires a mandate")
        self.mandate = mandate
        # executor(conn, proposal) -> order_id or raises. Injected so the
        # runtime never reaches for a database or a network on its own.
        self.executor = executor
        self.dissenter = dissenter or AgentDissenter()
        self.gate = VetoGate("risk", risk_gate or _notional_gate)
        self.compliance = VetoGate("compliance", compliance_gate or
                                   _allowlist_gate)
        self.restraint = RestraintLog()
        self.proposals = []          # recent proposals, newest last
        self.stats = {
            "ticks": 0, "proposed": 0, "held": 0, "blocked": 0,
            "executed": 0, "no_signal": 0, "errors": 0,
        }

    # -- observation -----------------------------------------------------
    def observe(self, candles, symbol, quote=None):
        """Feature vector for one symbol, or {} when there is not enough
        history for the features to mean anything."""
        try:
            closes = [float(c.get("c") or c.get("close") or 0.0)
                      for c in (candles or [])]
            highs = [float(c.get("h") or c.get("high") or 0.0)
                     for c in (candles or [])]
            lows = [float(c.get("l") or c.get("low") or 0.0)
                    for c in (candles or [])]
            vols = [float(c.get("v") or c.get("volume") or 0.0)
                    for c in (candles or [])]
        except Exception:
            return {}
        if len(closes) < MIN_BARS:
            return {}
        try:
            return compute_god_features(closes, highs=highs, lows=lows,
                                        volumes=vols) or {}
        except Exception:
            return {}

    # -- the loop --------------------------------------------------------
    def tick(self, market, now=None):
        """Run one observation cycle across the mandate's symbols.

        `market` maps symbol -> {"candles": [...], "quote": {...}}.
        Returns the proposals produced this tick. Never raises: an agent
        that crashes must not take the runtime with it, so faults are
        counted and recorded.
        """
        m = self.mandate
        out = []
        if m.state != "running":
            return out
        self.stats["ticks"] += 1
        for sym in m.symbols:
            try:
                entry = (market or {}).get(sym) or {}
                candles = entry.get("candles") or []
                quote = entry.get("quote") or {}
                features = self.observe(candles, sym, quote)
                if not features:
                    self.stats["no_signal"] += 1
                    continue
                sig = self._form_signal(sym, candles, quote, features)
                if sig is None:
                    self.stats["no_signal"] += 1
                    continue
                p = self._propose(sig, features, now=now, quote=quote)
                if p is not None:
                    out.append(p)
                    self.proposals.append(p)
                    if len(self.proposals) > 200:
                        self.proposals = self.proposals[-200:]
            except Exception as e:
                # A fault is data, not a crash. Counted, not swallowed.
                self.stats["errors"] += 1
                self.restraint.record(
                    _FaultProxy(sym), "runtime",
                    "tick failed: {}".format(e))
        return out

    def _form_signal(self, symbol, candles, quote, features):
        """Ask the strategy engine. Returns None when it has no view."""
        try:
            from engines.signals import preview_signal
        except ImportError:
            return None
        params = dict(self.mandate.params)
        params.setdefault("qty", 1.0)
        try:
            r = preview_signal(self.mandate.strategy, candles, quote, params)
        except Exception:
            return None
        side = r.get("side")
        if side not in ("buy", "sell"):
            return None
        price = None
        for k in ("last", "ask", "bid"):
            v = (quote or {}).get(k)
            try:
                if v and float(v) > 0:
                    price = float(v)
                    break
            except (TypeError, ValueError):
                continue
        if price is None:
            try:
                price = float(candles[-1].get("c") or candles[-1].get("close"))
            except Exception:
                return None
        qty = r.get("qty") or params.get("qty") or 1.0
        p = Proposal(self.mandate.agent_id, symbol, side, qty, price,
                     str(r.get("reason", "")))
        p.features = {
            "close": features.get("close"),
            "rsi_14": features.get("rsi_14"),
            "adx_14": features.get("adx_14"),
            "atr_pct": features.get("atr_pct"),
            "vol_ratio": features.get("vol_ratio"),
        }
        p.notes.append("strategy={}".format(self.mandate.strategy))
        return p

    def _profile(self):
        """The authority document handed to the gates.

        The mandate *is* the profile: the compliance gate reads its
        symbol list from here. Handing the gates None made every
        proposal fail closed, which is safe but useless -- an agent that
        can never clear its own gates is not a bounded agent, it is a
        broken one.
        """
        return {"symbols": list(self.mandate.symbols),
                "mode": self.mandate.mode,
                "max_notional": self.mandate.max_notional,
                "mandate": self.mandate.agent_id,
                # Dissenter._find_rule walks profile.rules looking for the
                # rule that fired. An agent's signal comes from a strategy
                # function, not a DNA rule, so there is nothing to find --
                # but the attribute has to exist or the dissenter raises,
                # and a dissenter that always raises is not a check.
                "rules": []}

    def _propose(self, sig, features, now=None, quote=None):
        """Adversarial review, then gates, then maybe an order."""
        m = self.mandate
        p = sig

        # -- 1. mandate authority -------------------------------------
        if not m.may_trade(p.symbol):
            p.outcome = "blocked"
            p.notes.append("symbol outside mandate")
            self.restraint.record(p, "mandate", "symbol not in mandate")
            self.stats["blocked"] += 1
            return p
        if not m.notional_ok(p.qty, p.price):
            p.outcome = "blocked"
            p.notes.append("notional {:.2f} exceeds cap {:.2f}".format(
                abs(p.qty * p.price), m.max_notional))
            self.restraint.record(p, "mandate", "notional cap")
            self.stats["blocked"] += 1
            return p
        if not m.rate_ok(now):
            p.outcome = "blocked"
            p.notes.append("action budget exhausted for this hour")
            self.restraint.record(p, "mandate", "rate limit")
            self.stats["blocked"] += 1
            return p

        # -- 2. adversarial review ------------------------------------
        # The dissenter cannot re-run the rule; it looks for structural
        # defects the rule is blind to.
        try:
            objections = self.dissenter.objections(
                p, self._profile(), features, quote)
        except Exception as e:
            objections = ["dissenter raised: {}".format(e)]
        p.objections = [o for o in objections if o]
        if len(p.objections) > m.min_dissent_support:
            p.outcome = "held"
            p.notes.append("demoted by {} objection(s)".format(
                len(p.objections)))
            self.restraint.record(p, "dissenter",
                                  "; ".join(p.objections))
            self.stats["held"] += 1
            return p

        # -- 3. gates (both must pass; either can halt) ----------------
        allowed, reason = self.gate.evaluate(p, self._profile(), features)
        if not allowed:
            p.outcome = "blocked"
            p.gate = "risk"
            p.notes.append("risk gate: {}".format(reason))
            self.restraint.record(p, "risk", reason)
            self.stats["blocked"] += 1
            return p
        allowed, reason = self.compliance.evaluate(p, self._profile(), features)
        if not allowed:
            p.outcome = "blocked"
            p.gate = "compliance"
            p.notes.append("compliance gate: {}".format(reason))
            self.restraint.record(p, "compliance", reason)
            self.stats["blocked"] += 1
            return p

        p.outcome = "proposed"
        self.stats["proposed"] += 1

        # -- 4. execution is a separate, explicit authority ------------
        if not m.auto_execute:
            p.notes.append("awaiting ratification (auto_execute off)")
            return p
        if self.executor is None:
            # Fail closed: authorised to trade but nothing to trade with.
            p.outcome = "blocked"
            p.notes.append("auto_execute on but no executor attached")
            self.restraint.record(p, "executor", "no executor")
            self.stats["blocked"] += 1
            return p
        try:
            p.order_id = self.executor(p)
            p.outcome = "executed"
            m.record_action(now)
            self.stats["executed"] += 1
        except Exception as e:
            p.outcome = "blocked"
            p.notes.append("execution failed: {}".format(e))
            self.restraint.record(p, "executor", str(e))
            self.stats["blocked"] += 1
        return p

    def status(self):
        return {
            "mandate": self.mandate.to_dict(),
            "stats": dict(self.stats),
            "restraint": self.restraint.to_dict(),
            "recent": [p.to_dict() for p in self.proposals[-10:]],
        }


# ------------------------------------------------------------- default gates
def _notional_gate(signal, profile, features=None):
    """Risk gate: a proposal must be positive-size and priced.

    Reads attributes, not keys: an agent hands gates a Proposal object.
    """
    try:
        qty = abs(float(getattr(signal, "qty", None)))
        price = float(getattr(signal, "price", None))
    except (TypeError, ValueError):
        return False, "qty/price not numeric"
    if qty <= 0:
        return False, "qty must be > 0"
    if price <= 0:
        return False, "price must be > 0"
    cap = (profile or {}).get("max_notional") if isinstance(profile, dict) else None
    if cap:
        try:
            if qty * price > float(cap):
                return False, "notional {:.2f} exceeds cap {:.2f}".format(
                    qty * price, float(cap))
        except (TypeError, ValueError):
            pass
    return True, ""


class AgentDissenter(object):
    """Adversarial review for agent signals.

    Deliberately not brokers.gods.Dissenter. That one reviews a fired
    DNA rule: it walks `profile.rules` looking for the rule's support
    count and measures feature staleness over feature *series*. An agent
    has neither -- its signal comes from a strategy function and its
    features are scalars. Forcing both shapes through one interface got
    the diff: every call raised and every proposal was blocked, so the
    agent could never act at all.

    Each check here names a real defect that would make a strategy
    signal untrustworthy, and returns a reason or nothing. An adversary
    that always objects is as useless as one that never does.
    """

    def __init__(self, max_signal_age_s=300, max_spread_bps=60.0):
        self.max_signal_age_s = int(max_signal_age_s)
        self.max_spread_bps = float(max_spread_bps)

    def objections(self, signal, profile=None, features=None, quote=None):
        out = []
        now = int(time.time())
        age = now - int(getattr(signal, "ts", now) or now)
        if age > self.max_signal_age_s:
            out.append("proposal is {}s old, max {}".format(
                age, self.max_signal_age_s))
        if not str(getattr(signal, "reason", "") or "").strip():
            out.append("no reason attached to the signal")
        # An incomplete feature vector means the view was partial. Every
        # one of these being None is a reason not to act, not a detail.
        missing = [k for k, v in (features or {}).items() if v is None]
        if missing:
            out.append("features unavailable: {}".format(
                ", ".join(sorted(missing)[:4])))
        q = quote or {}
        try:
            bid, ask = float(q.get("bid")), float(q.get("ask"))
            if bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                if mid > 0:
                    sp = (ask - bid) / mid * 10000.0
                    if sp > self.max_spread_bps:
                        out.append("spread {:.1f}bps too wide to act on".format(sp))
        except (TypeError, ValueError):
            pass
        return out

    def dissent(self, signal, profile=None, features=None, quote=None):
        o = self.objections(signal, profile, features, quote)
        return ("object", o) if o else ("pass", [])


def _allowlist_gate(signal, profile, features=None):
    """Compliance gate: a mandate with no symbols trades nothing.

    Unlike gods.default_compliance_gate (whose allowlist is empty and so
    rejects everything by design), this gate receives the mandate's own
    symbol list via the profile. Reads attributes, not keys.
    """
    allowed = (profile or {}).get("symbols") if isinstance(profile, dict) else None
    if not allowed:
        return False, "no symbol allowlist for this agent"
    symbol = getattr(signal, "symbol", None)
    if symbol is None and isinstance(signal, dict):
        symbol = signal.get("symbol")
    if symbol not in allowed:
        return False, "symbol {} not in allowlist".format(symbol)
    return True, ""


class AgentRuntime(object):
    """Owns a set of agents and the market snapshot they all read."""

    def __init__(self):
        self.agents = {}
        self.market = {}
        self._order = []

    def add(self, mandate, **kw):
        if mandate.agent_id in self.agents:
            raise MandateError("agent {} already exists".format(
                mandate.agent_id))
        a = TradingAgent(mandate, **kw)
        self.agents[mandate.agent_id] = a
        self._order.append(mandate.agent_id)
        return a

    def get(self, agent_id):
        return self.agents.get(str(agent_id))

    def remove(self, agent_id):
        aid = str(agent_id)
        a = self.agents.pop(aid, None)
        if a is not None:
            a.mandate.state = "retired"
            self._order = [x for x in self._order if x != aid]
        return a is not None

    def start(self, agent_id):
        a = self.get(agent_id)
        if a is None:
            return False
        a.mandate.state = "running"
        return True

    def pause(self, agent_id):
        a = self.get(agent_id)
        if a is None:
            return False
        a.mandate.state = "paused"
        return True

    def set_market(self, market):
        self.market = market or {}

    def tick(self, now=None):
        out = []
        for aid in self._order:
            a = self.agents.get(aid)
            if a is None:
                continue
            try:
                out.extend(a.tick(self.market, now=now))
            except Exception:
                # One agent's failure must not stop the others.
                a.stats["errors"] += 1
        return out

    def snapshot(self):
        return {
            "agents": [self.agents[a].status() for a in self._order
                       if a in self.agents],
            "market_symbols": sorted(self.market.keys()),
        }


# --------------------------------------------------------------- default rt
_runtime = None


def runtime():
    """Process-wide runtime singleton."""
    global _runtime
    if _runtime is None:
        _runtime = AgentRuntime()
    return _runtime
