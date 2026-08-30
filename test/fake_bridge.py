"""
A stand-in for the in-game Lua bridge, speaking the same two-file protocol.

Lets drive.py be exercised end to end without launching a game, and -- more
usefully -- lets the two failure modes be reproduced on demand:

    --lie          acknowledge writes without performing them
    --deadcheck    additionally fake reads, but keep reporting the old value
                   at the write site -- the two channels then disagree
    --stale-reads  the dangerous one: the write site echoes whatever was asked
                   for, and reads come from a cache filled by the first write.
                   Both channels agree, every assertion passes, and only a
                   poison that the check fails to notice gives it away.

Run in one terminal:   python test/fake_bridge.py --data ./_t [--lie]
And in another:        python drive.py --data ./_t probe

`python drive.py demo` does both halves in one process; this file is what it
runs against.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path

# The starting world. Copied per run -- a module-level dict that survived
# between runs would let one scenario's writes leak into the next one's
# baseline, which is exactly the kind of shared state that makes a test suite
# pass in isolation and fail in sequence.
INITIAL_WORLD = {
    "BP_PlayerCharacter_C /Game/Maps/L_Test.L_Test:PersistentLevel.BP_PlayerCharacter_C_0": {
        "Health": 100.0, "MaxHealth": 100.0,
        # Large enough that a flat +1234 poison is absorbed by float64.
        # A healthy bridge used to be convicted of a DEAD_CHECK here.
        "HugeCounter": 1e20,
    },
}


def run(
    data: Path,
    lie: bool = False,
    deadcheck: bool = False,
    stop: threading.Event | None = None,
    quiet: bool = False,
    stale_reads: bool = False,
    wrote_false: bool = False,
    refuse_nth_write: int = 0,
    vanish_after_write: int = 0,
    ready: threading.Event | None = None,
) -> None:
    """Serve the two-file protocol until `stop` is set (or forever, if None)."""
    world = {obj: dict(props) for obj, props in INITIAL_WORLD.items()}

    data.mkdir(parents=True, exist_ok=True)
    cmd, resp = data / "cmd.jsonl", data / "resp.jsonl"
    cmd.touch(exist_ok=True)
    resp.touch(exist_ok=True)
    cursor = sum(1 for _ in cmd.open(encoding="utf-8", errors="replace"))
    # Everything already in the file at this instant is a previous session's
    # traffic and gets skipped. A caller that sends before this line runs would
    # have its first command skipped too, so it is told when the cursor is set
    # rather than left to guess -- an in-process demo loses that race often
    # enough to accuse itself of failing its own negative control.
    if ready is not None:
        ready.set()

    def reply(o: dict) -> None:
        with resp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(o) + "\n")

    mode = ("lying" if lie else "honest") + ("+deadcheck" if deadcheck else "")
    if stale_reads:
        mode = "stale-reads"
    reply({"id": 0, "ok": True, "event": "bridge_up", "mode": mode})
    if not quiet:
        print(f"[fake_bridge] up on {data}  mode={mode}")

    faked: dict[tuple[str, str], float] = {}
    # Filled by the FIRST write to a property and never updated. A read served
    # from here is stale, not wrong -- which is why nothing downstream notices.
    pinned: dict[tuple[str, str], float] = {}
    # Writes are counted so a scenario can target one of them by position. A
    # probe writes three times: the target, the poison, then the restore.
    # Refusing the second reaches WITHHELD, refusing the third reaches
    # POISON_STUCK, and neither branch had any way to be exercised before.
    writes = 0

    while stop is None or not stop.is_set():
        lines = cmd.open(encoding="utf-8", errors="replace").readlines()
        for line in lines[cursor:]:
            cursor += 1
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
            except Exception:
                continue
            op, out = c.get("op"), {}

            if op == "ping":
                out = {"ok": True, "world": "L_Test", "travelling": False}
            elif op == "find":
                keys = [k for k in world if k.startswith(c.get("class", ""))]
                out = {"ok": True, "count": len(keys), "objects": keys}
            elif op == "read":
                if vanish_after_write and writes >= vanish_after_write:
                    out = {"ok": False, "err": "object not found"}
                    out["id"] = c.get("id")
                    reply(out)
                    continue
                key = (c["obj"], c["prop"])
                if stale_reads and key in pinned:
                    out = {"ok": True, "value": pinned[key], "vtype": "number"}
                elif deadcheck and key in faked:
                    out = {"ok": True, "value": faked[key], "vtype": "number"}
                else:
                    try:
                        out = {"ok": True, "value": world[c["obj"]][c["prop"]], "vtype": "number"}
                    except KeyError:
                        out = {"ok": False, "err": "object not found"}
            elif op == "write":
                writes += 1
                try:
                    before = world[c["obj"]][c["prop"]]
                except KeyError:
                    out = {"ok": False, "err": "object not found"}
                else:
                    if wrote_false:
                        # ok, and honest about not having written.
                        out = {"ok": True, "wrote": False, "before": before,
                               "after": before, "requested": c["value"]}
                        out["id"] = c.get("id")
                        reply(out)
                        continue
                    if refuse_nth_write and writes == refuse_nth_write:
                        # Acknowledged, silently not applied. The write site
                        # reports the old value, so a caller that reads it can
                        # tell -- which is the whole point of that channel.
                        out = {"ok": True, "wrote": True, "before": before,
                               "after": before, "requested": c["value"]}
                        out["id"] = c.get("id")
                        reply(out)
                        continue
                    if stale_reads:
                        # Echo the request, so the write site looks flawless.
                        pinned.setdefault((c["obj"], c["prop"]), c["value"])
                        after = c["value"]
                    elif lie:
                        after = before                       # never actually written
                        if deadcheck:
                            faked[(c["obj"], c["prop"])] = c["value"]  # but reads back "right"
                    else:
                        world[c["obj"]][c["prop"]] = c["value"]
                        after = c["value"]
                    out = {"ok": True, "wrote": True, "before": before,
                           "after": after, "requested": c["value"]}
            else:
                out = {"ok": False, "err": f"unknown op: {op}"}

            out["id"] = c.get("id")
            reply(out)
        time.sleep(0.02)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--lie", action="store_true")
    ap.add_argument("--deadcheck", action="store_true")
    ap.add_argument("--stale-reads", action="store_true")
    ap.add_argument("--wrote-false", action="store_true")
    ap.add_argument("--refuse-nth-write", type=int, default=0)
    ap.add_argument("--vanish-after-write", type=int, default=0)
    a = ap.parse_args()
    run(Path(a.data), a.lie, a.deadcheck, stale_reads=a.stale_reads,
        wrote_false=a.wrote_false, refuse_nth_write=a.refuse_nth_write,
        vanish_after_write=a.vanish_after_write)
