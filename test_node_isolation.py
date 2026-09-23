#!/usr/bin/env python3
"""Falsification suite for the node-identity guard in harness.py.

WHY THIS FILE EXISTS.

The harness used to accept the first HTTP 200 it saw on its port as proof that its
own node was up. It is not proof. Any listener satisfies that condition -- including
a node left running by an earlier experiment -- and the harness would then measure a
node it never configured while writing the parameters it *intended* into the results
file.

That turns a broken experiment into a successful-looking one, and there is no way to
detect it downstream: the numbers are real, they are simply measurements of a
different system than the row claims.

So the guard in Node.start()/_assert_identity() is a MEASUREMENT INSTRUMENT, and an
instrument is worth exactly what its failure modes are worth. Every test below
asserts the guard FIRES. One control asserts it does NOT fire on a healthy node --
a guard that fires on everything is as useless as one that never fires, and it would
be the easier mistake to make and not notice.

The decoys are REAL nodes running the real node.py, differing only in the condition
we are about to make a claim about. A stub that returns 200 would be a weaker
witness, and would only prove the guard can detect a stub.

Run:  python3 test_node_isolation.py
"""

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent

_spec = importlib.util.spec_from_file_location("harness", BASE / "harness.py")
harness = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(harness)

NODE = harness.NODE
PYTHON = harness.PYTHON

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(f"  {'PASS' if condition else 'FAIL'}  {name}")
    if detail:
        for line in detail.rstrip().splitlines():
            print(f"          {line}")


