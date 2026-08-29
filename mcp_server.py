"""
MCP server for a running Unreal Engine game.

Exposes the live game to an agent, with one deliberate difference from every
other "drive the game" tool: the write tool cannot return a bare success.

`write_property` runs the full verification -- an independent re-read, a
cross-check against the write site's own observation, and a negative control
that proves the check is capable of failing -- and returns a verdict. When the
evidence does not support a claim of success, it says so, and it says why.

That is the whole design. An agent calling this tool cannot report a state
change it did not make, because the tool will not hand it the words.

Run:
    pip install "mcp>=2"
    python mcp_server.py --data "<game>/Binaries/Win64/ue4ss/Mods/GameBridge/data"

Register with a client (Claude Code shown; adapt for others):
    claude mcp add ue-live -- python /abs/path/mcp_server.py --data "/abs/path/to/data"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from drive import FileBridge

DATA_DIR = os.environ.get("UE_LIVE_BRIDGE_DATA", "")

server = MCPServer(
    name="ue-live-bridge",
    instructions=(
        "Drives a running Unreal Engine game through a UE4SS bridge.\n\n"
        "Blueprint class names take the _C suffix (BP_ChangeManager_C, not "
        "BP_ChangeManager). Call find_objects before reading or writing: it "
        "returns full object names, which are the only valid handles, and it "
        "warms the resolver so later calls do not scan the object array.\n\n"
        "write_property returns a verdict, not a boolean. Treat any verdict "
        "other than CONFIRMED as the write NOT having happened, including "
        "WITHHELD -- 'unproven' is not 'succeeded'. Do not retry a write on a "
        "verdict of INCONSISTENT_BRIDGE; the bridge is unreliable and repeating "
        "the call will not make it truthful."
    ),
)

_bridge: FileBridge | None = None


def bridge() -> FileBridge:
    global _bridge
    if _bridge is None:
        if not DATA_DIR:
            raise RuntimeError(
                "no data directory configured; pass --data or set UE_LIVE_BRIDGE_DATA"
            )
        _bridge = FileBridge(Path(DATA_DIR))
    return _bridge


@server.tool(description="Check that the game is running and the bridge is responding. Returns the current world and resolver cache stats.")
def ping() -> dict:
    return bridge().send(op="ping")


@server.tool(description="List live objects of a class. Blueprint classes need the _C suffix. Returns full object names, which are the handles used by every other tool.")
def find_objects(class_name: str, limit: int = 25) -> dict:
    return bridge().send(op="find", **{"class": class_name}, limit=limit)


@server.tool(description="Read one property off a live object. Takes the full object name from find_objects.")
def read_property(obj: str, prop: str) -> dict:
    return bridge().send(op="read", obj=obj, prop=prop)


@server.tool(description="Call a no-argument UFunction on a live object.")
def call_function(obj: str, fn: str) -> dict:
    return bridge().send(op="call", obj=obj, fn=fn)


@server.tool(
    description=(
        "Write a property and verify it. Returns a verdict, never a bare success. "
        "CONFIRMED means the change was independently observed and the check was "
        "proven capable of failing. Anything else means the write should be "
        "treated as not having happened."
    )
)
def write_property(obj: str, prop: str, value: float) -> dict:
    """Write, then earn the right to say it worked.

    Four things have to line up before this returns CONFIRMED:
      1. the write site reports the new value
      2. an independent re-read agrees
      3. a poison value provably lands
      4. the check notices the poison

    Any gap between them is reported as the specific gap it is, because
    "unverified" and "failed" are different facts and collapsing them is how
    a caller ends up confidently wrong.
    """
    b = bridge()

    before = b.send(op="read", obj=obj, prop=prop)
    if not before.get("ok"):
        return {"verdict": "UNREADABLE", "detail": before.get("err", "read failed")}
    original = before["value"]

    w = b.send(op="write", obj=obj, prop=prop, value=value)
    if not w.get("ok"):
        return {"verdict": "WRITE_REJECTED", "detail": w.get("err", "write failed"),
                "original": original}

    def near(a: Any, x: float) -> bool:
        return isinstance(a, (int, float)) and abs(float(a) - x) < 1e-6

    landed_at_write = near(w.get("after"), value)

    r = b.send(op="read", obj=obj, prop=prop)
    holds = r.get("ok") and near(r.get("value"), value)

    if landed_at_write != holds:
        return {
            "verdict": "INCONSISTENT_BRIDGE",
            "detail": "the write site and an independent re-read disagree about "
                      "whether the value changed; a channel cannot verify itself",
            "write_site_after": w.get("after"),
            "reread": r.get("value"),
            "original": original,
        }

    if not holds:
        return {
            "verdict": "FALSE_SUCCESS",
            "detail": "the bridge accepted the write but the value did not change; "
                      "both channels agree",
            "observed": r.get("value"),
            "original": original,
        }

    # The value looks right. That is not yet a reason to believe the check.
    poison = float(value) + 1234.0
    pw = b.send(op="write", obj=obj, prop=prop, value=poison)
    poison_landed = near(pw.get("after"), poison)
    pr = b.send(op="read", obj=obj, prop=prop)
    noticed = not (pr.get("ok") and near(pr.get("value"), value))

    b.send(op="write", obj=obj, prop=prop, value=original if _restore_original else value)

    if not poison_landed:
        return {
            "verdict": "WITHHELD",
            "detail": "the poison never applied, so the check was never shown to be "
                      "capable of failing; the apparent success is unproven",
            "original": original,
        }
    if not noticed:
        return {
            "verdict": "DEAD_CHECK",
            "detail": "the check survived a poison that provably landed; it cannot "
                      "distinguish success from failure and its verdict means nothing",
            "original": original,
        }

    return {
        "verdict": "CONFIRMED",
        "detail": "independently observed, and the check was proven able to fail",
        "value": value,
        "original": original,
    }


# The negative control leaves the world holding a poison value for a moment. The
# final write puts back whichever the caller asked for: the value they wanted
# (default) or the value that was there before.
_restore_original = False


def main() -> int:
    global DATA_DIR, _restore_original
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=DATA_DIR,
                    help="GameBridge data directory inside the game install")
    ap.add_argument("--restore-original", action="store_true",
                    help="after verifying, put the original value back instead of "
                         "leaving the requested one (useful for read-only auditing)")
    a = ap.parse_args()
    if not a.data:
        print("--data is required (or set UE_LIVE_BRIDGE_DATA)", file=sys.stderr)
        return 2
    DATA_DIR = a.data
    _restore_original = a.restore_original
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
