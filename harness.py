#!/usr/bin/env python3
"""
harness.py — the measurement harness for CSE5BCC Assessment 1.

Design notes (these matter for the write-up):

1. Every experiment runs the same programs with the same code path. Conditions are
   varied only through environment variables (DIFFICULTY, AUTO_SYNC), never by editing
   source between conditions, and the program actually executed is recorded in every
   result row.

   The two programs differ in one way that constrains the whole design. node.py reads
   DIFFICULTY; the provided submission hard-codes its target at "0000" and ignores the
   environment entirely (verified at start-up, not assumed -- see assert_provided_shape).
   So every arm that varies difficulty necessarily runs the modified node, and a node
   running the provided file is always a fixed-difficulty node. That is a property of the
   submission rather than a limitation of this harness, and it is reported as a finding
   instead of being worked around: see exp_race for the one arm it makes unrunnable, and
   why running that arm anyway would have produced a confident false negative.

2. Two clocks are recorded for each block:
     - wall_time   : measured by this harness around the HTTP call. This is the number a
                     user experiences, and it therefore includes HTTP overhead and the
                     serialisation of the JSON response (which grows with chain length).
     - server_time : the program's own mining_time_seconds.
   Reporting both, and their difference, makes the overhead visible instead of hidden.

3. ATTEMPTS vs RATE. For a difficulty target of k leading hex zeros the number of hash
   attempts before success is a geometric random variable with mean 16^k — so a single
   block's *duration* is dominated by luck and is a poor measurement. The attempt count
   (nonce + 1, because the solver tests nonce 0 first) recovers the luck, and

       attempt_rate = (nonce + 1) / mining_time

   is a luck-independent measure of how much work the implementation can do per second.
   Every claim about "which factor makes mining slower" is made against attempt_rate,
   not against duration.

4. The harness never writes to the repository tree; all output goes to results/ and
   logs/ alongside it.
"""

import argparse
import atexit
import csv
import hashlib
import importlib.util
import json
import os
import re
import signal
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
PROVIDED = BASE / "provided" / "blockchain_assessment.py"
NODE = BASE / "node.py"
LOGS = BASE / "logs"
RESULTS = BASE / "results"
LOGS.mkdir(exist_ok=True)
RESULTS.mkdir(exist_ok=True)

VENV_PY = BASE / ".venv" / "bin" / "python"
PYTHON = str(VENV_PY) if VENV_PY.exists() else sys.executable


# The provided submission hard-codes its mining target (difficulty_target = "0000") and
# reads no environment variable, so a node running it can only ever mine at this target.
# Checked against the file at start-up rather than assumed: that assumption is what every
# "provided" row in the results rests on, and if the file ever starts honouring DIFFICULTY
# those rows would carry a condition no node was ever configured to run under.
PROVIDED_DIFFICULTY = "0000"

# Nodes currently alive, so an interrupted run cannot leave a listener on a port that the
# next run then refuses to measure past. Populated in Node.start(), drained in Node.stop().
_LIVE_NODES = []


def _content(index, prev_hash, txs, nonce) -> bytes:
    """The harness's model of the bytes the submission hashes to test a nonce.

    Mirrors `Blockchain.valid_proof`'s f-string exactly. Note that `txs` is interpolated
    with str(), so a transaction list renders as its Python repr — `[]` for genesis — and
    NOT as JSON; the two are identical until a payload contains a quote, which is why the
    distinction is worth stating. The submission itself is the authority on this format.

    Not a guess, and not to be "tidied": assert_format_matches_provided() mines nonces
    that meet the target under these bytes and requires the provided valid_proof to accept
    every one, then mines nonces that only a deliberately wrong format (txs as JSON) meets
    and requires it to reject those. Nonces both formats reject prove nothing — they agree
    by both saying no — so the check is built only on nonces where the answers must differ.
    """
    return f"{index}{prev_hash}{txs}{nonce}".encode()


def _fingerprints(chain):
    """One short content fingerprint per block, so block survival is judged on bytes.

    Deliberately per-block rather than per-chain: the question these experiments ask is
    which individual blocks survived a reconciliation, and a whole-chain hash answers only
    whether the chains are identical. Twelve hex characters is ~48 bits — ample to tell
    apart blocks that are this structurally similar, and short enough to read in a report.
    """
    return [hashlib.sha256(json.dumps(blk, sort_keys=True).encode()).hexdigest()[:12]
            for blk in chain]



def genesis_predicate(chain, target):
    """Is chain[0] a genuine genesis of the provided program, mined at `target`?

    Returns (ok, detail). Both properties are recomputed from the chain's own bytes rather
    than accepted from the node, because the submission's /blockchain route reports no
    difficulty at all and there is nothing else on the wire to check:

    1. the recorded previous-hash must equal the hash that program seeds genesis with
       (sha256 of json.dumps("genesis_block", sort_keys=True)) — a fingerprint of how the
       file builds its first block;
    2. the nonce must meet `target` on the payload the submission actually hashes.

    (2) alone cannot separate a fresh node at our target from any node whose first four
    hex digits happen to be zero, and the only nonce available is the one reported. (1)
    ties the reply to this program; (2) ties it to the configured target.
    """
    if not chain:
        return False, "the node returned an empty chain"
    g = chain[0]
    seed = hashlib.sha256(json.dumps("genesis_block", sort_keys=True).encode()).hexdigest()
    recorded = g.get("hash_of_previous_block")
    if recorded != seed:
        return False, (f"genesis previous-hash {str(recorded)[:16]}... is not this "
                       f"program's seed hash {seed[:16]}...")
    digest = hashlib.sha256(
        _content(g.get("index"), recorded, [], g.get("nonce"))).hexdigest()
    if digest[:len(target)] != target:
        return False, (f"genesis nonce {g.get('nonce')} hashes to {digest[:16]}..., which "
                       f"does not meet the configured target {target!r}")
    return True, f"nonce {g.get('nonce')} meets {target!r} on this program's own seed"


