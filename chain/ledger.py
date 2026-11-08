"""Mafutani Chain ledger: transactions, blocks, UTXO state, validation, storage.

Zero-dep (stdlib + sqlite3). Uses a separate SQLite DB from the trading app.

Transaction model (simple UTXO):
- Inputs reference (txid, vout) and carry a PQC signature over the tx sighash.
- Outputs carry (address, amount_base, creation_epoch).
- A special SEED tx (no inputs, type=epoch_seed) is allowed once per epoch at
  or after epoch start; it outputs exactly EPOCH_REWARD to a recipient address.
  The seed recipient is the block miner's address for determinism in this local
  chain model (nodes accept the block's declared seed recipient if rules hold).

Consensus-critical expiry rule:
- An output is spendable only if current_epoch < expiry_epoch(output.creation_epoch).
- Expired outputs remain in history but cannot be referenced by inputs.
"""
import json
import os
import sqlite3
import struct
import time
from hashlib import sha3_256

from chain import consensus, pqc

DB_PATH_DEFAULT = os.path.join(os.path.dirname(__file__), "mft.db")


class ConsensusError(Exception):
    pass


def _b2h(b):
    return b.hex() if isinstance(b, (bytes, bytearray)) else str(b)


def _h2b(h):
    if isinstance(h, (bytes, bytearray)):
        return bytes(h)
    return bytes.fromhex(str(h))


def txid_for(tx):
    return sha3_256(canonical_tx_bytes(tx, include_sigs=False)).hexdigest()


def canonical_tx_bytes(tx, include_sigs=True):
    obj = {
        "type": tx.get("type", "transfer"),
        "inputs": [],
        "outputs": [
            {"a": o["a"], "v": int(o["v"]), "ce": int(o["ce"])}
            for o in tx.get("outputs", [])
        ],
        "epoch": int(tx.get("epoch", 0)),
        "memo": tx.get("memo", ""),
    }
    for i in tx.get("inputs", []):
        ii = {"tx": i["tx"], "n": int(i["n"]), "pub": i.get("pub")}
        if include_sigs:
            ii["sig"] = i.get("sig")
        obj["inputs"].append(ii)
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sighash(tx, input_index):
    """Hash signed by an input: tx without signatures, bound to input index."""
    base = canonical_tx_bytes(tx, include_sigs=False)
    return sha3_256(base + struct.pack(">I", int(input_index))).digest()


def make_output(address, amount_base, creation_epoch):
    if int(amount_base) <= 0:
        raise ConsensusError("output amount must be positive")
    if not isinstance(address, str) or not address.startswith(pqc.ADDR_PREFIX):
        raise ConsensusError("invalid output address")
    return {"a": address, "v": int(amount_base), "ce": int(creation_epoch)}


def make_seed_tx(epoch, recipient_address, memo="epoch seed"):
    return {
        "type": consensus.SEED_TX_TYPE,
        "epoch": int(epoch),
        "inputs": [],
        "outputs": [make_output(recipient_address, consensus.EPOCH_REWARD, int(epoch))],
        "memo": memo or "epoch seed",
    }


def block_header_bytes(block):
    v = int(block.get("v", 1))
    obj = {
        "v": v,
        "h": int(block["h"]),
        "prev": block["prev"],
        "mrkl": block["mrkl"],
        "ts": int(block["ts"]),
        "bits": int(block["bits"]),
        "epoch": int(block["epoch"]),
    }
    # v2+: include state/commitment root for Merkle-ized full-chain abstraction.
    # Legacy (v1, h<=1) preserved for existing seeded/genesis blocks.
    if v >= 2 or (int(block["h"]) >= 2 and block.get("state_root")):
        obj["state_root"] = str(block.get("state_root") or "")
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def block_hash(block):
    return sha3_256(block_header_bytes(block) + struct.pack(">Q", int(block["nonce"]))).hexdigest()


