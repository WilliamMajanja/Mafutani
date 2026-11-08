"""Post-quantum signatures for Mafutani Chain (stdlib only).

Scheme: hash-based WOTS+ one-time signatures authenticated by a Merkle tree,
using SHAKE256. Hash-based signatures are post-quantum (security relies on the
preimage/collision resistance of the hash function, not on factoring or discrete
log). This is "real PQC" in the sense that it resists known quantum attacks.

Practical notes / limits (stated honestly):
- This is a stateful hash-based wallet construction at the *key index* level:
  each WOTS+ leaf index MUST be used at most once. Reusing an index breaks
  WOTS+ security. Wallet code persists and advances the index.
- The Merkle tree height is fixed per keypair (default 10 => 1024 signatures).
  When exhausted, generate a new keypair and move funds.
- Signatures are larger than ECDSA (~2-8 KB depending on params) but cheap to
  verify on SBCs and require no big-number libraries.
- This is an original, clear implementation of standard WOTS+ ideas for this
  codebase; it is NOT a drop-in replacement for audited SLH-DSA/XMSS libraries.
  Use on production mainnet money only after independent cryptographic review.

Parameters (WOTS+, SHAKE256, n=32 bytes):
- w = 16 (Winternitz parameter)
- len1 = ceil(8*n / log2(w)) = 64
- len2 = floor(log_w(len1*(w-1))) + 1 = 3
- len = 67 chains of 32-byte values
"""
import hashlib
import hmac
import json
import os
import struct

# -- parameters -----------------------------------------------------------
N = 32                      # security parameter / hash output bytes
W = 16                      # Winternitz parameter (nibbles)
LOG_W = 4
LEN1 = (8 * N) // LOG_W     # 64
LEN2 = 3
LEN = LEN1 + LEN2           # 67
TREE_HEIGHT = 10            # 2**10 = 1024 one-time signatures per keypair
MAX_INDEX = (1 << TREE_HEIGHT) - 1

ADDR_PREFIX = "mft1"
SIG_ALG = "MFT-PQC-WOTS-MERKLE-SHAKE256-v1"


class PQCError(ValueError):
    """Invalid key, signature, or state."""


def _shake(data, outlen=N):
    return hashlib.shake_256(data).digest(outlen)


def _h(tag, *parts):
    """Domain-separated hash."""
    h = hashlib.shake_256()
    h.update(tag.encode("utf-8") + b"\x00")
    for p in parts:
        if isinstance(p, int):
            p = struct.pack(">Q", p & 0xFFFFFFFFFFFFFFFF)
        h.update(p)
    return h.digest(N)


def _base_w(msg_bytes, out_len):
    """Convert bytes to base-w digits."""
    total = 0
    for b in msg_bytes:
        total = (total << 8) | b
    bits = len(msg_bytes) * 8
    out = []
    for i in range(out_len):
        shift = bits - (i + 1) * LOG_W
        if shift >= 0:
            out.append((total >> shift) & (W - 1))
        else:
            out.append(0)
    return out


