"""GODS layer: DNA-encoded rules, adversarial dissenter, dual veto.

Three ideas from public trading projects, ported into the Vibe layer.
Zero dependencies, deterministic, fail-closed.

1. GodStra DNA encoding (freqtrade/freqtrade-strategies, @Mablue)
   A rule's parameters are grouped into *genes*, each gene a typed slot
   (operator, indicator, integer, real). The encoding matters because it
   makes a rule mutable: a gene can be flipped, swapped, or crossed with
   another rule's gene without parsing text. Here it is used for
   *mutation* -- a rule can be perturbed and re-evaluated, which is the
   basis of a simple evolutionary search.

2. Pantheon adversarial dissenter (NAME0x0/Pantheon-Trades)
   When a signal fires, a separate check argues *against* it. The
   dissenter is not a second opinion on the same evidence; it is a
   structural countermeasure against consensus. Its job is to find the
   reason the signal is wrong, and it is required to state one.

3. Pantheon dual veto + restraint audit
   Two independent gates (risk, compliance) can each halt a signal
   unilaterally. Every *rejected* signal is recorded with a reason code,
   so the discipline is auditable -- a system that only logs its wins
   cannot be evaluated.

What this module does NOT do
----------------------------
* No LLM. The dissenter is deterministic: it checks structural
  properties of the signal (feature gaps, support, coverage, recency)
  rather than reasoning in prose. A dissenter that needs a model is a
  dissenter that can be talked out of its objection.
* No codegen. A mutated rule is a new condition tree evaluated by the
  existing evaluator, never a string that gets exec'd. The journal is
  untrusted input; making it executable would be a security hole.
* No auto-promotion. A mutated rule that scores better is reported, not
  adopted. Adoption is an explicit operator action.
"""
import math

from .base import BrokerError
from . import vibe

__all__ = ["DnaRule", "Dissenter", "VetoGate", "RestraintLog",
           "mutate_rule", "crossover", "dissent", "GodsError"]


class GodsError(BrokerError):
    """Bad input to the GODS layer (bad DNA, bad gate, bad signal)."""


# --------------------------------------------------------------------- DNA
#: Gene types. Each gene is a typed slot in a rule's DNA.
GENE_TYPES = ("operator", "indicator", "integer", "real")

#: The alphabet each gene type can take. Bounded so mutation is a
#: finite search, not an open-ended one.
GENE_ALPHABET = {
    "operator": ("<", "<=", ">", ">=", "==", "!="),
    "indicator": ("rsi14", "prior_return", "volume_ratio", "range_pct",
                  "hour_utc", "weekday"),
    "integer": tuple(range(0, 101)),
    "real": (0.0, 0.001, 0.01, 0.1, 0.5, 1.0, 2.0, 5.0, 10.0),
}


class DnaRule(object):
    """A rule encoded as a typed gene sequence.

    The DNA is a tuple of (type, value) pairs. It is the *only*
    representation: the condition tree is derived from it, so a mutation
    cannot produce a tree the DNA does not describe.
    """

    __slots__ = ("dna", "rule_id", "support", "coverage")

    def __init__(self, dna, rule_id="R0", support=0, coverage=0.0):
        self.dna = tuple(dna)
        self.rule_id = rule_id
        self.support = int(support)
        self.coverage = float(coverage)

    def to_condition(self):
        """Derive a condition tree from the DNA.

        The first three genes are (operator, indicator, real) and form a
        single comparison. Further genes are ANDed on. A DNA with fewer
        than three genes is not a rule and returns None.
        """
        if len(self.dna) < 3:
            return None
        parts = []
        for i in range(0, len(self.dna) - 2, 3):
            op, ind, val = self.dna[i][1], self.dna[i + 1][1], self.dna[i + 2][1]
            if op not in GENE_ALPHABET["operator"]:
                raise GodsError("bad operator gene: {!r}".format(op))
            if ind not in GENE_ALPHABET["indicator"]:
                raise GodsError("bad indicator gene: {!r}".format(ind))
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                raise GodsError("bad value gene: {!r}".format(val))
            parts.append(vibe.cmp(ind, op, float(val)))
        if not parts:
            return None
        return parts[0] if len(parts) == 1 else vibe.all_of(*parts)

    def mutate(self, rate=0.3, rng=None):
        """Return a new DnaRule with some genes flipped.

        ``rng`` is a callable returning [0, 1). It is injectable so the
        mutation is reproducible -- a search that cannot be replayed is
        a search whose result cannot be trusted.
        """
        import random
        rng = rng or random.random
        new = []
        for gene_type, value in self.dna:
            if rng() < rate:
                choices = [v for v in GENE_ALPHABET[gene_type] if v != value]
                if choices:
                    new.append((gene_type, choices[int(rng() * len(choices))]))
                    continue
            new.append((gene_type, value))
        return DnaRule(new, rule_id=self.rule_id + "m",
                       support=self.support, coverage=self.coverage)

    def crossover(self, other):
        """Single-point crossover with another DnaRule."""
        if not self.dna or not other.dna:
            raise GodsError("cannot crossover empty DNA")
        point = min(len(self.dna), len(other.dna)) // 2
        child = self.dna[:point] + other.dna[point:]
        return DnaRule(child, rule_id=self.rule_id + "x",
                       support=self.support, coverage=self.coverage)

    def to_dict(self):
        return {
            "rule_id": self.rule_id,
            "dna": [[t, v] for t, v in self.dna],
            "support": self.support,
            "coverage": round(self.coverage, 4),
        }

    def __repr__(self):
        return "<DnaRule {} {}>".format(self.rule_id, self.dna)