def assert_provided_shape():
    """Verify from source that the submission is the fixed-target file this rests on.

    Every "provided" row claims a condition, and for this program the condition is reported
    nowhere — it is inferred from the genesis proof-of-work, which is only valid while (a)
    the target is hard-coded and (b) it equals PROVIDED_DIFFICULTY. So it is read from the
    file instead of believed: if the submission ever changes, the run stops here with one
    clear line rather than producing a set of rows asserting a target nothing mined at.
    """
    src = PROVIDED.read_text()
    found = re.search(r"difficulty_target\s*=\s*[\"']([0-9a-fA-F]+)[\"']", src)
    if not found:
        raise RuntimeError(
            f"{PROVIDED.name}: no literal difficulty_target assignment found, so the "
            f"target this harness assumes for the provided program cannot be confirmed")
    if found.group(1) != PROVIDED_DIFFICULTY:
        raise RuntimeError(
            f"{PROVIDED.name}: difficulty_target is {found.group(1)!r} but this harness "
            f"assumes {PROVIDED_DIFFICULTY!r} — the provided-node identity check and every "
            f"'provided' row depend on that value")
    if re.search(r"os\.environ|getenv", src):
        raise RuntimeError(
            f"{PROVIDED.name}: now reads an environment variable, so it may honour "
            f"DIFFICULTY — the fixed-target assumption no longer holds and the provided/"
            f"modified split stops being a controlled comparison")
    return found.group(1)


# ----------------------------------------------------------------------------- nodes

