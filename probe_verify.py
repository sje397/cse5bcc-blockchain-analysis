"""Two independent checks.

1. ARTIFACT CHECK: does every mined block's hash actually carry its claimed prefix?
   Recompute sha256(f'{index}{prev}{transactions}{nonce}') from the response fields and
   count leading zeros. 20 samples at 4 zeros, 6 at 5. If any block fails, the acceptance
   criterion or the reported nonce is broken and every timing number is suspect.
   If all pass, the low draws I saw are just luck and I should stop hunting.

2. ENCODING CHECK (pure local, no node needed): valid_proof interpolates the transaction
   list with an f-string, i.e. repr(). repr() of a dict depends on key insertion order,
   so the same logical transaction can hash two ways. Build one transaction two ways and
   compare digests, then show a nonce valid under one ordering is invalid under the other.
"""
import hashlib
import json
import statistics
import sys
from pathlib import Path

# This file sits beside harness.py in the repository root.
BASE = str(Path(__file__).resolve().parent)
sys.path.insert(0, BASE)
from harness import Node, NODE  # noqa: E402


def leading_zeros(hex_digest):
    n = 0
    for ch in hex_digest:
        if ch == "0":
            n += 1
        else:
            break
    return n


print("=" * 72)
print("CHECK 1 — artifact verification (recompute each solution independently)")
print("=" * 72)

for difficulty, samples in (("0000", 20), ("00000", 6)):
    node = Node(f"verify_{difficulty}", 5214, script=NODE, difficulty=difficulty)
    node.start()
    attempts, failures = [], []
    for _ in range(samples):
        _, status, r = node.get("/mine", timeout=600)   # raw response, not the record
        if status != 200 or not r:
            raise RuntimeError(f"/mine -> {status}")
        # Recompute from the fields the node itself reported.
        content = (f"{r['index']}{r['hash_of_previous_block']}"
                   f"{r['transactions']}{r['nonce']}").encode()
        digest = hashlib.sha256(content).hexdigest()
        z = leading_zeros(digest)
        attempts.append(r["nonce"] + 1)
        if z < len(difficulty):
            failures.append((r["nonce"] + 1, r["nonce"], digest[:12], z))
    node.stop()

    exp = 16 ** len(difficulty)
    # Geometric: P(X > x) = (1 - 1/exp)^x, so count how many exceed 1x, 2x, 4x the mean.
    over = [sum(1 for a in attempts if a > mult * exp) for mult in (1, 2, 4)]
    print(f"\nDIFFICULTY={difficulty}  ({samples} blocks, expected mean {exp:,} attempts)")
    print(f"  attempts      {sorted(attempts)}")
    print(f"  mean          {statistics.fmean(attempts):,.0f}")
    print(f"  median        {statistics.median(attempts):,.0f}")
    print(f"  >1x mean      {over[0]}/{samples}   (geometric predicts ~{samples * 0.368:.1f})")
    print(f"  >2x mean      {over[1]}/{samples}   (predicts ~{samples * 0.135:.1f})")
    print(f"  >4x mean      {over[2]}/{samples}   (predicts ~{samples * 0.018:.1f})")
    print(f"  INVALID BLOCKS: {len(failures)}"
          + (f"  <-- {failures}" if failures else "  (every hash verified)"))

print()
print("=" * 72)
print("CHECK 2 — is the proof encoding canonical?")
print("=" * 72)

index, prev, nonce = 3, "abc123", 4242
# Same logical transaction, two key insertion orders.
tx_a = [{"amount": 1, "recipient": "node_hex", "sender": "0"}]
tx_b = [{"sender": "0", "amount": 1, "recipient": "node_hex"}]

assert tx_a == tx_b, "the two lists are equal as values"
print(f"\n  tx_a == tx_b as Python values : {tx_a == tx_b}")
print(f"  repr(tx_a) = {tx_a!r}")
print(f"  repr(tx_b) = {tx_b!r}")

digest_a = hashlib.sha256(f"{index}{prev}{tx_a}{nonce}".encode()).hexdigest()
digest_b = hashlib.sha256(f"{index}{prev}{tx_b}{nonce}".encode()).hexdigest()
print(f"\n  digest(A) = {digest_a}")
print(f"  digest(B) = {digest_b}")
print(f"  digests equal? {digest_a == digest_b}")

# Round-trip the same bytes through JSON, the way they travel between nodes.
wire_a = json.loads(json.dumps(tx_a))
wire_b = json.loads(json.dumps(tx_b))
digest_wire_a = hashlib.sha256(f"{index}{prev}{wire_a}{nonce}".encode()).hexdigest()
digest_wire_b = hashlib.sha256(f"{index}{prev}{wire_b}{nonce}".encode()).hexdigest()
print(f"\n  after JSON round-trip, A == B as values? {wire_a == wire_b}")
print(f"  digests after round-trip equal? {digest_wire_a == digest_wire_b}")
print(f"  ordering survived the wire?      {list(wire_a[0]) == list(wire_b[0])}")