def mutate_rule(rule, rate=0.3, rng=None):
    """Mutate a DnaRule (convenience wrapper)."""
    if not isinstance(rule, DnaRule):
        raise GodsError("mutate_rule needs a DnaRule, got {}".format(
            type(rule).__name__))
    return rule.mutate(rate=rate, rng=rng)


def crossover(a, b):
    """Crossover two DnaRules (convenience wrapper)."""
    if not isinstance(a, DnaRule) or not isinstance(b, DnaRule):
        raise GodsError("crossover needs two DnaRules")
    return a.crossover(b)


# ---------------------------------------------------------------- dissenter
class Dissenter(object):
    """Structural counterargument against a signal.

    The dissenter does not re-evaluate the rule -- the rule already
    fired. It checks the *context* the rule cannot see: whether the
    features are stale, whether the support is thin, whether the symbol
    has been quiet. Each check returns a reason string or None.

    A dissenter that always objects is as useless as one that never
    does. The checks are deliberately narrow: each one names a specific
    structural defect that would make a fired rule untrustworthy.
    """

    def __init__(self, min_support=3, max_feature_age_bars=5):
        self.min_support = int(min_support)
        self.max_feature_age_bars = int(max_feature_age_bars)

    def objections(self, signal, profile, features=None):
        """Return a list of objection strings. Empty means no objection."""
        out = []
        if signal is None:
            return ["no signal to object to"]

        # Thin support: the rule fired on few roundtrips.
        rule = self._find_rule(profile, signal.rule_id)
        if rule is not None and rule.support < self.min_support:
            out.append("support {} below minimum {}".format(
                rule.support, self.min_support))

        # Stale features: the bar the rule fired on is old.
        if features:
            age = self._feature_age(features)
            if age is not None and age > self.max_feature_age_bars:
                out.append("features {} bars old, max {}".format(
                    age, self.max_feature_age_bars))

        # Zero coverage: the rule has never been evaluated on a live bar.
        if rule is not None and rule.coverage <= 0.0:
            out.append("rule has zero coverage in the journal")

        return out

    def _find_rule(self, profile, rule_id):
        if profile is None:
            return None
        for r in profile.rules:
            if r.rule_id == rule_id:
                return r
        return None

    def _feature_age(self, features):
        """How many bars old is the newest non-None feature?"""
        if not features:
            return None
        newest = None
        for series in features.values():
            if not series:
                continue
            for i in range(len(series) - 1, -1, -1):
                if series[i] is not None:
                    newest = max(newest or 0, len(series) - 1 - i)
                    break
        return newest

    def dissent(self, signal, profile, features=None):
        """Return (verdict, reasons). Verdict is 'pass' or 'object'."""
        objections = self.objections(signal, profile, features)
        if objections:
            return "object", objections
        return "pass", []


# ---------------------------------------------------------------- veto gate
class VetoGate(object):
    """An independent gate that can halt a signal unilaterally.

    Two gates are used: risk and compliance. They are independent by
    construction -- a signal must pass both, and either can halt it
    without consulting the other. A gate that can be overruled is not a
    gate.
    """

    def __init__(self, name, check):
        self.name = name
        self._check = check

    def evaluate(self, signal, profile, features=None):
        """Return (allowed, reason). allowed is True or False."""
        try:
            ok, reason = self._check(signal, profile, features)
        except Exception as e:
            # A gate that raises must fail closed. An exception in a
            # risk check is not a reason to allow the trade.
            return False, "gate {} raised: {}".format(self.name, e)
        return bool(ok), reason or ""


def default_risk_gate(signal, profile, features=None):
    """Risk gate: reject if the rule's support is too thin to trust."""
    if profile is None:
        return False, "no profile"
    for r in profile.rules:
        if r.rule_id == signal.rule_id:
            if r.support < 3:
                return False, "support {} < 3".format(r.support)
            return True, ""
    return False, "rule not in profile"


def default_compliance_gate(signal, profile, features=None):
    """Compliance gate: reject if the symbol is not in the allowed list.

    The allowed list is empty by default, which means *everything* is
    rejected. A compliance gate that allows everything is not a
    compliance gate; it is a comment.
    """
    allowed = ()
    if signal.symbol not in allowed:
        return False, "symbol {} not in compliance allowlist".format(
            signal.symbol)
    return True, ""


# ------------------------------------------------------------- restraint log
class RestraintLog(object):
    """Records every rejected signal with a reason code.

    The point is auditability. A system that only logs its wins cannot
    be evaluated -- the wins are evidence, but the refusals are the
    discipline. Each entry records what was rejected, by which gate, and
    why, so a later reader can ask "was that refusal correct?"
    """

    def __init__(self, limit=1000):
        self._entries = []
        self._limit = int(limit)

    def record(self, signal, gate, reason, rule_id=""):
        self._entries.append({
            "ts": int(signal.ts) if signal else 0,
            "symbol": signal.symbol if signal else "",
            "rule_id": rule_id or (signal.rule_id if signal else ""),
            "gate": gate,
            "reason": reason,
        })
        if len(self._entries) > self._limit:
            self._entries = self._entries[-self._limit:]

    def __len__(self):
        return len(self._entries)

    def recent(self, n=10):
        return list(self._entries[-n:])

    def to_dict(self):
        return {"count": len(self._entries), "recent": self.recent(10)}


# ------------------------------------------------------------------- gods
def dissent(signal, profile, features=None, min_support=3):
    """Convenience: run a Dissenter and return (verdict, reasons)."""
    return Dissenter(min_support=min_support).dissent(
        signal, profile, features)