def port_is_free(port):
    """True when nothing is listening on 127.0.0.1:port.

    CORRECTED 2026-09-23, after this function was *measured* returning True for a port
    a live node was serving. The previous implementation bound the port with
    SO_REUSEADDR set, and on macOS that option lets a 127.0.0.1 bind succeed while
    another socket holds 0.0.0.0:port -- which is exactly how node.py binds
    (app.run(host='0.0.0.0')). The probe therefore reported "free" for every running
    node, so the pre-flight it feeds could never fire. Measured on this machine:
    with SO_REUSEADDR the bind succeeds, without it the bind raises EADDRINUSE.
    The old docstring asserted "binding is the only reliable test" and the line below
    it removed that property -- the comment was load-bearing and wrong.

    A refused connection on loopback is definitive for LISTENERS, which is the only
    thing that can answer on the port and therefore the only thing this guard exists
    to exclude: a listener accepts, and nothing listening returns ECONNREFUSED
    immediately. (Stated precisely rather than as a general claim: a socket that is
    bound but not listening would also read as free here. That state cannot serve a
    request, so it is not what this is protecting against -- but it is a real limit of
    the probe, and the previous version of this docstring asserted a property the code
    did not have, which is how the bug survived.) Connect is strictly better than a
    bind, which also fails for a port merely in TIME_WAIT and would abort experiments
    for no reason. The bind remains only as a conservative fallback for the case
    connect cannot resolve.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.25)
        try:
            s.connect(("127.0.0.1", port))
            return False
        except ConnectionRefusedError:
            return True
        except OSError:
            pass  # inconclusive (timeout etc.) -- fall through to the bind check
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


class Node:
    """One node process, managed over HTTP."""

    def __init__(self, name, port, script=NODE, difficulty="000", auto_sync=False,
                 auto_sync_interval=2.0):
        if Path(script) == PROVIDED and difficulty != PROVIDED_DIFFICULTY:
            raise ValueError(
                f"{name}: the provided program hard-codes its target at "
                f"{PROVIDED_DIFFICULTY!r} and reads no environment variable, so it cannot "
                f"mine at {difficulty!r}. A node asked for a target the program cannot "
                f"express produces a row asserting a condition that was never in force, so "
                f"the pairing is refused here rather than measured. Either pass "
                f"difficulty={PROVIDED_DIFFICULTY!r}, or run the modified node ({NODE.name}) "
                f"which reads DIFFICULTY."
            )
        self.name = name
        self.port = port
        self.script = script
        self.difficulty = difficulty
        self.auto_sync = auto_sync
        self.auto_sync_interval = auto_sync_interval
        self.proc = None
        self.log_path = LOGS / f"{name}.log"
        self.startup_seconds = None
        self.observed_difficulty = None   # confirmed on the wire in _assert_identity()
        self.identity_basis = None        # HOW that confirmation was made, per script
        self.session = requests.Session()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self, timeout=600):
        # Pre-flight: a listener already on this port will answer the readiness poll
        # below, so we would measure it instead of the process we spawn -- and every
        # number in the results file would belong to a node we did not configure.
        # Refuse rather than measure whichever process happens to be listening.
        if not port_is_free(self.port):
            raise RuntimeError(
                f"{self.name}: port {self.port} is already in use before start - a node "
                f"from an earlier run is probably still alive; refusing to measure "
                f"whichever process happens to be listening")

        env = dict(os.environ)
        env["DIFFICULTY"] = self.difficulty
        env["AUTO_SYNC"] = "1" if self.auto_sync else "0"
        env["AUTO_SYNC_INTERVAL"] = str(self.auto_sync_interval)
        env["PYTHONUNBUFFERED"] = "1"
        # The genesis block is mined at import, *before* the HTTP server binds, so at
        # high difficulty the node is unreachable for that whole time. We measure it.
        self.log = open(self.log_path, "wb")
        t0 = time.perf_counter()
        self.proc = subprocess.Popen(
            [PYTHON, str(self.script), str(self.port)],
            stdout=self.log, stderr=subprocess.STDOUT,
            cwd=str(BASE), env=env,
        )
        while time.perf_counter() - t0 < timeout:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"{self.name} exited with {self.proc.returncode}; see {self.log_path}")
            try:
                r = self.session.get(f"{self.url}/blockchain", timeout=2)
                if r.status_code == 200:
                    self.startup_seconds = time.perf_counter() - t0
                    break
            except requests.RequestException:
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError(f"{self.name} did not become ready within {timeout}s")

        # Readiness above proves only that *something* answers on this port. Confirm it
        # is the process we spawned before any measurement is attributed to this node.
        # The window is real rather than theoretical: the poll can win the race against
        # a child that fails to bind and dies a moment later, and it then returns a live
        # answer from a stranger as though it were ours.
        self._assert_identity()
        _LIVE_NODES.append(self)
        return self.startup_seconds

    def _assert_identity(self):
        """Raise unless the responder is demonstrably the node we just started.

        Three checks, kept separate because each has its own blind spot: liveness rules
        out a child that died on bind, the reported difficulty ties the reply to the
        condition we configured, and chain length ties it to a chain never yet used.
        """
        if self.proc.poll() is not None:
            raise RuntimeError(
                f"{self.name} answered on port {self.port} but our own process had "
                f"already exited with {self.proc.returncode} - the reply came from a "
                f"different process; see {self.log_path}")
        _, status, data = self.get("/blockchain", timeout=10)
        if status != 200 or not isinstance(data, dict):
            raise RuntimeError(f"{self.name}: /blockchain unreadable (status {status})")
        observed = data.get("difficulty")
        if self.script == PROVIDED:
            # The submission's /blockchain route reports no difficulty at all, so the
            # condition cannot be read off the wire. It is instead inferred from the
            # genesis proof-of-work: a chain whose first block was mined at our target on
            # this program's own seed is evidence the reply came from a fresh provided
            # node. The weaker inference is recorded as such -- see identity_basis.
            ok, detail = genesis_predicate(data.get("chain"), PROVIDED_DIFFICULTY)
            if not ok:
                raise RuntimeError(
                    f"{self.name}: cannot confirm the responder is a fresh provided node "
                    f"at target {PROVIDED_DIFFICULTY!r} - {detail}")
            observed = PROVIDED_DIFFICULTY
            self.identity_basis = (
                f"genesis proof-of-work at {PROVIDED_DIFFICULTY!r} ({detail}); the "
                f"submission reports no difficulty field to check instead")
        else:
            if observed is None:
                raise RuntimeError(
                    f"{self.name}: responding node does not report its difficulty, so the "
                    f"condition actually measured cannot be verified")
            if observed != self.difficulty:
                raise RuntimeError(
                    f"{self.name}: asked for difficulty {self.difficulty!r} but the "
                    f"responding node reports {observed!r} - not the process we spawned")
            self.identity_basis = f"difficulty reported by the node as {observed!r}"
        if data.get("length") != 1:
            raise RuntimeError(
                f"{self.name}: expected a fresh chain (genesis only, length 1) but the "
                f"responding node reports length {data.get('length')} - it was already "
                f"running before we started it")
        # Record what the node said rather than what we asked for, so a result row can
        # never carry a parameter that was not confirmed on the wire.
        self.observed_difficulty = observed

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
        if getattr(self, "log", None):
            self.log.close()
        if self in _LIVE_NODES:
            _LIVE_NODES.remove(self)

    def get(self, path, timeout=300):
        t0 = time.perf_counter()
        r = self.session.get(f"{self.url}{path}", timeout=timeout)
        elapsed = time.perf_counter() - t0
        try:
            data = r.json()
        except ValueError:
            data = None
        return elapsed, r.status_code, data

    def raw_get(self, path, timeout=300):
        """GET without the shared Session.

        A requests.Session is not documented as thread-safe and one is in use here from
        two threads at once (a mining call in the background, a sync on the main thread).
        Connection-pool reuse across threads is a real source of spurious failures, and a
        spurious failure in the race experiment would look exactly like the result we are
        testing for. So the concurrent call gets its own connection.
        """
        t0 = time.perf_counter()
        r = requests.get(f"{self.url}{path}", timeout=timeout)
        elapsed = time.perf_counter() - t0
        try:
            data = r.json()
        except ValueError:
            data = None
        return elapsed, r.status_code, data


    def post(self, path, payload, timeout=60):
        t0 = time.perf_counter()
        r = self.session.post(f"{self.url}{path}", json=payload, timeout=timeout)
        elapsed = time.perf_counter() - t0
        try:
            data = r.json()
        except ValueError:
            data = None
        return elapsed, r.status_code, data

    def chain(self, timeout=300):
        _, _, data = self.get("/blockchain", timeout=timeout)
        return data

    def mine(self, timeout=600):
        """One block. Returns a record of the measurement."""
        wall, status, data = self.get("/mine", timeout=timeout)
        if status != 200 or data is None:
            raise RuntimeError(f"{self.name} /mine -> {status}")
        attempts = data["nonce"] + 1   # nonce 0 is tested first
        server = data["mining_time_seconds"]
        return {
            "node": self.name,
            # the difficulty the node CONFIRMED on the wire, not the one we intended:
            # an intended-only value lets a row assert a condition never measured.
            "difficulty": len(self.observed_difficulty),
            "block_index": data["index"],
            "attempts": attempts,
            "wall_time_s": round(wall, 6),
            "server_time_s": server,
            "overhead_s": round(wall - server, 6),
            "attempt_rate": attempts / server if server > 0 else None,
            "attempt_rate_wall": attempts / wall if wall > 0 else None,
            "nonce": data["nonce"],
        }

    def register(self, others):
        _, status, data = self.post("/nodes/add_nodes",
                                    {"nodes": [o.url for o in others]})
        if status != 201:
            raise RuntimeError(f"{self.name} add_nodes -> {status}")
        return data


def start_network(spec, difficulty=PROVIDED_DIFFICULTY, script=PROVIDED, auto_sync=False,
                  auto_sync_interval=2.0, mesh=True):
    """spec: list of (name, port).

    The default script is the program AS SUPPLIED, and the default difficulty is the
    target that program actually mines at — the two defaults belong together, because a
    provided node cannot be asked for any other target.

    Every experiment that characterises behaviour must measure that artifact, not the
    modified node — otherwise the results describe code the marker was not given. Pass
    script=NODE explicitly where the modification is the thing under test (the autosync
    and race experiments), since only that node reads DIFFICULTY.
    """
    nodes = [Node(n, p, script=script, difficulty=difficulty,
                  auto_sync=auto_sync, auto_sync_interval=auto_sync_interval)
             for n, p in spec]
    for n in nodes:
        n.start()
    if mesh:
        for n in nodes:
            n.register([o for o in nodes if o is not n])
    return nodes


def stop_all(nodes):
    for n in nodes:
        n.stop()


def _shutdown_nodes():
    """Stop every node this process still owns. Safe to call repeatedly."""
    while _LIVE_NODES:
        try:
            _LIVE_NODES[-1].stop()
        except Exception:
            _LIVE_NODES.pop()


def _install_shutdown_hooks():
    """Make an interrupt stop the nodes, not just the harness.

    A killed harness leaves its child Flask processes listening: the children are orphaned,
    the next run finds those ports occupied, and the failure surfaces later as a
    connection refusal from a node that looks unrelated to the run that caused it. This
    bounds that rather than eliminating it — SIGKILL cannot be intercepted, and a node
    blocked inside a request may not act on the signal until its socket call returns.
    """
    atexit.register(_shutdown_nodes)

    def _on_signal(signum, _frame):
        _shutdown_nodes()
        raise SystemExit(128 + signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_signal)


# ------------------------------------------------------------------------- utilities

def write_csv(path, rows, fieldnames=None):
    """Write rows as CSV with the header taken as the UNION of the rows' keys.

    Defaulting the header to rows[0] is what crashed the first full run. These tables are
    heterogeneous on purpose -- a per-node row beside a TOTAL row, or a row that gains a
    column once it has been measured -- and DictWriter raises ValueError on any key it was
    not given a column for. The alternative, giving every row a full set of placeholder
    columns up front, is worse than useless: a placeholder reads as a measurement.

    Union in first-seen order; a row without a column is written empty (restval) so that
    "not applicable to this row" is explicit in the file rather than incidental.
    """
    if not rows:
        print(f"  (no rows for {path.name})")
        return
    if fieldnames is None:
        fieldnames = []
        for row in rows:
            for key in row:
                if key not in fieldnames:
                    fieldnames.append(key)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, restval="")
        w.writeheader()
        w.writerows(rows)
    print(f"  wrote {path.relative_to(BASE)} ({len(rows)} rows, {len(fieldnames)} cols)")


def dump_json(path, obj):
    with open(path, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    print(f"  wrote {path.relative_to(BASE)}")


def describe(values):
    if not values:
        return {}
    return {
        "n": len(values),
        "mean": round(statistics.fmean(values), 6),
        "median": round(statistics.median(values), 6),
        "stdev": round(statistics.stdev(values), 6) if len(values) > 1 else 0.0,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
    }


def chain_bytes(chain_data):
    return len(json.dumps(chain_data).encode())


# ---------------------------------------------------------------------- experiment 1

def exp_difficulty(levels=(3, 4, 5, 6), budget_s=180, min_reps=5, max_reps=300,
                   port=5101):
    """Mining cost vs difficulty target length. One node, one difficulty per level.

    Reps are chosen adaptively from two warm-up blocks so that each level uses
    roughly `budget_s` of mining time, with a floor so that even the slow levels get
    a distribution rather than a single sample.
    """
    print("\n=== experiment: difficulty ===")
    rows = []
    summary = []
    for k in levels:
        difficulty = "0" * k
        node = Node(f"diff{k}", port, difficulty=difficulty)
        startup = node.start(timeout=max(600, budget_s * 4))
        try:
            warm = [node.mine() for _ in range(2)]
            est = statistics.fmean(r["server_time_s"] for r in warm)
            reps = int(max(min_reps, min(max_reps, budget_s / max(est, 1e-4))))
            print(f"  difficulty {k}: startup {startup:.2f}s, "
                  f"warm-up mean {est*1000:.1f}ms -> {reps} reps")
            recs = []
            for i in range(reps):
                rec = node.mine()
                rec["startup_seconds"] = round(startup, 4)
                rec["rep"] = i
                recs.append(rec)
                if (i + 1) % 25 == 0:
                    print(f"    {i+1}/{reps}")
            rows.extend(recs)
            times = [r["wall_time_s"] for r in recs]
            rates = [r["attempt_rate"] for r in recs if r["attempt_rate"]]
            attempts = [r["attempts"] for r in recs]
            summary.append({
                "difficulty": k,
                "expected_attempts_16pow_k": 16 ** k,
                "observed_mean_attempts": round(statistics.fmean(attempts), 1),
                "mean_wall_s": round(statistics.fmean(times), 6),
                "median_wall_s": round(statistics.median(times), 6),
                "mean_server_s": round(statistics.fmean([r["server_time_s"] for r in recs]), 6),
                "mean_attempt_rate": round(statistics.fmean(rates), 1),
                "chain_blocks": recs[-1]["block_index"],
                "startup_seconds": round(startup, 4),
                "reps": len(recs),
            })
            print(f"    mean wall {summary[-1]['mean_wall_s']:.4f}s, "
                  f"mean attempts {summary[-1]['observed_mean_attempts']:.0f}, "
                  f"rate {summary[-1]['mean_attempt_rate']:.0f}/s")
        finally:
            node.stop()
    write_csv(RESULTS / "difficulty_trials.csv", rows)
    write_csv(RESULTS / "difficulty_summary.csv", summary)
    dump_json(RESULTS / "difficulty_summary.json", summary)
    return summary


# ---------------------------------------------------------------------- experiment 2

def exp_payload_http(sizes=(1, 10, 100, 1000), difficulty="0000", reps=5, port=5111):
    """End-to-end: mine a block containing N pending transactions."""
    print("\n=== experiment: payload (end-to-end) ===")
    rows = []
    node = Node("payload", port, difficulty=difficulty)
    node.start()
    try:
        for n in sizes:
            for rep in range(reps):
                for i in range(n):
                    node.post("/transactions/new",
                              {"sender": f"s{i}", "recipient": f"r{i}", "amount": i + 1})
                rec = node.mine()
                rec["extra_transactions"] = n
                rec["transactions_in_block"] = n + 1  # + the coinbase
                rec["rep"] = rep
                rows.append(rec)
            sub = [r for r in rows if r["extra_transactions"] == n]
            print(f"  {n:5d} extra tx: mean wall "
                  f"{statistics.fmean(r['wall_time_s'] for r in sub):.4f}s, "
                  f"rate {statistics.fmean(r['attempt_rate'] for r in sub):.0f}/s")
    finally:
        node.stop()
    write_csv(RESULTS / "payload_http.csv", rows)
    return rows


# ---------------------------------------------------------------------- experiment 3

def _mine_nonces(payload, target, wanted, limit=3_000_000):
    """Nonces whose payload hashes to a digest meeting `target`.

    Mining is the point of the test, not an implementation detail: at '0000' roughly one
    nonce in 65536 qualifies, so a run of arbitrary nonces contains almost none on which
    two candidate formats disagree — and agreement on nonces both sides REJECT is not
    evidence about either one. A nonce only decides something when one side says yes.
    """
    out = []
    width = len(target)
    for nonce in range(limit):
        if hashlib.sha256(payload(nonce)).hexdigest()[:width] == target:
            out.append(nonce)
            if len(out) == wanted:
                return out
    raise AssertionError(
        f"could not mine {wanted} nonce(s) meeting {target!r} within {limit} attempts")


def assert_format_matches_provided(mod, wanted=3, prev_hash=None, index=7):
    """Prove our model of the hashed payload IS the provided program's, at nonces that bite.

    The microbenchmark splits an attempt into "serialise" and "the rest". That split is
    only meaningful if `content` here is byte-identical to the `content` built inside
    `Blockchain.valid_proof`, so this tests it rather than asserting it.

    The test is NOT a verdict comparison over a range of arbitrary nonces. At target
    '0000' both candidate formats reject essentially every nonce, so their verdicts agree
    by both saying no — and that agreement holds even when the bytes are wrong. An earlier
    version of this function compared verdicts over 200 nonces, reported agreement at
    200/200, and measured nothing; its own control refused to certify it, which is the
    only reason it was caught.

    Instead, mine nonces that only ONE of the two formats calls valid, in both directions:

      * nonces valid under OUR bytes must be ACCEPTED by valid_proof. A wrong byte model
        would have to be lucky on each of `wanted` such nonces (~1/65536 each).
      * nonces valid only under a deliberately wrong control format must be REJECTED,
        which fails loudly if valid_proof ignores its arguments or hashes something else.
      * a nonce our bytes reject must be rejected too, so a method that returns constant
        True cannot pass.

    The control is the documented trap: `str(txs)` — the Python repr the submission
    interpolates — versus `json.dumps(txs)`. The same list, double quotes instead of
    single, so a wrong guess at the payload format is detected rather than silently
    reused. The two differ only once a payload contains a quote, which is why the check
    uses transactions and asserts the control payload is not byte-identical to ours.
    """
    prev_hash = prev_hash or "a" * 64
    txs = [{"amount": i + 1, "recipient": f"r{i}", "sender": f"s{i}"} for i in range(3)]
    bc = mod.Blockchain.__new__(mod.Blockchain)
    target = bc.difficulty_target
    if not target:
        raise AssertionError(
            "provided difficulty_target is empty — every nonce would meet it and no test "
            "of the payload format could fail")

    def ours(nonce: int) -> bytes:
        return _content(index, prev_hash, txs, nonce)

    def control(nonce: int) -> bytes:
        return f"{index}{prev_hash}{json.dumps(txs)}{nonce}".encode()

    if control(0) == ours(0):
        raise AssertionError(
            "the control payload is byte-identical to the modelled one, so the test would "
            "be comparing a format with itself")

    accepts_ours = _mine_nonces(ours, target, wanted)
    accepts_control = _mine_nonces(control, target, wanted)

    for nonce in accepts_ours:
        if not bc.valid_proof(index, prev_hash, txs, nonce):
            raise AssertionError(
                f"provided valid_proof REJECTS nonce {nonce}, which our byte model meets "
                f"{target!r} — the harness bytes are not the bytes the program hashes, so "
                f"the serialise column would measure the harness, not the submission")

    for nonce in accepts_control:
        if bc.valid_proof(index, prev_hash, txs, nonce):
            raise AssertionError(
                f"provided valid_proof ACCEPTS nonce {nonce}, which only the deliberately "
                f"wrong control format (txs as JSON) meets {target!r} — valid_proof is not "
                f"hashing the payload we model")

    width = len(target)
    rejected = next(n for n in range(accepts_ours[-1] + 1, accepts_ours[-1] + 5000)
                    if hashlib.sha256(ours(n)).hexdigest()[:width] != target)
    if bc.valid_proof(index, prev_hash, txs, rejected):
        raise AssertionError(
            f"provided valid_proof ACCEPTS nonce {rejected}, which meets {target!r} under "
            f"no format tested — it is not testing the hash it appears to test")

    return len(accepts_ours), len(accepts_control)


def exp_micro(sizes=(0, 1, 10, 100, 500, 1000, 2500, 5000, 10000), calls=20000,
              difficulty=PROVIDED_DIFFICULTY, script=PROVIDED):
    """In-process microbenchmark: cost of ONE hash attempt vs transaction-list length.

    This is the mechanism behind the end-to-end payload result. It is a microbenchmark
    and is labelled as such: it measures `Blockchain.valid_proof` directly, not the
    served system. It runs against the program AS SUPPLIED, and `assert_format_matches_
    provided` proves the harness's byte model matches that program before any timing is
    reported.
    """
    print("\n=== experiment: microbenchmark (cost per attempt) ===")
    os.environ["DIFFICULTY"] = difficulty
    spec = importlib.util.spec_from_file_location("node_under_test", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    n_ours, n_control = assert_format_matches_provided(mod)
    print(f"  format check: {n_ours} nonces mined to meet '{PROVIDED_DIFFICULTY}' under the "
          f"harness's bytes -> provided valid_proof accepted all {n_ours}; {n_control} nonces "
          f"that ONLY the wrong (JSON) format meets -> provided rejected all {n_control}")

    rows = []
    prev_hash = "a" * 64
    for n in sizes:
        txs = [{"amount": i + 1, "recipient": f"r{i}", "sender": f"s{i}"}
               for i in range(n)]
        bc = mod.Blockchain.__new__(mod.Blockchain)
        # time the payload serialisation on its own — the REAL hashed bytes, same
        # expression the provided valid_proof evaluates
        t0 = time.perf_counter()
        for i in range(calls):
            _content(7, prev_hash, txs, i)
        ser = (time.perf_counter() - t0) / calls
        # time the full attempt (serialise + sha256 + compare)
        t0 = time.perf_counter()
        for i in range(calls):
            bc.valid_proof(7, prev_hash, txs, i)
        full = (time.perf_counter() - t0) / calls
        rows.append({
            "transactions": n,
            "payload_bytes": len(_content(7, prev_hash, txs, 0)),
            "us_per_serialise": round(ser * 1e6, 3),
            "us_per_attempt": round(full * 1e6, 3),
            "attempts_per_second": round(1.0 / full, 1),
            "serialise_fraction": round(ser / full, 4),
        })
        print(f"  {n:6d} tx ({rows[-1]['payload_bytes']:>9d} B): "
              f"{rows[-1]['us_per_attempt']:8.3f} us/attempt  "
              f"({rows[-1]['attempts_per_second']:>10.0f}/s)  "
              f"serialise {rows[-1]['serialise_fraction']*100:5.1f}%")
    write_csv(RESULTS / "micro_payload.csv", rows)
    return rows


# ---------------------------------------------------------------------- experiment 4

def exp_sync(block_counts=(5, 10, 25, 50, 100, 200, 400), tx_per_block=0,
             difficulty=PROVIDED_DIFFICULTY, port=5121):
    """Node B, having never synced, pulls a chain of K blocks from node A.

    The independent variable recorded is chain size in BYTES, because that is what
    actually has to move across the network and be re-parsed.
    """
    label = "empty" if tx_per_block == 0 else f"{tx_per_block}tx"
    print(f"\n=== experiment: sync ({label} blocks) ===")
    rows = []
    for k in block_counts:
        nodes = start_network([("A", port), ("B", port + 1)], difficulty=difficulty)
        a, b = nodes
        try:
            for _ in range(k):
                for i in range(tx_per_block):
                    a.post("/transactions/new",
                           {"sender": f"s{i}", "recipient": f"r{i}", "amount": i + 1})
                a.mine()
            chain_a = a.chain()
            size = chain_bytes(chain_a)
            wall, status, data = b.get("/nodes/sync")
            if status != 200:
                raise RuntimeError(f"sync -> {status}")
            after = b.chain()
            rows.append({
                "label": label,
                "blocks": k,
                "tx_per_block": tx_per_block,
                "chain_bytes": size,
                "sync_wall_s": round(wall, 6),
                "sync_server_s": data["sync_time_seconds"],
                "updated": data["message"].startswith("The blockchain has been updated"),
                "b_length_after": after["length"],
                "agree": json.dumps(after["chain"], sort_keys=True)
                         == json.dumps(chain_a["chain"], sort_keys=True),
            })
            print(f"  {k:4d} blocks ({size/1e6:7.3f} MB): "
                  f"wall {wall*1000:8.2f} ms, server {data['sync_time_seconds']*1000:8.2f} ms, "
                  f"agree={rows[-1]['agree']}")
        finally:
            stop_all(nodes)
    path = RESULTS / f"sync_{label}.csv"
    write_csv(path, rows)
    return rows


# ---------------------------------------------------------------------- experiment 5

def exp_fork(difficulty=PROVIDED_DIFFICULTY, port=5131):
    """Divergence, the strict-inequality rule, and destroyed work.

    Scripted sequence over three fully-meshed nodes. After every step we record each
    node's chain length and a short fingerprint of its chain, so the exact moment of
    agreement/disagreement is visible rather than asserted.
    """
    print("\n=== experiment: fork behaviour ===")
    events = []
    a, b, c = start_network([("A", port), ("B", port + 1), ("C", port + 2)],
                            difficulty=difficulty)

    def fingerprint(chain_data):
        import hashlib as _h
        raw = json.dumps(chain_data["chain"], sort_keys=True).encode()
        return _h.sha256(raw).hexdigest()[:12]

    def snapshot(step, note):
        state = {}
        for n in (a, b, c):
            ch = n.chain()
            state[n.name] = {"length": ch["length"], "fp": fingerprint(ch)}
        agree = len({v["fp"] for v in state.values()}) == 1
        events.append({"step": step, "note": note, "agree": agree, **state})
        print(f"  {step:<42} A={state['A']['length']} B={state['B']['length']} "
              f"C={state['C']['length']}  agree={agree}")
        return state

    try:
        snapshot("start (3 nodes, genesis only)", "")

        a.mine(); a.mine()
        snapshot("A mines 2 blocks, B and C idle", "A ahead")

        b.mine()
        snapshot("B mines 1 block (B has its own fork)", "B=2, A=3")

        b.get("/nodes/sync")
        snapshot("B syncs", "B should adopt A's longer chain")

        c.get("/nodes/sync")
        snapshot("C syncs", "all three agree")

        # --- the tie: B and C mine one block each from the same common tip ---
        b.mine(); c.mine()
        snapshot("B and C each mine 1 block independently", "equal length -> tie")
        b.get("/nodes/sync")
        snapshot("B syncs (B=4 C=4: not strictly longer)", "expect NO adoption")
        c.get("/nodes/sync")
        snapshot("C syncs (C=4 B=4: not strictly longer)", "expect NO adoption")
        b.get("/nodes/sync")
        snapshot("B syncs again", "expect still no adoption")
        c.get("/nodes/sync")
        snapshot("C syncs again", "expect still no adoption")

        # --- break the tie by making one branch strictly longer ---
        c.mine()
        snapshot("C mines one more block (C=5, B=4)", "now strictly longer")
        b.get("/nodes/sync")
        snapshot("B syncs", "B must now adopt C's chain and lose its own block")

        # --- and the same one-block-ahead race in the other direction ---
        a.mine()
        b.get("/nodes/sync")
        snapshot("A mines, B syncs", "B chases the longer chain")
    finally:
        stop_all((a, b, c))

    dump_json(RESULTS / "fork_events.json", events)
    with open(RESULTS / "fork_events.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["step", "note", "agree", "A_length", "A_fp",
                    "B_length", "B_fp", "C_length", "C_fp"])
        for e in events:
            w.writerow([e["step"], e["note"], e["agree"],
                        e["A"]["length"], e["A"]["fp"],
                        e["B"]["length"], e["B"]["fp"],
                        e["C"]["length"], e["C"]["fp"]])
    print(f"  wrote results/fork_events.csv ({len(events)} steps)")
    return events


# ---------------------------------------------------------------------- experiment 6

def exp_race(difficulty_slow="000000", difficulty_fast="00", port=5141,
             script=None, label="race", sync_timeout=300):
    """Does a sync arriving DURING proof-of-work corrupt the chain?

    Run only against the modified node (script=NODE, which reads DIFFICULTY and locks the
    chain). The arm needs the node under test to spend long enough in proof-of-work that a
    concurrent sync lands inside that window, and it buys that window with difficulty --
    which the provided program ignores. Its target is fixed at 0000 (~65k attempts, tens of
    milliseconds), so for the submission there is no slow/fast contrast to exploit: every
    sync would arrive after mining had already finished, and the arm would report "no
    corruption" without ever having created the condition it claims to test. That is a
    false negative manufactured by the instrument, so the provided variant is not run and
    the submission's fixed target is reported as a finding in its place.

    The verdict is not decided by this harness's own opinion: a third node is asked to accept the long chain, and the
    node's own valid_chain() is the oracle. If the long chain is *invalid*, the third
    node refuses it and reports "Our blockchain is the latest" despite the other chain
    being longer.
    """
    print(f"\n=== experiment: concurrent sync during mining ({label}) ===")
    script = script or PROVIDED
    P, S, T = port, port + 1, port + 2
    p = Node(f"{label}_P_slow", P, script=script, difficulty=difficulty_slow)
    s = Node(f"{label}_S_fast", S, script=script, difficulty=difficulty_fast)
    t = Node(f"{label}_T_witness", T, script=script, difficulty=difficulty_fast)
    for n in (p, s, t):
        n.start(timeout=900)
    for n in (p, s, t):
        n.register([o for o in (p, s, t) if o is not n])

    result = {"label": label, "script": str(Path(script).relative_to(BASE))}
    try:
        for _ in range(4):
            s.mine()
        t.get("/nodes/sync")            # T holds S's chain too
        p_before = p.chain()
        result["fast_chain"] = s.chain()["length"]
        result["slow_chain_before"] = p_before["length"]

        # start the slow mine in a background thread, then sync P mid-PoW
        mined = {}

        def slow_mine():
            try:
                mined["rec"] = p.mine(timeout=900)
            except Exception as exc:      # noqa: BLE001 - recorded, not swallowed
                mined["error"] = repr(exc)

        th = threading.Thread(target=slow_mine, daemon=True)
        th.start()
        time.sleep(3.0)                 # P is now deep inside proof_of_work
        sync_wall, sync_status, sync_data = p.raw_get("/nodes/sync", timeout=sync_timeout)
        result["mid_mine_sync_seconds"] = round(sync_wall, 3)
        result["mid_mine_sync_message"] = sync_data["message"] if sync_data else None
        th.join(timeout=900)
        result.update({k: v for k, v in mined.items()})

        result["slow_chain_after"] = p.chain()["length"]
        result["fast_chain_after_mid_sync"] = s.chain()["length"]

        # THE ORACLE: ask the fast node to adopt P's chain.
        _, _, verdict = s.get("/nodes/sync")
        result["witness_verdict"] = verdict["message"]
        result["witness_accepted_long_chain"] = verdict["message"].startswith(
            "The blockchain has been updated")
        result["witness_length_after"] = s.chain()["length"]

        # P's own view
        p_chain = p.chain()
        result["p_chain_length"] = p_chain["length"]
        result["p_last_block_index"] = p_chain["chain"][-1]["index"]

        # Independent corroboration only (the node's own valid_chain is the oracle)
        result["independent_link_check"] = _independent_link_check(p_chain["chain"])

        print(f"  slow chain before/after: {result['slow_chain_before']} -> "
              f"{result['slow_chain_after']}")
        print(f"  sync during PoW took {result['mid_mine_sync_seconds']}s: "
              f"'{result['mid_mine_sync_message']}'")
        print(f"  witness (fast node) verdict on the longer chain: "
              f"'{result['witness_verdict']}'")
        print(f"  accepted longer chain: {result['witness_accepted_long_chain']}")
        print(f"  independent link check on P's chain: "
              f"{result['independent_link_check']}")
    finally:
        stop_all((p, s, t))

    dump_json(RESULTS / f"{label}_result.json", result)
    return result


def _independent_link_check(chain):
    """Corroboration only — the node's own valid_chain() is the oracle.

    This deliberately checks ONE property: that every block's hash_of_previous_block
    really is the hash of the block before it. That is the property the race corrupts.
    It deliberately does NOT re-check proof-of-work, because the chain produced by the
    race mixes blocks mined at two different difficulty targets, so a uniform nonce check
    would report "invalid" for a reason that has nothing to do with the defect under test
    — a check that fails for the wrong reason is worse than no check.
    """
    import hashlib as _h
    for i in range(1, len(chain)):
        prev, cur = chain[i - 1], chain[i]
        expected = _h.sha256(json.dumps(prev, sort_keys=True).encode()).hexdigest()
        if cur["hash_of_previous_block"] != expected:
            return {"links_ok": False, "broken_at_index": i,
                    "expected_prev_hash": expected[:16],
                    "recorded_prev_hash": cur["hash_of_previous_block"][:16],
                    "blocks_in_chain": len(chain)}
    return {"links_ok": True, "blocks_in_chain": len(chain)}


# ---------------------------------------------------------------------- experiment 7

def exp_convergence(difficulty=PROVIDED_DIFFICULTY, port=5151):
    """How much mined work is destroyed when a SHORTER branch is abandoned?

    A mines 3 blocks, B mines 2, then B syncs. Worth stating what this experiment cannot
    show: the equal-length case never arises, because update_blockchain() adopts only a
    STRICTLY longer chain, so equal-length forks simply never resolve. That is exp_fork's
    result and the more interesting of the two; this one prices the work lost when the
    resolution goes the other way.

    Per node: attempts spent, and how many of that node's own mined blocks are still in its
    chain at the end, counted by fingerprint. For B that count is the destroyed work.
    """
    print("\n=== experiment: wasted work under divergence ===")
    rows = []
    a, b = start_network([("A", port), ("B", port + 1)], difficulty=difficulty)
    try:
        # A mines three blocks, B mines two, neither syncs.
        a_rate = []
        for _ in range(3):
            a_rate.append(a.mine()["attempts"])
        b_rate = []
        for _ in range(2):
            b_rate.append(b.mine()["attempts"])
        rows.append({"node": "A", "blocks_mined": 3, "attempts": sum(a_rate)})
        rows.append({"node": "B", "blocks_mined": 2, "attempts": sum(b_rate)})
        a_before = a.chain()
        b_before = b.chain()
        b.get("/nodes/sync")
        after = b.chain()

        # Count survivors -- but first prove the counter can see one.
        #
        # Two traps here, both hit in the first full run:
        #  * each node builds its own genesis in __init__ carrying `timestamp: time()`, so
        #    no two nodes' genesis blocks are byte-identical. "Subtract the genesis block
        #    they share" subtracts a block that was never shared, and the count came out
        #    NEGATIVE (-1). The genesis is now reported as its own measured fact.
        #  * a set intersection cannot distinguish "B's blocks were destroyed" from "the
        #    fingerprint comparison is broken" -- both produce an empty result. A's blocks
        #    are the positive control: A is the longer branch, so A's blocks MUST be in
        #    what B adopts. If they are not, this run has measured nothing and says so,
        #    rather than publishing a 0 that reads like a finding.
        before_fp, after_fp = _fingerprints(b_before["chain"]), _fingerprints(after["chain"])
        a_fp = _fingerprints(a_before["chain"])
        b_survived = sum(1 for f in before_fp[1:] if f in after_fp)
        a_survived = sum(1 for f in a_fp[1:] if f in _fingerprints(a.chain()["chain"]))
        a_control = sum(1 for f in a_fp[1:] if f in after_fp)
        if a_control != 3:
            raise AssertionError(
                f"positive control failed: A's 3 mined blocks should be present in the chain "
                f"B adopted (A is the longer branch) but {a_control} are, so the fingerprint "
                f"comparison is not measuring survival and B's count means nothing")
        rows[0]["own_blocks_in_final_chain"] = a_survived
        rows[1]["own_blocks_in_final_chain"] = b_survived
        rows[1]["blocks_destroyed"] = 2 - b_survived
        rows[1]["final_length"] = after["length"]
        rows[1]["genesis_survived_sync"] = before_fp[0] == after_fp[0]
        rows[1]["a_blocks_present_in_adopted"] = a_control
        print(f"  A mined 3 (attempts {sum(a_rate)}), B mined 2 (attempts {sum(b_rate)})")
        print(f"  after B syncs: B's chain length {b_before['length']} -> {after['length']}, "
              f"B's own mined blocks surviving = {b_survived}/2")
        print(f"  positive control: A retained {a_survived}/3 of its own blocks, and "
              f"{a_control}/3 are present in the chain B adopted")
        print(f"  B's genesis survived the adoption: {before_fp[0] == after_fp[0]} -- each "
              f"node stamps its own at __init__, so none was ever 'shared'")
        total_wasted = sum(b_rate)
        rows.append({"node": "TOTAL", "blocks_mined": 5,
                     "attempts": sum(a_rate) + sum(b_rate),
                     "wasted_attempts": total_wasted})
        print(f"  wasted attempts (B's abandoned branch): {total_wasted}")
        dump_json(RESULTS / "wasted_work.json",
                  {"a_attempts": sum(a_rate), "b_attempts": sum(b_rate),
                   "a_blocks_surviving": a_survived,
                   "b_blocks_surviving": b_survived,
                   "b_blocks_destroyed": 2 - b_survived,
                   "a_control_blocks_in_adopted_chain": a_control,
                   "genesis_survived_b_sync": before_fp[0] == after_fp[0],
                   "wasted_attempts": total_wasted})
    finally:
        stop_all((a, b))
    write_csv(RESULTS / "wasted_work.csv", rows)
    return rows


# ---------------------------------------------------------------------- experiment 8

def exp_autosync(difficulty="00", port=5161, interval=1.0):
    """The modification: does automatic sync converge a two-node network?

    Both nodes mine the same number of blocks while AUTO_SYNC is on. Without the
    modification each would keep its own chain indefinitely; the question is whether
    the background thread produces agreement, and how quickly.
    """
    print("\n=== experiment: automatic sync modification ===")
    a, b = start_network([("A", port), ("B", port + 1)], difficulty=difficulty,
                         script=NODE,          # the modification is the subject here
                         auto_sync=True, auto_sync_interval=interval)
    rows = []
    try:
        for i in range(6):
            slot = i % 2
            (a if slot == 0 else b).mine()
            time.sleep(interval * 1.5)
            ca, cb = a.chain(), b.chain()
            same = json.dumps(ca["chain"], sort_keys=True) == \
                json.dumps(cb["chain"], sort_keys=True)
            rows.append({"round": i, "miner": "A" if slot == 0 else "B",
                         "A_length": ca["length"], "B_length": cb["length"],
                         "agreed": same})
            print(f"  round {i}: A={ca['length']} B={cb['length']} agree={same}")
        # settle
        for _ in range(3):
            time.sleep(interval * 2)
        ca, cb = a.chain(), b.chain()
        same = json.dumps(ca["chain"], sort_keys=True) == \
            json.dumps(cb["chain"], sort_keys=True)
        rows.append({"round": "settled", "miner": None,
                     "A_length": ca["length"], "B_length": cb["length"],
                     "agreed": same})
        print(f"  settled: A={ca['length']} B={cb['length']} agree={same}")
    finally:
        stop_all((a, b))
    write_csv(RESULTS / "autosync.csv", rows)
    return rows


# ---------------------------------------------------------------------------------

EXPERIMENTS = {
    "difficulty": exp_difficulty,
    "micro": exp_micro,
    "payload": exp_payload_http,
    "sync": exp_sync,
    "fork": exp_fork,
    # No "race" entry: the arm needs the node under test to sit in proof-of-work long
    # enough for a sync to land inside the window, and the window is set by difficulty,
    # which the provided program ignores. Its variant cannot create the condition it
    # tests, so it is not offered. See exp_race's docstring.
    "race_fixed": lambda: exp_race(script=NODE, label="race_fixed"),
    "wasted": exp_convergence,
    "autosync": exp_autosync,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("experiment", choices=sorted(EXPERIMENTS) + ["all", "quick"])
    args = ap.parse_args()

    _install_shutdown_hooks()
    target = assert_provided_shape()
    print(f"provided program: fixed target {target!r}, verified from source")

    t0 = time.perf_counter()
    if args.experiment == "all":
        order = ["difficulty", "micro", "payload", "sync", "sync_fat", "fork",
                 "wasted", "autosync", "race_fixed"]
    elif args.experiment == "quick":
        order = ["micro", "fork", "wasted"]
    else:
        order = [args.experiment]

    for name in order:
        if name == "sync_fat":
            exp_sync(block_counts=(5, 10, 25, 50, 100, 200), tx_per_block=100)
        else:
            EXPERIMENTS[name]()

    print(f"\nall done in {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    main()
