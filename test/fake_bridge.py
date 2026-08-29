"""
A stand-in for the in-game Lua bridge, speaking the same two-file protocol.

Lets drive.py be exercised end to end without launching a game, and -- more
usefully -- lets the two failure modes be reproduced on demand:

    --lie        acknowledge writes without performing them
    --deadcheck  additionally fake reads so the value always looks correct

Run in one terminal:   python fake_bridge.py --data ./_t [--lie]
And in another:        python drive.py --data ./_t probe
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

WORLD = {
    "BP_PlayerCharacter_C /Game/Maps/L_Test.L_Test:PersistentLevel.BP_PlayerCharacter_C_0": {
        "Health": 100.0, "MaxHealth": 100.0,
    },
}


def run(data: Path, lie: bool, deadcheck: bool) -> None:
    data.mkdir(parents=True, exist_ok=True)
    cmd, resp = data / "cmd.jsonl", data / "resp.jsonl"
    cmd.touch(exist_ok=True)
    resp.touch(exist_ok=True)
    cursor = sum(1 for _ in cmd.open(encoding="utf-8", errors="replace"))

    def reply(o: dict) -> None:
        with resp.open("a", encoding="utf-8") as f:
            f.write(json.dumps(o) + "\n")

    reply({"id": 0, "ok": True, "event": "bridge_up",
           "mode": ("lying" if lie else "honest") + ("+deadcheck" if deadcheck else "")})
    print(f"[fake_bridge] up on {data}  lie={lie} deadcheck={deadcheck}")

    faked: dict[tuple[str, str], float] = {}

    while True:
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
                keys = [k for k in WORLD if k.startswith(c.get("class", ""))]
                out = {"ok": True, "count": len(keys), "objects": keys}
            elif op == "read":
                key = (c["obj"], c["prop"])
                if deadcheck and key in faked:
                    out = {"ok": True, "value": faked[key], "vtype": "number"}
                else:
                    try:
                        out = {"ok": True, "value": WORLD[c["obj"]][c["prop"]], "vtype": "number"}
                    except KeyError:
                        out = {"ok": False, "err": "object not found"}
            elif op == "write":
                try:
                    before = WORLD[c["obj"]][c["prop"]]
                except KeyError:
                    out = {"ok": False, "err": "object not found"}
                else:
                    if lie:
                        after = before                       # never actually written
                        if deadcheck:
                            faked[(c["obj"], c["prop"])] = c["value"]  # but reads back "right"
                    else:
                        WORLD[c["obj"]][c["prop"]] = c["value"]
                        after = c["value"]
                    out = {"ok": True, "wrote": True, "before": before,
                           "after": after, "requested": c["value"]}
            else:
                out = {"ok": False, "err": f"unknown op: {op}"}

            out["id"] = c.get("id")
            reply(out)
        time.sleep(0.05)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--lie", action="store_true")
    ap.add_argument("--deadcheck", action="store_true")
    a = ap.parse_args()
    run(Path(a.data), a.lie, a.deadcheck)