def _checksum(digits):
    csum = 0
    for d in digits:
        csum += (W - 1 - d)
    # csum fits in 12 bits for these params; encode as LEN2 base-w digits
    csum <<= (LEN2 * LOG_W - 12)  # left shift to fill LEN2 nibbles (12 bits)
    raw = csum.to_bytes((LEN2 * LOG_W + 7) // 8, "big")
    return _base_w(raw, LEN2)[-LEN2:]


def _chain(x, start, steps, pub_seed, addr):
    """Iterate hash chain (uses public seed as domain context)."""
    for i in range(start, start + steps):
        x = _h("wots.chain", pub_seed, addr, i, x)
    return x


def wots_keygen(seed, pub_seed, addr):
    """Generate WOTS+ (sk elements, pk element) for one leaf.

    sk elements are derived from secret `seed`; public chain computations use
    `pub_seed` so verification can recompute without the secret seed.
    """
    sk = []
    pk_parts = []
    for i in range(LEN):
        sk_i = _h("wots.sk", seed, addr, i)
        sk.append(sk_i)
        pk_parts.append(_chain(sk_i, 0, W - 1, pub_seed, addr + struct.pack(">I", i)))
    pk = _h("wots.pk", b"".join(pk_parts))
    return sk, pk, pk_parts


def wots_sign(seed, pub_seed, addr, msg_hash):
    """Sign 32-byte msg_hash with a WOTS+ leaf. Returns list of chain values."""
    digits = _base_w(msg_hash, LEN1)
    digits.extend(_checksum(digits))
    sk, _pk, _parts = wots_keygen(seed, pub_seed, addr)
    sig = []
    for i, d in enumerate(digits):
        sig.append(_chain(sk[i], 0, d, pub_seed, addr + struct.pack(">I", i)))
    return sig, digits


def wots_pk_from_sig(sig, digits, seed_pub_ctx, addr):
    """Recover/compute WOTS+ pk element from signature and message digits."""
    # Note: verification needs the public seed context; to keep verification
    # stateless from secret seed, we include pk_parts commitment in auth path via
    # Merkle leaf = H(wots.pk, pk_element). We use a standard approach where
    # verifier chains sig[i] forward by (W-1-d_i) steps.
    pk_parts = []
    for i, (s, d) in enumerate(zip(sig, digits)):
        pk_parts.append(_chain(s, d, (W - 1) - d, seed_pub_ctx, addr + struct.pack(">I", i)))
    return _h("wots.pk", b"".join(pk_parts))


def _leaf_hash(pk_element, index):
    return _h("merkle.leaf", index, pk_element)


def _node_hash(left, right, height, index):
    return _h("merkle.node", height, index, left, right)


def merkle_build(seed, pub_seed, height=TREE_HEIGHT):
    """Build Merkle tree over WOTS+ leaves. Returns (root, layers).

    layers[0] = leaves, layers[h] has 2**(height-h) nodes.
    Memory: O(2^height) hashes - fine for small heights (1024 leaves = 32KB).
    """
    n = 1 << height
    leaves = []
    for idx in range(n):
        addr = struct.pack(">I", idx)
        _sk, pk, _parts = wots_keygen(seed, pub_seed, addr)
        leaves.append(_leaf_hash(pk, idx))
    layers = [leaves]
    cur = leaves
    for h in range(height):
        nxt = []
        for i in range(0, len(cur), 2):
            nxt.append(_node_hash(cur[i], cur[i + 1], h, i // 2))
        layers.append(nxt)
        cur = nxt
    root = cur[0]
    return root, layers


def merkle_auth_path(layers, index):
    """Authentication path for leaf `index`."""
    path = []
    idx = index
    for h in range(len(layers) - 1):
        sibling = idx ^ 1
        path.append(layers[h][sibling])
        idx >>= 1
    return path


def merkle_verify(root, leaf, index, path):
    cur = leaf
    idx = index
    for h, sibling in enumerate(path):
        if (idx & 1) == 0:
            cur = _node_hash(cur, sibling, h, idx >> 1)
        else:
            cur = _node_hash(sibling, cur, h, idx >> 1)
        idx >>= 1
    return hmac.compare_digest(cur, root)


def generate_keypair(height=TREE_HEIGHT):
    """Create a new PQC keypair. Caller MUST persist the returned dict securely
    and treat `next_index` as monotonic state (never reuse/decrement)."""
    if height < 2 or height > 16:
        raise PQCError("height must be 2..16")
    seed = os.urandom(N)
    pub_seed = os.urandom(N)
    root, layers = merkle_build(seed, pub_seed, height)
    # Cache layers? For large height it's memory-heavy; for height<=12 fine.
    # We store layers only in-memory via wallet runtime; persistent form stores
    # seeds and rebuilds/caches. To keep wallet simple, we persist auth cache
    # lazily by rebuilding (cheap for 1024 leaves) on load.
    pub = {
        "alg": SIG_ALG,
        "h": height,
        "root": root.hex(),
        "pub_seed": pub_seed.hex(),
    }
    priv = {
        "alg": SIG_ALG,
        "h": height,
        "seed": seed.hex(),
        "pub_seed": pub_seed.hex(),
        "root": root.hex(),
        "next_index": 0,
    }
    return pub, priv


def address_from_pub(pub):
    h = _shake(("addr|" + json.dumps(pub, sort_keys=True, separators=(",", ":"))).encode(), 20)
    # base32-ish hex for readability (stdlib-only, no bech32 dep)
    return ADDR_PREFIX + h.hex()


def sign_message(priv, message_bytes):
    """Sign message bytes. Advances `priv['next_index']` in-place - persist it."""
    if priv.get("alg") != SIG_ALG:
        raise PQCError("unsupported key algorithm")
    idx = int(priv.get("next_index", 0))
    height = int(priv["h"])
    if idx > ((1 << height) - 1):
        raise PQCError("key exhausted: generate a new PQC keypair and rotate funds")
    seed = bytes.fromhex(priv["seed"])
    pub_seed = bytes.fromhex(priv["pub_seed"])
    msg_hash = _shake(b"msg|" + message_bytes, N)
    addr = struct.pack(">I", idx)
    digits = _base_w(msg_hash, LEN1)
    digits.extend(_checksum(digits))
    # WOTS+ signature
    sk, pk_element, _ = wots_keygen(seed, pub_seed, addr)
    wots_sig = []
    for i, d in enumerate(digits):
        wots_sig.append(_chain(sk[i], 0, d, pub_seed, addr + struct.pack(">I", i)))
    # Merkle auth path (rebuild - deterministic; cacheable)
    root, layers = merkle_build(seed, pub_seed, height)
    path = merkle_auth_path(layers, idx)
    sig = {
        "alg": SIG_ALG,
        "idx": idx,
        "h": height,
        "wots": [s.hex() for s in wots_sig],
        "path": [p.hex() for p in path],
        "pub_seed": pub_seed.hex(),
        "root": root.hex(),
    }
    priv["next_index"] = idx + 1
    return sig


def verify_signature(pub, message_bytes, sig):
    """Verify signature against public key. Returns True/False."""
    try:
        if sig.get("alg") != SIG_ALG or pub.get("alg") != SIG_ALG:
            return False
        if sig.get("root") != pub.get("root") or sig.get("pub_seed") != pub.get("pub_seed"):
            return False
        idx = int(sig["idx"])
        height = int(sig["h"])
        if height != int(pub["h"]) or idx < 0 or idx > ((1 << height) - 1):
            return False
        msg_hash = _shake(b"msg|" + message_bytes, N)
        digits = _base_w(msg_hash, LEN1)
        digits.extend(_checksum(digits))
        wots_sig = [bytes.fromhex(s) for s in sig["wots"]]
        if len(wots_sig) != LEN or len(digits) != LEN:
            return False
        pub_seed = bytes.fromhex(sig["pub_seed"])
        addr = struct.pack(">I", idx)
        # Recompute WOTS+ public element
        pk_parts = []
        for i, (s, d) in enumerate(zip(wots_sig, digits)):
            pk_parts.append(_chain(s, d, (W - 1) - d, pub_seed, addr + struct.pack(">I", i)))
        pk_element = _h("wots.pk", b"".join(pk_parts))
        leaf = _leaf_hash(pk_element, idx)
        path = [bytes.fromhex(p) for p in sig["path"]]
        if len(path) != height:
            return False
        root = bytes.fromhex(pub["root"])
        return merkle_verify(root, leaf, idx, path)
    except Exception:
        return False


def pub_fingerprint(pub):
    return _shake(json.dumps(pub, sort_keys=True).encode(), 8).hex()
