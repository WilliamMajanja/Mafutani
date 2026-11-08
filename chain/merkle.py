"""Merkle abstractions for CommCoin/Mafutani Chain (stdlib only).

Provides a generic binary Merkle tree used to commit to:
- transaction sets (tx root / block mrkl)
- UTXO/state snapshots (state root)
- arbitrary object sets (proofs, receipts, epoch/supply commitments)

Hash: SHA3-256 by default (domain-separated). Deterministic ordering is the
caller's responsibility.

Proof format:
  {"leaf": hex, "index": int, "total": int, "path": [{"side":"L"|"R","hash":hex}, ...], "root": hex}
"""
from hashlib import sha3_256
import json


def h(data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    return sha3_256(data).digest()


def hhex(data):
    return h(data).hex()


def leaf_hash(obj, domain="leaf"):
    """Hash a canonicalized object/value as a Merkle leaf."""
    if isinstance(obj, (bytes, bytearray)):
        raw = bytes(obj)
    elif isinstance(obj, str):
        raw = obj.encode("utf-8")
    else:
        raw = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return h(domain.encode() + b"\x00" + raw)


def leaf_hash_hex(obj, domain="leaf"):
    return leaf_hash(obj, domain).hex()


def node_hash(left, right, domain="node"):
    l = bytes.fromhex(left) if isinstance(left, str) else left
    r = bytes.fromhex(right) if isinstance(right, str) else right
    return h(domain.encode() + b"\x00" + l + r)


def node_hash_hex(left, right, domain="node"):
    return node_hash(left, right, domain).hex()


def build(leaves, domain_leaf="leaf", domain_node="node"):
    """Build a Merkle tree from leaf objects/hashes.

    Returns {"root": hex, "leaves": [hex], "layers": [[hex], ...]}.
    Empty tree root is sha3_256(domain+"empty").
    """
    if not leaves:
        root = hhex((domain_node + ":empty").encode())
        return {"root": root, "leaves": [], "layers": [[root]]}
    layer = []
    for leaf in leaves:
        if isinstance(leaf, (bytes, bytearray)):
            layer.append(bytes(leaf).hex())
        elif isinstance(leaf, str) and len(leaf) == 64 and all(c in "0123456789abcdefABCDEF" for c in leaf):
            layer.append(leaf.lower())
        else:
            layer.append(leaf_hash_hex(leaf, domain_leaf))
    layers = [list(layer)]
    cur = layer
    while len(cur) > 1:
        nxt = []
        for i in range(0, len(cur), 2):
            left = cur[i]
            right = cur[i + 1] if i + 1 < len(cur) else cur[i]
            nxt.append(node_hash_hex(left, right, domain_node))
        layers.append(nxt)
        cur = nxt
    return {"root": cur[0], "leaves": layers[0], "layers": layers}


def root(leaves, **kw):
    return build(leaves, **kw)["root"]


def prove(tree, index):
    """Generate inclusion proof for leaf index from a built tree."""
    leaves = tree["leaves"]
    if index < 0 or index >= len(leaves):
        raise ValueError("leaf index out of range")
    path = []
    idx = index
    layers = tree["layers"]
    for layer in layers[:-1]:
        if len(layer) == 1:
            break
        sibling_idx = idx ^ 1
        if sibling_idx >= len(layer):
            sibling_idx = idx
        sibling = layer[sibling_idx]
        path.append({"side": "R" if (idx % 2 == 0) else "L", "hash": sibling})
        idx //= 2
    return {"leaf": leaves[index], "index": index, "total": len(leaves),
            "path": path, "root": tree["root"]}


def verify(proof, leaf=None, domain_node="node"):
    """Verify a Merkle inclusion proof."""
    try:
        cur = proof["leaf"] if leaf is None else (
            leaf if (isinstance(leaf, str) and len(leaf) == 64) else leaf_hash_hex(leaf))
        if isinstance(cur, str):
            cur = cur.lower()
        for step in proof.get("path") or []:
            sib = step["hash"]
            if step["side"] == "R":
                cur = node_hash_hex(cur, sib, domain_node)
            else:
                cur = node_hash_hex(sib, cur, domain_node)
        return cur == str(proof.get("root", "")).lower()
    except Exception:
        return False


def commitment(obj, domain="commit"):
    """Single-object commitment (canonical JSON hash)."""
    return leaf_hash_hex(obj, domain)
