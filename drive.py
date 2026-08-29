"""
Drive the in-game bridge from outside, and verify what it claims.

This is the piece that turns the spike into a real answer: point it at a running
game with the GameBridge mod loaded and it will exercise the whole chain, then
run the negative-control pass over a real write.

    python drive.py --data "<game>/Binaries/Win64/ue4ss/Mods/GameBridge/data" ping
    python drive.py --data ... find BP_PlayerCharacter_C
    python drive.py --data ... probe          # full chain + negative control

The `probe` mode is the one that matters. It:
  1. finds a live object
  2. reads a numeric property
  3. writes a new value, taking the bridge's own claim
  4. RE-READS through a separate request and compares       <- postcondition
  5. poisons the check and confirms it goes red             <- negative control
  6. restores the original value

Step 5 is the part nothing else does, and it is what separates "my check passed"
from "my check is capable of failing."
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


class FileBridge:
    """Talks to the in-game Lua bridge over the two append-only files."""

    def __init__(self, data_dir: Path, timeout: float = 8.0):
        self.dir = Path(data_dir)
        self.cmd = self.dir / "cmd.jsonl"
        self.resp = self.dir / "resp.jsonl"
        self.timeout = timeout
        self._id = int(time.time()) % 100000
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cmd.touch(exist_ok=True)
        self.resp.touch(exist_ok=True)
        # Start reading responses from the end, so a previous session's
        # transcript is never mistaken for an answer to this request.
        self._seen = sum(1 for _ in self.resp.open(encoding="utf-8", errors="replace"))

    def send(self, **cmd) -> dict:
        self._id += 1
        cmd["id"] = self._id
        with self.cmd.open("a", encoding="ascii") as f:
            f.write(json.dumps(cmd) + "\n")
        return self._await(self._id)

    def _await(self, want_id: int) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            lines = self.resp.open(encoding="utf-8", errors="replace").readlines()
            for line in lines[self._seen:]:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("id") == want_id:
                    self._seen = len(lines)
                    return obj
            time.sleep(0.15)
        raise TimeoutError(
            f"no response to id={want_id} within {self.timeout}s. "
            "Is the game running with GameBridge loaded, and is this the right data dir?"
        )

    # verifier-side read: a separate request, never the write's own return value
    def read(self, obj: str, prop: str):
        r = self.send(op="read", obj=obj, prop=prop)
        if not r.get("ok"):
            raise RuntimeError(r.get("err", "read failed"))
        return r["value"]


def probe(b: FileBridge, cls: str, prop: str) -> int:
    print(f"\n[1] ping")
    r = b.send(op="ping")
    print(f"    world={r.get('world')}  travelling={r.get('travelling')}")
    if not r.get("ok"):
        print("    bridge did not answer ok"); return 1

    print(f"\n[2] find {cls}")
    r = b.send(op="find", **{"class": cls}, limit=5)
    objs = r.get("objects") or []
    print(f"    found {r.get('count')}: {objs[:3]}")
    if not objs:
        print("    nothing found -- pick a class that exists in the current level")
        return 1
    obj = objs[0]

    print(f"\n[3] read {prop}")
    original = b.read(obj, prop)
    print(f"    {prop} = {original!r}")
    if not isinstance(original, (int, float)):
        print("    need a numeric property for the write test")
        return 1

    target = float(original) + 7.0
    print(f"\n[4] write {prop} <- {target}")
    w = b.send(op="write", obj=obj, prop=prop, value=target)
    claimed = bool(w.get("wrote"))
    w_before, w_after = w.get("before"), w.get("after")
    print(f"    bridge claimed: {claimed}   (before={w_before} after={w_after})")

    # The write response carries its own observation, taken at the write site.
    # Treat it as a second, independent channel rather than as decoration: when
    # it disagrees with the re-read below, one of the two is fabricated, and
    # that disagreement is worth more than either reading alone.
    landed_at_write = (
        isinstance(w_after, (int, float)) and abs(float(w_after) - target) < 1e-6
    )

    print(f"\n[5] postcondition: independent re-read")
    observed = b.read(obj, prop)
    holds = abs(float(observed) - target) < 1e-6
    print(f"    re-read {prop} = {observed!r}  -> {'target' if holds else 'NOT target'}")

    print(f"\n[6] cross-channel agreement")
    print(f"    write-site says landed: {landed_at_write}    re-read says landed: {holds}")
    contradiction = landed_at_write != holds
    if contradiction:
        print("    CONTRADICTION -- the two channels disagree; at least one is lying")

    print(f"\n[7] negative control: prove the check can go red")
    # The poison must be shown to have LANDED before its result means anything.
    # A poison that silently fails to apply makes a dead check look alive, which
    # is how this harness fooled itself on the first run.
    poison_val = float(target) + 1234.0
    pw = b.send(op="write", obj=obj, prop=prop, value=poison_val)
    poison_landed = (
        isinstance(pw.get("after"), (int, float))
        and abs(float(pw["after"]) - poison_val) < 1e-6
    )
    poisoned = b.read(obj, prop)
    noticed = abs(float(poisoned) - target) >= 1e-6
    print(f"    poison landed at write site: {poison_landed}")
    print(f"    re-read after poison: {poisoned!r}; check "
          f"{'went RED' if noticed else 'still passed'}")
    if not poison_landed:
        print("    poison never applied -- the negative control proves nothing here")

    print(f"\n[8] restore")
    b.send(op="write", obj=obj, prop=prop, value=original)
    print(f"    {prop} = {b.read(obj, prop)!r}")

    print("\n" + "=" * 58)
    # Order matters. The negative control is what licenses trusting a PASS; it is
    # not needed to trust a FAIL. Two independent channels agreeing that nothing
    # changed is already conclusive, so that verdict is reported on its own.
    if contradiction:
        print("INCONSISTENT BRIDGE: the write site and the re-read disagree about")
        print("whether the value changed. No verdict about success is available")
        print("from this bridge -- a channel cannot verify itself.")
        return 1
    if claimed and not holds:
        print("FALSE SUCCESS caught: the bridge reported a write it did not make.")
        print("Both channels agree the value never changed.")
        return 1
    if not poison_landed:
        print("VERDICT WITHHELD: the poison did not apply, so the check was never")
        print("shown to be capable of failing. The apparent pass is unproven.")
        return 1
    if not noticed:
        print("DEAD CHECK: the check survived a poison that provably landed.")
        return 1
    print("Chain verified end to end, and the check is provably alive.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="GameBridge data directory")
    ap.add_argument("cmd", choices=["ping", "find", "read", "probe"])
    ap.add_argument("arg", nargs="?", default="BP_PlayerCharacter_C")
    ap.add_argument("--prop", default="Health")
    a = ap.parse_args()

    b = FileBridge(Path(a.data))
    if a.cmd == "ping":
        print(json.dumps(b.send(op="ping"), indent=1))
    elif a.cmd == "find":
        print(json.dumps(b.send(op="find", **{"class": a.arg}, limit=25), indent=1))
    elif a.cmd == "read":
        print(b.read(a.arg, a.prop))
    else:
        return probe(b, a.arg, a.prop)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError as e:
        print(f"\nTIMEOUT: {e}")
        sys.exit(2)