def spawn_decoy(port, difficulty, timeout=120):
    """Start a real node.py on `port` and wait until it answers. Not ours."""
    env = dict(os.environ)
    env["DIFFICULTY"] = difficulty
    env["PYTHONUNBUFFERED"] = "1"
    env["AUTO_SYNC"] = "0"
    proc = subprocess.Popen(
        [PYTHON, str(NODE), str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        cwd=str(BASE), env=env,
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"decoy on {port} exited {proc.returncode}")
        try:
            if requests.get(f"http://127.0.0.1:{port}/blockchain", timeout=2).status_code == 200:
                return proc
        except requests.RequestException:
            pass
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError(f"decoy on {port} never became ready")


def alive_dummy():
    """A live process that is deliberately NOT the node under test.

    Setting Node.proc to this isolates the difficulty and freshness guards from the
    liveness guard, so a test that passes can be attributed to the guard it names
    rather than to whichever check happened to fire first.
    """
    return subprocess.Popen([PYTHON, "-c", "import time; time.sleep(120)"])


def main():
    procs = []
    saved_port_is_free = harness.port_is_free

    try:
        # ---------------------------------------------------------------- control
        # -------------------------------------------------- the pre-flight probe
        # The probe that FEEDS the pre-flight was itself broken: it bound the port with
        # SO_REUSEADDR set, which on macOS lets a 127.0.0.1 bind succeed alongside a real
        # listener on 0.0.0.0 -- exactly how node.py binds. It reported "free" for every
        # running node, so the pre-flight could never fire. Both directions are asserted
        # here, so a probe stuck on True and a probe stuck on False each fail one check.
        # The bug was the stuck-on-True kind, and that kind reads as a healthy system.
        print("\n[probe] port_is_free must tell an occupied port from a free one")
        decoy = spawn_decoy(5395, "0000")
        procs.append(decoy)
        occupied = harness.port_is_free(5395)
        free = harness.port_is_free(5394)
        check("probe: a port a live node is serving must read as occupied",
              occupied is False,
              f"port_is_free(5395) -> {occupied!r}, with a node answering on 5395")
        check("probe: an unused port must read as free (not busy-for-everything)",
              free is True,
              f"port_is_free(5394) -> {free!r}, with nothing listening on 5394")

        # ---------------------------------------------------------------- control
        # A guard that fires on healthy input is not a guard. This must NOT raise.
        print("\n[control] guard must not fire on a healthy, freshly started node")
        n = harness.Node("control", 5399, difficulty="00")
        procs.append(n)
        try:
            n.start(timeout=180)
            check("control: healthy node starts without raising", True,
                  f"startup {n.startup_seconds:.2f}s")
            check("control: observed difficulty recorded from the wire",
                  n.observed_difficulty == "00",
                  f"observed_difficulty={n.observed_difficulty!r} (requested '00')")
            check("control: numbers can only come from the node we spawned",
                  n.mine()["difficulty"] == 2,
                  "mine() row records len('00') == 2 from the confirmed value")
        except Exception as exc:  # noqa: BLE001 - the control failing is the finding
            check("control: healthy node starts without raising", False, repr(exc))
        finally:
            n.stop()

        # ------------------------------------------------------- pre-flight guard
        # The exact 2026-09-23 failure: a node from an earlier run still alive.
        print("\n[pre-flight] a stale listener on the port must be refused, not measured")
        decoy = spawn_decoy(5398, "0000")
        procs.append(decoy)
        n = harness.Node("victim-preflight", 5398, difficulty="000")
        try:
            n.start(timeout=30)
            check("pre-flight: start() raises when the port is occupied", False,
                  "start() returned success -- the decoy would have been measured")
        except RuntimeError as exc:
            check("pre-flight: start() raises when the port is occupied",
                  "already in use" in str(exc), str(exc))
        except Exception as exc:  # noqa: BLE001
            check("pre-flight: start() raises when the port is occupied", False,
                  f"wrong exception type: {exc!r}")

        # The falsification of the OLD logic, stated positively: the condition the
        # old code accepted as proof of readiness is TRUE for a total stranger.
        r = requests.get("http://127.0.0.1:5398/blockchain", timeout=5)
        check("old condition was satisfied by a stranger (HTTP 200)", r.status_code == 200,
              f"status={r.status_code}; the old `if r.status_code == 200: return` "
              f"would have reported this stranger as our node")

        # ------------------------------------------- identity: wrong difficulty
        print("\n[identity] right port, right liveness, WRONG CONDITION must be refused")
        harness.port_is_free = lambda port: True   # bypass pre-flight on purpose
        n = harness.Node("victim-difficulty", 5398, difficulty="00000")
        n.proc = alive_dummy()
        procs.append(n.proc)
        try:
            n._assert_identity()
            check("identity: wrong difficulty is refused", False,
                  "no raise -- asked for '00000', decoy reports '0000'")
        except RuntimeError as exc:
            check("identity: wrong difficulty is refused",
                  "reports" in str(exc), str(exc))

        # -------------------------------------------- identity: already-used chain
        print("\n[identity] right difficulty, but a chain already in use, must be refused")
        decoy2 = spawn_decoy(5397, "000")
        procs.append(decoy2)
        for _ in range(3):
            requests.get("http://127.0.0.1:5397/mine", timeout=30)
        live = requests.get("http://127.0.0.1:5397/blockchain", timeout=5).json()
        check("setup: decoy chain is no longer fresh", live["length"] == 4,
              f"decoy length={live['length']} difficulty={live['difficulty']!r}")
        n = harness.Node("victim-chain", 5397, difficulty="000")
        n.proc = alive_dummy()
        procs.append(n.proc)
        try:
            n._assert_identity()
            check("identity: a previously-used chain is refused", False,
                  "no raise -- decoy already holds 4 blocks")
        except RuntimeError as exc:
            check("identity: a previously-used chain is refused",
                  "length" in str(exc), str(exc))

        # -------------------------------------------------- identity: dead child
        print("\n[identity] a reply from a port our own child never bound must be refused")
        n = harness.Node("victim-liveness", 5397, difficulty="000")
        dead = subprocess.Popen([PYTHON, "-c", "pass"])
        dead.wait()
        n.proc = dead
        try:
            n._assert_identity()
            check("identity: child already exited is refused", False, "no raise")
        except RuntimeError as exc:
            check("identity: child already exited is refused",
                  "already exited" in str(exc), str(exc))

    finally:
        harness.port_is_free = saved_port_is_free
        for p in procs:
            try:
                if hasattr(p, "stop"):
                    p.stop()
                elif p.poll() is None:
                    p.kill()
                    p.wait(timeout=10)
            except Exception:  # noqa: BLE001 - cleanup must never mask a result
                pass

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("FAILED: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
