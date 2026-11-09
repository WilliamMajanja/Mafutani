# External Security References (Informational, Non-Endorsement)

This document records **publicly referenced** external projects/materials reviewed for defensive lessons. It is **not** an endorsement, dependency, certification, audit, or proof of vulnerability/compromise. Mafutani remains **stdlib-only** (enforced by `tests/test_zero_dependency.py`) and **experimental/unaudited**.

## CPIP (Coffee Pot Internet Protocol) — WilliamMajanja/CPIP-

- Source: https://github.com/WilliamMajanja/CPIP-
- What it is: an HTCPCP/RFC 2324/7168-inspired implementation with mesh/multi-transport, covert-channel, defense/posture, crypto, and operational tooling concepts (per its README). **Not a blockchain consensus protocol.**
- License/usage: review upstream license/terms before reuse. Mafutani does **not** vendor or depend on CPIP.
- Defensive ideas potentially relevant (stdlib-only adaptations only, no guarantees):
  - Explicit runtime defense/readiness toggles and operator posture visibility (`/api/admin/readiness` in Mafutani is a similar preventive checklist concept).
  - Clear separation of simulated/draft capabilities from real, configured backends.
  - Transport/endpoint hardening mindset (origin/host validation patterns, rate/size limits, no secrets in URLs) — consistent with MCP/transport security best practices where applicable.
  - Honest labeling of experimental/non-FIPS/unaudited crypto components.

## Publicly reported concerns (verify independently; no speculation)

> **Do not treat this section as proof that any network “will break consensus.”** Consensus safety/liveness depends on implementation, parameters, adversary model, deployment, and patches. Verify against primary advisories, source, maintainers, and independent analysis.

### Minima (external project; not integrated in Mafutani)
- Mafutani has **removed Minima integration** from the active runtime.
- Public materials describe Minima’s Tx-PoW/cascading/pruning design and operational assumptions (e.g., transaction volume/bootstrap considerations). Review primary docs/source: https://docs.minima.global/
- A third-party/fork PoC repository (https://github.com/WilliamMajanja/Minima-private, claims by its author) lists dependency-related CVEs addressed in that context (e.g., BouncyCastle/Rhino/H2 items cited there). **These are not verified here as Minima core consensus breaks.** Treat as unverified claims unless corroborated by upstream advisories/releases.
- Lesson for CommCoin: dependency hygiene is less relevant to Mafutani’s stdlib-only runtime, but **cryptographic/state implementation correctness, validation, and consensus transition safety** are critical.

### BNB Chain / BSC / Binance ecosystem (external; not integrated in Mafutani)
- Public discussion (reported concern, not a proven exploit here): https://github.com/bnb-chain/bsc/issues/3800 and https://forum.bnbchain.org/t/potential-committee-divergence-issue-in-binance-consensus/5332 describe a **potential committee divergence/safety scenario** involving validator-set activation across branches and delayed switching under finality-stall conditions (Parlia/Binance Consensus). Maintainers provided risk assessment/responses in the issue thread; **verify current status, affected versions, and mitigations** from official sources.
- Public CVEs exist for some related libraries/components in the broader ecosystem (examples found in NVD/advisories): tss-lib timing side-channel issues (CVE-2023-26556, CVE-2023-26557) and older keygen issue (CVE-2020-12118) — **component-specific**, not automatic proof of chain consensus failure.
- Bridge/application-layer incidents (e.g., historical BNB Bridge exploit) are distinct from base consensus safety; do not conflate.
- Lesson for CommCoin: be cautious with **validator/committee/set transitions, delayed activation, fork-choice/finality interactions, and state transitions across branches**. Any such logic must be specified, tested (including stall/fork scenarios), and independently reviewed. Mafutani/CommCoin currently uses a different, experimental model (SHAKE256 PoW, epoch seed/expiry rules, tip-append sync import) — still unaudited.

## Mafutani/CommCoin actions (defensive, non-guaranteed)
- Keep **stdlib-only** and **honest risk labeling**.
- Treat PQC/wallet state, consensus, sync/import, and off-ramp settlement as **high risk**.
- Add/maintain: `confirm:true` gates, atomic PQC state save + fail-safe, reference-only value labeling, acceptance≠settlement, callback reconciliation scaffolds (not verification), maker-checker/step-up/velocity hooks, readiness checklist.
- Before production/mainnet claims: independent crypto/consensus/security audit, adversarial/fuzz testing (fork/stall/set-transition/state-reuse cases), and operator compliance/custody programs.