def merkle_root(txids):
    if not txids:
        return sha3_256(b"").hexdigest()
    layer = [bytes.fromhex(t) if isinstance(t, str) else t for t in txids]
    while len(layer) > 1:
        if len(layer) % 2 == 1:
            layer.append(layer[-1])
        nxt = []
        for i in range(0, len(layer), 2):
            nxt.append(sha3_256(layer[i] + layer[i + 1]).digest())
        layer = nxt
    return layer[0].hex()


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS blocks (
  height INTEGER PRIMARY KEY,
  hash TEXT NOT NULL UNIQUE,
  prev TEXT NOT NULL,
  ts INTEGER NOT NULL,
  bits INTEGER NOT NULL,
  nonce INTEGER NOT NULL,
  epoch INTEGER NOT NULL,
  mrkl TEXT NOT NULL,
  work TEXT NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blocks_hash ON blocks(hash);
CREATE INDEX IF NOT EXISTS idx_blocks_epoch ON blocks(epoch);
CREATE TABLE IF NOT EXISTS txs (
  txid TEXT PRIMARY KEY,
  height INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  type TEXT NOT NULL,
  epoch INTEGER NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_txs_height ON txs(height);
CREATE TABLE IF NOT EXISTS utxos (
  txid TEXT NOT NULL,
  vout INTEGER NOT NULL,
  address TEXT NOT NULL,
  amount TEXT NOT NULL,
  creation_epoch INTEGER NOT NULL,
  spent_height INTEGER,
  spent_txid TEXT,
  PRIMARY KEY (txid, vout)
);
CREATE INDEX IF NOT EXISTS idx_utxos_addr ON utxos(address, spent_height);
CREATE INDEX IF NOT EXISTS idx_utxos_unspent ON utxos(spent_height) WHERE spent_height IS NULL;
CREATE TABLE IF NOT EXISTS epoch_seeds (
  epoch INTEGER PRIMARY KEY,
  height INTEGER NOT NULL,
  txid TEXT NOT NULL
);
"""


class Chain(object):
    def __init__(self, path=None):
        self.path = path or os.environ.get("MAFUTANI_CHAIN_DB") or DB_PATH_DEFAULT
        d = os.path.dirname(os.path.abspath(self.path))
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._init_db()
        if self.height() < 0:
            self._write_genesis()

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _init_db(self):
        self.conn.executescript(SCHEMA)
        # safe migrations for Merkle/state abstractions
        for stmt in (
            "ALTER TABLE blocks ADD COLUMN state_root TEXT NOT NULL DEFAULT ''",
        ):
            try:
                self.conn.execute(stmt)
            except Exception:
                pass

    def _get_meta(self, k, default=None):
        r = self.conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default

    def _set_meta(self, k, v):
        self.conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (k, str(v)))

    def height(self):
        r = self.conn.execute("SELECT MAX(height) AS h FROM blocks").fetchone()
        h = r["h"] if r else None
        return -1 if h is None else int(h)

    def tip(self):
        h = self.height()
        if h < 0:
            return None
        return dict(self.conn.execute("SELECT * FROM blocks WHERE height=?", (h,)).fetchone())

    def get_block(self, height):
        r = self.conn.execute("SELECT * FROM blocks WHERE height=?", (int(height),)).fetchone()
        return dict(r) if r else None

    def get_block_by_hash(self, bh):
        r = self.conn.execute("SELECT * FROM blocks WHERE hash=?", (bh,)).fetchone()
        return dict(r) if r else None

    def get_tx(self, txid):
        r = self.conn.execute("SELECT * FROM txs WHERE txid=?", (txid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["tx"] = json.loads(d["body"])
        return d

    def has_seed_for_epoch(self, epoch):
        r = self.conn.execute("SELECT 1 FROM epoch_seeds WHERE epoch=?", (int(epoch),)).fetchone()
        return r is not None

    def current_epoch(self):
        tip = self.tip()
        if not tip:
            return consensus.epoch_for_time(consensus.GENESIS_TIME)
        return int(tip["epoch"])

    def utxos_for_address(self, address, current_epoch=None, include_expired=False):
        if current_epoch is None:
            current_epoch = self.current_epoch()
        rows = self.conn.execute(
            "SELECT * FROM utxos WHERE address=? AND spent_height IS NULL ORDER BY creation_epoch, txid, vout",
            (address,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["expired"] = consensus.is_expired(d["creation_epoch"], current_epoch)
            if include_expired or not d["expired"]:
                out.append(d)
        return out

    def balance(self, address, current_epoch=None):
        utxos = self.utxos_for_address(address, current_epoch=current_epoch)
        return sum(int(u["amount"]) for u in utxos)

    def _write_genesis(self):
        genesis = {
            "v": 1, "h": 0, "prev": "00" * 32, "ts": consensus.GENESIS_TIME,
            "bits": consensus.INITIAL_DIFFICULTY_BITS, "nonce": 0,
            "epoch": 0, "txs": [],
        }
        genesis["mrkl"] = merkle_root([])
        genesis["hash"] = block_hash(genesis)
        self._connect_block(genesis)

    def _connect_block(self, block):
        txs = block.get("txs", [])
        txids = [txid_for(tx) for tx in txs]
        mrkl = merkle_root(txids)
        bh = block_hash(block)
        prev_work = 0
        if int(block["h"]) > 0:
            prev = self.get_block(int(block["h"]) - 1)
            prev_work = int(prev["work"]) if prev else 0
        work = prev_work + consensus.work_from_bits(int(block["bits"]))
        cur = self.conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        try:
            cur.execute(
                "INSERT INTO blocks(height,hash,prev,ts,bits,nonce,epoch,mrkl,work,body) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (int(block["h"]), bh, block["prev"], int(block["ts"]), int(block["bits"]),
                 int(block["nonce"]), int(block["epoch"]), mrkl, str(work),
                 json.dumps(txs, sort_keys=True)))
            for i, tx in enumerate(txs):
                tid = txids[i]
                ttype = tx.get("type", "transfer")
                cur.execute(
                    "INSERT INTO txs(txid,height,idx,type,epoch,body) VALUES(?,?,?,?,?,?)",
                    (tid, int(block["h"]), i, ttype, int(tx.get("epoch", block["epoch"])), json.dumps(tx, sort_keys=True)))
                # spend inputs
                for inp in tx.get("inputs", []):
                    cur.execute("UPDATE utxos SET spent_height=?, spent_txid=? WHERE txid=? AND vout=? AND spent_height IS NULL",
                                (int(block["h"]), tid, inp["tx"], int(inp["n"])))
                    if cur.rowcount != 1:
                        raise ConsensusError("input not found or already spent: %s:%s" % (inp["tx"], inp["n"]))
                # create outputs
                for vout, out in enumerate(tx.get("outputs", [])):
                    cur.execute(
                        "INSERT INTO utxos(txid,vout,address,amount,creation_epoch,spent_height,spent_txid) VALUES(?,?,?,?,?,NULL,NULL)",
                        (tid, vout, out["a"], str(int(out["v"])), int(out["ce"])))
                if ttype == consensus.SEED_TX_TYPE:
                    cur.execute("INSERT OR IGNORE INTO epoch_seeds(epoch,height,txid) VALUES(?,?,?)",
                                (int(tx.get("epoch", block["epoch"])), int(block["h"]), tid))
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        return bh

    def validate_tx(self, tx, current_epoch, height, block_has_seed=False):
        ttype = tx.get("type", "transfer")
        outputs = tx.get("outputs") or []
        inputs = tx.get("inputs") or []
        if not outputs:
            raise ConsensusError("tx must have outputs")
        out_sum = 0
        for o in outputs:
            v = int(o["v"])
            if v <= 0 or v > consensus.MAX_MONEY:
                raise ConsensusError("invalid output amount")
            if int(o["ce"]) != int(current_epoch) and ttype != consensus.SEED_TX_TYPE:
                # normal outputs created in current epoch
                raise ConsensusError("output creation epoch must match current epoch")
            if not str(o.get("a", "")).startswith(pqc.ADDR_PREFIX):
                raise ConsensusError("invalid address")
            out_sum += v
        if out_sum > consensus.MAX_MONEY:
            raise ConsensusError("output sum too large")

        if ttype == consensus.SEED_TX_TYPE:
            if inputs:
                raise ConsensusError("seed tx must not have inputs")
            if int(tx.get("epoch", -1)) != int(current_epoch):
                raise ConsensusError("seed tx epoch mismatch")
            if out_sum != consensus.EPOCH_REWARD:
                raise ConsensusError("seed tx amount must equal EPOCH_REWARD")
            if self.has_seed_for_epoch(current_epoch) or block_has_seed:
                raise ConsensusError("seed tx already issued for epoch")
            return 0, out_sum

        # transfer
        if not inputs:
            raise ConsensusError("transfer tx must have inputs")
        in_sum = 0
        for i, inp in enumerate(inputs):
            row = self.conn.execute("SELECT * FROM utxos WHERE txid=? AND vout=?", (inp["tx"], int(inp["n"]))).fetchone()
            if not row:
                raise ConsensusError("missing input %s:%s" % (inp["tx"], inp["n"]))
            if row["spent_height"] is not None:
                raise ConsensusError("input already spent %s:%s" % (inp["tx"], inp["n"]))
            if consensus.is_expired(row["creation_epoch"], current_epoch):
                raise ConsensusError("input expired (creation_epoch=%s, current=%s)" % (row["creation_epoch"], current_epoch))
            # verify PQC signature
            pub = inp.get("pub")
            sig = inp.get("sig")
            if not pub or not sig:
                raise ConsensusError("input missing pub/sig")
            addr = pqc.address_from_pub(pub)
            if addr != row["address"]:
                raise ConsensusError("pubkey does not match input address")
            if not pqc.verify_signature(pub, sighash(tx, i), sig):
                raise ConsensusError("invalid PQC signature for input %d" % i)
            in_sum += int(row["amount"])
        if in_sum < out_sum:
            raise ConsensusError("inputs < outputs")
        if in_sum - out_sum > consensus.MAX_MONEY:
            raise ConsensusError("fee overflow")
        return in_sum, out_sum

    def validate_block(self, block):
        h = int(block["h"])
        prev = self.tip()
        if h == 0:
            if block["prev"] != "00" * 32:
                raise ConsensusError("bad genesis prev")
        else:
            if not prev or int(prev["height"]) != h - 1:
                raise ConsensusError("block height does not extend tip")
            if block["prev"] != prev["hash"]:
                raise ConsensusError("prev hash mismatch")
            if int(block["ts"]) < int(prev["ts"]):
                raise ConsensusError("timestamp before prev")
        if int(block["ts"]) > int(time.time()) + consensus.MAX_FUTURE_SECONDS:
            raise ConsensusError("timestamp too far in future")
        epoch = consensus.epoch_for_time(int(block["ts"]))
        if int(block.get("epoch", epoch)) != epoch:
            raise ConsensusError("epoch mismatch")
        txs = block.get("txs", [])
        txids = [txid_for(tx) for tx in txs]
        if merkle_root(txids) != block.get("mrkl"):
            raise ConsensusError("merkle root mismatch")
        if not consensus.meets_target(block_header_bytes(block), int(block["nonce"]), int(block["bits"])):
            raise ConsensusError("PoW target not met")
        # validate txs
        block_has_seed = False
        for tx in txs:
            self.validate_tx(tx, epoch, h, block_has_seed=block_has_seed)
            if tx.get("type") == consensus.SEED_TX_TYPE:
                block_has_seed = True
        return True

    def add_block(self, block):
        self.validate_block(block)
        bh = self._connect_block(block)
        return bh

    def next_bits(self):
        tip = self.tip()
        if not tip or int(tip["height"]) < 1:
            return consensus.INITIAL_DIFFICULTY_BITS
        h = int(tip["height"])
        if (h + 1) % consensus.DIFFICULTY_INTERVAL != 0:
            return int(tip["bits"])
        start_h = max(1, h - consensus.DIFFICULTY_INTERVAL + 1)
        start = self.get_block(start_h)
        if not start:
            return int(tip["bits"])
        actual = int(tip["ts"]) - int(start["ts"])
        if actual <= 0:
            actual = 1
        return consensus.adjust_bits(int(tip["bits"]), actual)

    def mempool_template(self):
        """Placeholder for tx mempool; this minimal node accepts direct block tx lists."""
        return []

    def chain_info(self):
        tip = self.tip()
        h = self.height()
        epoch = int(tip["epoch"]) if tip else 0
        return {
            "chain_id": consensus.CHAIN_ID,
            "height": h,
            "tip_hash": tip["hash"] if tip else None,
            "tip_ts": int(tip["ts"]) if tip else None,
            "epoch": epoch,
            "epoch_start": consensus.epoch_start(epoch),
            "epoch_end": consensus.epoch_end(epoch),
            "epoch_seeded": self.has_seed_for_epoch(epoch),
            "next_bits": self.next_bits(),
            "coin_symbol": consensus.COIN_SYMBOL,
            "coin_name": consensus.COIN_NAME,
            "coin_alias": consensus.COIN_ALIAS,
            "reference_value_usd": consensus.REFERENCE_VALUE_USD,
            "reference_value_note": consensus.REFERENCE_VALUE_NOTE,
            "epoch_reward_mft": str(consensus.EPOCH_REWARD // consensus.COIN),
            "epoch_reward": str(consensus.EPOCH_REWARD // consensus.COIN),
            "token_lifetime_epochs": consensus.TOKEN_LIFETIME_EPOCHS,
            "pqc": pqc.SIG_ALG,
        }
