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


PROOF_SCHEMA_VERSION = "mft-merkle-proof/v1"
ALLOWED_DOMAINS = {"leaf","node","tx","txnode","utxo","utxonode","block","commit","chain-info","utxo-set"}

def _is_hex64(x):
    return isinstance(x, str) and len(x) == 64 and all(c in "0123456789abcdefABCDEF" for c in x)

def verify(proof, leaf=None, domain_node="node", max_path=64):
    """Verify a Merkle inclusion proof (stdlib-only). Experimental/unaudited.
    Returns bool. Use verify_detailed() for reasons."""
    return bool(verify_detailed(proof, leaf=leaf, domain_node=domain_node, max_path=max_path).get("valid"))

def verify_detailed(proof, leaf=None, domain_node="node", max_path=64):
    """Strict verification with explicit reasons. Not a security/compliance guarantee."""
    try:
        if not isinstance(proof, dict):
            return {"valid": False, "reason": "proof must be an object"}
        if domain_node and str(domain_node) not in ALLOWED_DOMAINS:
            return {"valid": False, "reason": "domain not allowed"}
        if leaf is None:
            cur = proof.get("leaf")
        else:
            cur = leaf if _is_hex64(leaf) else leaf_hash_hex(leaf)
        if not _is_hex64(cur): return {"valid": False, "reason": "invalid leaf hash"}
        root = proof.get("root")
        if not _is_hex64(root): return {"valid": False, "reason": "invalid root hash"}
        path = proof.get("path") or []
        if not isinstance(path, list): return {"valid": False, "reason": "invalid path"}
        if len(path) > int(max_path): return {"valid": False, "reason": "path too long"}
        cur = cur.lower()
        for step in path:
            if not isinstance(step, dict): return {"valid": False, "reason": "invalid path step"}
            side = step.get("side")
            sib = step.get("hash")
            if side not in ("L","R") or not _is_hex64(sib): return {"valid": False, "reason": "invalid path step"}
            if side == "R":
                cur = node_hash_hex(cur, sib, domain_node)
            else:
                cur = node_hash_hex(sib, cur, domain_node)
        ok = cur == str(root).lower()
        return {"valid": ok, "reason": "ok" if ok else "root mismatch", "schema_version": PROOF_SCHEMA_VERSION,
                "domain_node": str(domain_node)}
    except Exception as e:
        return {"valid": False, "reason": "verification error"}


def commitment(obj, domain="commit"):
    """Single-object commitment (canonical JSON hash)."""
    return leaf_hash_hex(obj, domain)
