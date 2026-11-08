"""PiNet: Raspberry Pi / SBC-friendly CommCoin block capture (stdlib only).

Philosophy (implemented as technical controls, not guarantees):
- SBC-friendly: bounded mining rounds, low overhead, simple sync.
- No unnecessary token loss: explicit warnings/policies in wallet/tx flows;
  consensus expiry is deterministic and documented.
- No nonsense hacks: validation on import/submit, no privileged shortcuts,
  auditable operations, hash-based PQC.
- Capture blocks on PiNet by mining and/or syncing from peers.
"""
import json
import os
import time

from chain.ledger import Chain, txid_for
from chain.miner import Miner
from node.mempool import Mempool
from node.sync import export_blocks, export_headers, import_blocks, pull_from_peer
from node.proofs import ProofService


def _env_int(name, default):
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return int(default)


class PiNetNode(object):
    def __init__(self, db_path=None):
        self.chain = Chain(path=db_path)
        self.mempool = Mempool(max_txs=_env_int("MAFUTANI_MEMPOOL_MAX", 5000))
        self.proofs = ProofService(self.chain)
        self.started_at = time.time()

    def close(self):
        try:
            self.chain.close()
        except Exception:
            pass

    def status(self):
        info = self.chain.chain_info()
        tip = self.chain.tip()
        state = self.proofs.utxo_set_commitment(limit=200000)
        return {
            "ok": True,
            "node": "commcoin-pinet",
            "mode": "sbc-capture",
            "sbc_friendly": True,
            "transport": "stdlib-http-json+file-export",
            "uptime_s": int(time.time() - self.started_at),
            "chain": info,
            "tip": {"height": tip.get("height"), "hash": tip.get("hash"),
                    "ts": tip.get("ts"), "epoch": tip.get("epoch")} if tip else None,
            "state": state,
            "mempool": self.mempool.snapshot(limit=20),
            "safety": {
                "pqc_stateful_warning": "MFT PQC wallets are stateful; never reuse/restore stale signature state.",
                "expiry_note": "Outputs expire by deterministic consensus (token lifetime epochs) and are replaced by epoch seeding; this is protocol behavior, not a hidden hack.",
                "reference_value": "Configured/reference valuation metadata only, not a market price/guarantee.",
            },
        }

    def submit_tx(self, tx):
        # Structural + consensus validation against current state; mempool is staging only.
        epoch = self.chain.current_epoch()
        height = self.chain.height() + 1
        self.chain.validate_tx(tx, epoch, height, block_has_seed=False)
        tid = txid_for(tx)
        res = self.mempool.add(tid, tx)
        res["txid"] = tid
        return res

    def capture(self, address, rounds=None):
        # SBC-friendly bounded mining rounds; uses mempool txs if valid.
        rounds = int(rounds or _env_int("MAFUTANI_PINET_ROUNDS", 100000))
        rounds = max(1000, min(rounds, _env_int("MAFUTANI_PINET_MAX_ROUNDS", 2000000)))
        miner = Miner(self.chain, address=address)
        extra = self.mempool.drain_for_block(limit=200)
        # filter to txs still valid now (best-effort)
        valid_extra = []
        epoch = self.chain.current_epoch()
        h = self.chain.height() + 1
        seed_seen = False
        for tx in extra:
            try:
                self.chain.validate_tx(tx, epoch, h, block_has_seed=seed_seen)
                if tx.get("type") == "epoch_seed":
                    seed_seen = True
                valid_extra.append(tx)
            except Exception:
                self.mempool.remove(txid_for(tx))
        result = miner.mine_and_submit(address=address, extra_txs=valid_extra, rounds=rounds)
        if result.get("ok"):
            # clear included txids
            for tx in valid_extra:
                self.mempool.remove(txid_for(tx))
        result["rounds"] = rounds
        result["mempool_drained_attempted"] = len(valid_extra)
        return result

    def export(self, start=1, limit=200):
        return export_blocks(self.chain, start, limit=limit)

    def headers(self, start=0, limit=2000):
        return export_headers(self.chain, start, limit=limit)

    def import_blocks(self, blocks):
        return import_blocks(self.chain, blocks)

    def pull(self, peer_url, max_blocks=100):
        return pull_from_peer(self.chain, peer_url, max_blocks=max_blocks)

    def verify_proof(self, proof):
        from chain import merkle
        domain = "txnode" if (proof or {}).get("mrkl") else "node"
        # tx proofs use txnode domain in this implementation
        ok = merkle.verify(proof or {}, domain_node=proof.get("domain_node") or "txnode")
        return {"ok": bool(ok), "proof": proof}


# process-local node cache keyed by db path (lightweight; chain has its own sqlite conn)
_NODES = {}


def get_node(db_path=None):
    path = db_path or os.environ.get("MAFUTANI_CHAIN_DB") or Chain().path
    # normalize
    try:
        from chain.ledger import DB_PATH_DEFAULT
        path = path or DB_PATH_DEFAULT
    except Exception:
        pass
    key = os.path.abspath(str(path))
    n = _NODES.get(key)
    if n is None:
        n = PiNetNode(db_path=path)
        _NODES[key] = n
    return n
