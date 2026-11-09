"""Mafutani PQC wallet (stdlib only).

SECURITY: The private key file contains secret material and a monotonic
`next_index`. Losing the file loses funds. Copying the file and signing from
both copies can reuse a WOTS+ index and FORGE-PROOFING IS LOST. Always keep a
single authoritative wallet state; back up before use and never restore an old
backup over a newer state.
"""
import copy
import json
import os
import time

from chain import consensus, pqc
from chain.ledger import Chain, canonical_tx_bytes, sighash, txid_for


class Wallet(object):
    def __init__(self, path, chain=None):
        self.path = path
        self.chain = chain
        self.data = None

    @classmethod
    def create(cls, path, height=pqc.TREE_HEIGHT):
        pub, priv = pqc.generate_keypair(height=height)
        data = {
            "version": 1,
            "created_at": int(time.time()),
            "label": os.path.basename(path),
            "address": pqc.address_from_pub(pub),
            "pub": pub,
            "priv": priv,
            "policy_ack": {
                "pqc_stateful_index": True,
                "never_reuse_backup_state": True,
            },
        }
        w = cls(path)
        w.data = data
        w.save()
        return w

    def load(self):
        with open(self.path, "r", encoding="utf-8") as f:
            self.data = json.load(f)
        if not self.data.get("address") or not self.data.get("priv"):
            raise ValueError("invalid wallet file")
        return self

    def save(self):
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        tmp = self.path + ".tmp"
        body = json.dumps(self.data, indent=2, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass

    @property
    def address(self):
        return self.data["address"]

    @property
    def pub(self):
        return self.data["pub"]

    @property
    def priv(self):
        return self.data["priv"]

    def info(self, chain=None):
        c = chain or self.chain
        out = {
            "address": self.address,
            "pub_fingerprint": pqc.pub_fingerprint(self.pub),
            "alg": self.pub.get("alg"),
            "signatures_used": int(self.priv.get("next_index", 0)),
            "signatures_max": (1 << int(self.priv.get("h", pqc.TREE_HEIGHT))),
            "created_at": self.data.get("created_at"),
        }
        if c is not None:
            epoch = c.current_epoch()
            utxos = c.utxos_for_address(self.address, current_epoch=epoch, include_expired=True)
            spendable = [u for u in utxos if not u["expired"]]
            expired = [u for u in utxos if u["expired"]]
            out["balance_base"] = sum(int(u["amount"]) for u in spendable)
            out["balance_mft"] = str(out["balance_base"] // consensus.COIN)
            out["expired_base"] = sum(int(u["amount"]) for u in expired)
            out["utxo_count"] = len(spendable)
            out["expired_utxo_count"] = len(expired)
            out["epoch"] = epoch
        return out

    def _select_utxos(self, chain, amount_base, epoch):
        utxos = chain.utxos_for_address(self.address, current_epoch=epoch)
        # smallest-first to reduce dust; deterministic
        utxos.sort(key=lambda u: (int(u["amount"]), u["txid"], int(u["vout"])))
        selected = []
        total = 0
        for u in utxos:
            selected.append(u)
            total += int(u["amount"])
            if total >= amount_base:
                break
        if total < amount_base:
            raise ValueError("insufficient spendable balance (expired tokens are not spendable)")
        return selected, total

    def _append_signing_journal(self, entry):
        """Best-effort append-only intent journal (evidence/control aid, not a security guarantee)."""
        try:
            jp = self.path + ".signing.journal"
            with open(jp, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            pass

    def build_transfer(self, chain, to_address, amount_mft, memo="", fee_base=0):
        if not str(to_address).startswith(pqc.ADDR_PREFIX):
            raise ValueError("invalid recipient address")
        amount_base = int(amount_mft) * consensus.COIN if isinstance(amount_mft, int) and amount_mft >= 0 else None
        if amount_base is None:
            amount_base = int(float(str(amount_mft)) * consensus.COIN)
        if amount_base <= 0:
            raise ValueError("amount must be positive")
        epoch = chain.current_epoch()
        need = amount_base + int(fee_base)
        selected, total = self._select_utxos(chain, need, epoch)
        outputs = [{"a": to_address, "v": amount_base, "ce": epoch}]
        change = total - need
        if change > 0:
            outputs.append({"a": self.address, "v": change, "ce": epoch})
        tx = {
            "type": "transfer",
            "epoch": epoch,
            "inputs": [{"tx": u["txid"], "n": int(u["vout"]), "pub": self.pub, "sig": None} for u in selected],
            "outputs": outputs,
            "memo": memo or "",
        }
        # Stateful PQC safety (experimental/unaudited): one WOTS+ leaf/index per signature.
        # NEVER reuse indexes; NEVER restore stale wallet backups/state/copies.
        max_sigs = (1 << int(self.priv.get("h", pqc.TREE_HEIGHT)))
        used = int(self.priv.get("next_index", 0))
        need_sigs = len(tx["inputs"])
        if need_sigs <= 0:
            raise ValueError("no inputs to sign")
        if used + need_sigs > max_sigs:
            raise ValueError(
                "insufficient PQC signature capacity (%s used, %s needed, %s max). "
                "Rotate to a new wallet; do NOT reuse/restore old state." % (used, need_sigs, max_sigs))
        # Operate on a deep copy of private state; commit only after atomic save succeeds.
        priv_work = copy.deepcopy(self.priv)
        intent = {"ts": int(time.time()), "event": "sign_intent", "address": self.address,
                  "next_index_before": used, "need_sigs": need_sigs, "max_sigs": max_sigs,
                  "inputs": len(tx["inputs"]), "outputs": len(tx["outputs"]), "amount_base": amount_base}
        self._append_signing_journal(intent)
        for i in range(len(tx["inputs"])):
            tx["inputs"][i]["sig"] = pqc.sign_message(priv_work, sighash(tx, i))
        # Commit advanced state atomically before exposing tx as usable.
        old_priv = self.data.get("priv")
        self.data["priv"] = priv_work
        try:
            self.save()
        except Exception as e:
            self.data["priv"] = old_priv  # keep in-memory state unchanged; on-disk unchanged
            self._append_signing_journal({"ts": int(time.time()), "event": "save_failed_critical",
                                          "next_index_before": used, "error": str(e)[:200]})
            raise ValueError(
                "CRITICAL: signatures were generated but wallet state could not be saved (%s). "
                "Do NOT broadcast/use the transaction. Inspect wallet file integrity and "
                "PQC index state before any further signing. Never reuse/restore stale state." % e)
        self._append_signing_journal({"ts": int(time.time()), "event": "sign_committed",
                                      "next_index_before": used, "next_index_after": int(self.data["priv"].get("next_index", 0))})
        tx["txid"] = txid_for(tx)
        tx["pqc_safety"] = {"stateful": True, "index_range_consumed": [used, int(self.data["priv"].get("next_index", 0)) - 1],
                            "warning": "Never reuse/restore stale wallet state/backups."}
        return tx
