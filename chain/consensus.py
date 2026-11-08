"""Consensus constants and helpers for Mafutani Chain (MFT). Zero-dep.

Normative rules:
- COIN = 10**8 base units.
- EPOCH_REWARD = 100,000,000,000 MFT (100B) minted once per epoch via a SEED tx.
- EPOCH_SECONDS = 182 days (6 months). Epoch 0 starts at GENESIS_TIME.
- TOKEN_LIFETIME_EPOCHS = 1: outputs expire at (creation_epoch + 1). Expired
  outputs MUST NOT be spent. The per-epoch SEED replaces expiring supply.
  This is deterministic and does not break consensus: every node computes the
  same epoch, expiry, and valid seed window from block timestamps.
- PoW: target is encoded as compact-ish integer difficulty; valid if
  int(SHAKE256(header||nonce), 16) < target. Difficulty adjusts every
  DIFFICULTY_INTERVAL blocks toward TARGET_BLOCK_SECONDS.
- Genesis is block 0 at height 0 with no transactions and fixed hash rules.
"""
import hashlib
import struct
import time

# Monetary
COIN = 10 ** 8
EPOCH_REWARD = 100_000_000_000 * COIN   # 100B MFT
EPOCH_SECONDS = 182 * 24 * 3600         # 6 months
TOKEN_LIFETIME_EPOCHS = 1               # expires next epoch; replaced by seed
MAX_MONEY = EPOCH_REWARD * 10           # sanity cap (active+inflight)

# Identity / reference valuation (display & policy metadata; NOT consensus,
# NOT a guaranteed market price). Operators may override via environment.
import os as _os
COIN_SYMBOL = _os.environ.get("MAFUTANI_COIN_SYMBOL", "MFT")
COIN_NAME = _os.environ.get("MAFUTANI_COIN_NAME", "Mafutani Coin")
COIN_ALIAS = _os.environ.get("MAFUTANI_COIN_ALIAS", "CommCoin")
try:
    REFERENCE_VALUE_USD = float(_os.environ.get("MAFUTANI_COIN_USD_VALUE", "100000"))
except Exception:
    REFERENCE_VALUE_USD = 100000.0
REFERENCE_VALUE_NOTE = (
    "Reference/configured valuation for display and accounting metadata only. "
    "Not a market price, guarantee, offer, or financial advice.")

# PoW / chain
TARGET_BLOCK_SECONDS = 120              # 2 minutes, SBC-friendly
DIFFICULTY_INTERVAL = 72                # adjust ~every 2.4 hours
INITIAL_DIFFICULTY_BITS = 16            # SBC-friendly bootstrapping (pure-Python mineable); adjusts upward
MAX_FUTURE_SECONDS = 2 * 3600
GENESIS_TIME = 1767225600               # 2026-01-01T00:00:00Z (fixed)
CHAIN_ID = "mafutani-mainnet-v1"

# Seed tx marker
SEED_TX_TYPE = "epoch_seed"


def epoch_for_time(ts):
    if ts < GENESIS_TIME:
        return 0
    return int((int(ts) - GENESIS_TIME) // EPOCH_SECONDS)


def epoch_start(epoch):
    return GENESIS_TIME + int(epoch) * EPOCH_SECONDS


def epoch_end(epoch):
    return epoch_start(int(epoch) + 1)


def output_expiry_epoch(creation_epoch):
    return int(creation_epoch) + TOKEN_LIFETIME_EPOCHS


def is_expired(creation_epoch, current_epoch):
    return int(current_epoch) >= output_expiry_epoch(creation_epoch)


def bits_to_target(bits):
    """Convert compact difficulty bits to integer target. Higher bits => easier."""
    bits = max(1, min(248, int(bits)))
    return (1 << (256 - bits)) - 1


def target_to_bits(target):
    if target <= 0:
        return 1
    t = int(target)
    bitlen = t.bit_length()
    return max(1, 256 - bitlen)


def pow_hash(header_bytes, nonce):
    h = hashlib.shake_256()
    h.update(header_bytes)
    h.update(struct.pack(">Q", int(nonce) & 0xFFFFFFFFFFFFFFFF))
    return h.digest(32)


def pow_value(header_bytes, nonce):
    return int.from_bytes(pow_hash(header_bytes, nonce), "big")


def meets_target(header_bytes, nonce, bits):
    return pow_value(header_bytes, nonce) < bits_to_target(bits)


def work_from_bits(bits):
    target = bits_to_target(bits)
    if target <= 0:
        return 0
    return (1 << 256) // (target + 1)


def adjust_bits(last_bits, actual_seconds, interval=DIFFICULTY_INTERVAL):
    expected = TARGET_BLOCK_SECONDS * interval
    if expected <= 0:
        return last_bits
    # Clamp adjustment to avoid wild swings
    actual = max(expected // 4, min(expected * 4, int(actual_seconds)))
    target = bits_to_target(last_bits)
    target = (target * actual) // expected
    # Bounds
    max_target = bits_to_target(8)
    min_target = bits_to_target(240)
    target = max(min_target, min(max_target, target))
    return target_to_bits(target)
