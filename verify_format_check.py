"""Falsification battery for assert_format_matches_provided().

The rule: a check that has never failed proves nothing. Each case below must raise for one
specific reason, and the last case demonstrates that the PREVIOUS version of the check
could not fail at all.
"""
import contextlib
import hashlib
import importlib.util
import io
import time
import types
from pathlib import Path

BASE = Path(__file__).resolve().parent
INDEX, PREV = 7, "a" * 64
TXS = [{"amount": i + 1, "recipient": f"r{i}", "sender": f"s{i}"} for i in range(3)]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mod = load("node_under_test", BASE / "provided" / "blockchain_assessment.py")
h = load("harness", BASE / "harness.py")

TRUE_CONTENT = h._content
JSON_CONTENT = lambda i, p, t, n: f"{i}{p}{__import__('json').dumps(t)}{n}".encode()

print("byte check: str(txs) vs json.dumps(txs) differ for this payload:",
      TRUE_CONTENT(INDEX, PREV, TXS, 0) != JSON_CONTENT(INDEX, PREV, TXS, 0))

results = []


def expect_raise(label, fn, needle):
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            fn()
    except AssertionError as e:
        ok = needle in str(e)
        results.append(ok)
        print(f"  {'PASS' if ok else 'FAIL'}  {label}\n         -> AssertionError: {str(e)[:120]}")
        return
    except Exception as e:  # noqa: BLE001
        results.append(False)
        print(f"  FAIL  {label}\n         -> wrong exception: {type(e).__name__}: {e}")
        return
    results.append(False)
    print(f"  FAIL  {label}\n         -> NO exception: the check is blind to this")


print("\n=== 1. the real model must PASS (and mine, not scan) ===")
t0 = time.perf_counter()
n_ours, n_ctrl = h.assert_format_matches_provided(mod)
dt = time.perf_counter() - t0
ok = n_ours == 3 and n_ctrl == 3
results.append(ok)
print(f"  {'PASS' if ok else 'FAIL'}  {n_ours} nonces accepted by the submission, "
      f"{n_ctrl} control nonces rejected; {dt:.2f}s")

print("\n=== 2. a WRONG byte model in the harness must FAIL the check ===")
# A third format, differing from BOTH the true model and the JSON control, so this exercises
# the "submission rejects a nonce our bytes meet" path rather than the byte-identical guard.
SEP_CONTENT = lambda i, p, t, n: f"{i}:{p}{t}{n}".encode()
h._content = SEP_CONTENT
expect_raise("harness bytes = '<index>:' separator (differs from true model and from control)",
             lambda: h.assert_format_matches_provided(mod), "REJECTS nonce")
h._content = JSON_CONTENT
expect_raise("harness bytes = the control (JSON) exactly — guard fires before any verdict",
             lambda: h.assert_format_matches_provided(mod), "byte-identical")
h._content = TRUE_CONTENT

print("\n=== 3. a control identical to the model must be refused, not silently passed ===")
real_json = h.json
h.json = types.SimpleNamespace(dumps=str)   # makes control() == ours()
expect_raise("control payload byte-identical to ours", lambda: h.assert_format_matches_provided(mod),
             "byte-identical")
h.json = real_json

print("\n=== 4. valid_proof that ignores its arguments must be caught ===")
real_vp = mod.Blockchain.valid_proof
mod.Blockchain.valid_proof = staticmethod(lambda *a: True)
expect_raise("valid_proof returns constant True", lambda: h.assert_format_matches_provided(mod),
             "ACCEPTS nonce")
mod.Blockchain.valid_proof = staticmethod(lambda *a: False)
expect_raise("valid_proof returns constant False", lambda: h.assert_format_matches_provided(mod),
             "REJECTS nonce")
mod.Blockchain.valid_proof = real_vp

print("\n=== 5. WHY THIS EXISTS: the previous check, on the SAME wrong bytes ===")


def old_check(content_fn, n=200):
    """The replaced implementation, verbatim in shape: count verdict agreement."""
    bc = mod.Blockchain.__new__(mod.Blockchain)
    target = bc.difficulty_target
    agree = sum(
        bc.valid_proof(INDEX, PREV, TXS, k)
        == (hashlib.sha256(content_fn(INDEX, PREV, TXS, k)).hexdigest()[:len(target)] == target)
        for k in range(n)
    )
    return agree, n


for label, fn in (("correct bytes", TRUE_CONTENT), ("WRONG bytes (txs as JSON)", JSON_CONTENT)):
    agree, n = old_check(fn)
    print(f"  old check with {label:26s}: {agree}/{n} agreement "
          f"-> {'CERTIFIES it' if agree == n else 'rejects it'}")
old_wrong = old_check(JSON_CONTENT)
old_is_blind = old_wrong[0] == old_wrong[1]
results.append(old_is_blind)
print(f"  {'PASS' if old_is_blind else 'FAIL'}  old check cannot distinguish the wrong format "
      f"(this is the defect that was fixed)")

print(f"\n{sum(results)}/{len(results)} cases behaved as required")
raise SystemExit(0 if all(results) else 1)
