"""Zero-dependency, SBC-friendly miner for Mafutani Chain.

Mining is intentionally simple: iterate nonces over a block header until the
SHAKE256 PoW meets the current difficulty target. Difficulty adjusts to target
~2 minute blocks and can be kept low for Raspberry Pi / SBC devices.

This miner is cooperative and in-process (stdlib). For production use, run it
in a dedicated process/service; it holds the chain DB lock briefly when
submitting blocks.
"""
import time

from chain import consensus
from chain.ledger import Chain, block_hash, block_header_bytes, merkle_root, make_seed_tx, txid_for


class Miner(object):
    def __init__(self, chain=None, address=None, max_nonce_rounds=None):
        self.chain = chain or Chain()
        self.address = address
        self.max_nonce_rounds = max_nonce_rounds  # None = unlimited until stopped

    def build_candidate(self, address=None, extra_txs=None, ts=None):
        addr = address or self.address
        if not addr:
            raise ValueError("miner address required")
        tip = self.chain.tip()
        height = 0 if not tip else int(tip["height"]) + 1
        prev = "00" * 32 if not tip else tip["hash"]
        now = int(ts if ts is not None else time.time())
        if tip and now <= int(tip["ts"]):
            now = int(tip["ts"]) + 1
        epoch = consensus.epoch_for_time(now)
        bits = self.chain.next_bits()
        txs = list(extra_txs or [])
        # If this epoch has not been seeded, include deterministic seed tx.
        # "Replace expired tokens without breaking consensus": each epoch mints
        # 100B fresh MFT; prior-epoch outputs expire per consensus rules.
        if not self.chain.has_seed_for_epoch(epoch):
            # Ensure only one seed in candidate
            if not any(t.get("type") == consensus.SEED_TX_TYPE for t in txs):
                txs.insert(0, make_seed_tx(epoch, addr))
        block = {
            "v": 1,
            "h": height,
            "prev": prev,
            "ts": now,
            "bits": int(bits),
            "nonce": 0,
            "epoch": int(epoch),
            "txs": txs,
        }
        block["mrkl"] = merkle_root([txid_for(t) for t in txs])
        return block

    def mine_one(self, address=None, extra_txs=None, rounds=50000, start_nonce=0, progress_cb=None):
        """Try up to `rounds` nonces. Returns (block, found_bool). SBC-friendly
        in small batches so callers can yield/sleep between rounds."""
        block = self.build_candidate(address=address, extra_txs=extra_txs)
        header = block_header_bytes(block)
        bits = int(block["bits"])
        nonce = int(start_nonce)
        limit = nonce + int(rounds)
        while nonce < limit:
            if consensus.meets_target(header, nonce, bits):
                block["nonce"] = nonce
                block["hash"] = block_hash(block)
                return block, True
            nonce += 1
            if progress_cb and (nonce & 0x3FFF) == 0:
                progress_cb(nonce)
        block["nonce"] = nonce
        return block, False

    def mine_and_submit(self, address=None, extra_txs=None, rounds=200000, start_nonce=0):
        block, found = self.mine_one(address=address, extra_txs=extra_txs, rounds=rounds, start_nonce=start_nonce)
        if not found:
            return {"ok": False, "reason": "no solution in round", "height": block["h"], "bits": block["bits"],
                    "next_nonce": int(start_nonce) + int(rounds)}
        try:
            bh = self.chain.add_block(block)
            return {"ok": True, "hash": bh, "height": block["h"], "epoch": block["epoch"],
                    "seeded": self.chain.has_seed_for_epoch(block["epoch"]),
                    "txs": len(block.get("txs", [])), "nonce": block["nonce"]}
        except Exception as e:
            return {"ok": False, "reason": str(e), "height": block["h"]}
