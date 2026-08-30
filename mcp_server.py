"""
MCP server over the GameBridge.

Five tools: ping, find_objects, read_property, call_function, write_property.

write_property returns a verdict rather than a boolean, so a caller cannot
report a state change it did not make -- the tool will not hand it the words.
Verification is not read-only; see the tool description and the README.

    pip install "mcp>=2"
    python mcp_server.py --data "<game>/.../Mods/GameBridge/data"

    claude mcp add ue-live -- python /abs/path/mcp_server.py --data "/abs/path/data"
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from drive import FileBridge, poison_for, restore_and_check
from verify import Verdict

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
        "WITHHELD -- 'unproven' is not 'succeeded'. INCONSISTENT_BRIDGE has two "
        "causes: a channel is lying, or the property is volatile and the game "
        "rewrote it between the two reads. Retrying helps with neither, but the "
        "second is not a bug -- check whether the game owns that field before "
        "concluding the bridge is broken. POISON_STUCK means the write worked "
        "but verification left a poison value live in the game: say so to the "
        "user and fix that before doing anything else with that property. "
        "WITHHELD also covers a value so large that no poison can be told "
        "apart from it in float64 -- the write may have landed, but nothing "
        "proved the check could have said otherwise."
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
        "treated as not having happened. "
        "SIDE EFFECT: verification performs FOUR writes, not one -- the target "
        "value, then a poison value (offset from the target, scaled to its "
        "magnitude) to prove the check can fail, then the final value. The poison is live in the game for roughly half a "
        "second. Do not point this at a property the game consumes continuously "
        "(health, position, timers) unless that excursion is acceptable. The "
        "restore is read back, so POISON_STUCK is reported rather than left "
        "for the game to discover."
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
        return {"verdict": Verdict.UNREADABLE.value,
                "detail": before.get("err", "read failed")}
    original = before["value"]

    w = b.send(op="write", obj=obj, prop=prop, value=value)
    if not w.get("ok"):
        return {"verdict": Verdict.WRITE_REJECTED.value,
                "detail": w.get("err", "write failed"), "original": original}
    # A bridge that answers ok but sets wrote=false is telling the truth about
    # not having written. The CLI has always read this field; this tool did not,
    # so the same response produced HONEST_FAILURE there and CONFIRMED here.
    if w.get("wrote") is False:
        return {"verdict": Verdict.HONEST_FAILURE.value,
                "detail": "the bridge reported that the write did not succeed, and "
                          "did not claim otherwise; nothing to catch here -- fix the write",
                "original": original}

    def near(a: Any, x: float) -> bool:
        return isinstance(a, (int, float)) and abs(float(a) - x) < 1e-6

    landed_at_write = near(w.get("after"), value)

    r = b.send(op="read", obj=obj, prop=prop)
    holds = r.get("ok") and near(r.get("value"), value)

    if landed_at_write != holds:
        return {
            "verdict": Verdict.INCONSISTENT_BRIDGE.value,
            "detail": "the write site and an independent re-read disagree about "
                      "whether the value changed. Either a channel is lying, or "
                      "the game rewrote a volatile property between the two reads; "
                      "a channel cannot verify itself, so no verdict is available",
            "write_site_after": w.get("after"),
            "reread": r.get("value"),
            "original": original,
        }

    if not holds:
        return {
            "verdict": Verdict.FALSE_SUCCESS.value,
            "detail": "the bridge accepted the write but the value did not change; "
                      "both channels agree",
            "observed": r.get("value"),
            "original": original,
        }

    # The value looks right. That is not yet a reason to believe the check.
    #
    # Everything from here until the restore puts a poison value into a live
    # game, so it runs under try/finally. An exception between the poison write
    # and the restore -- a timeout while the game hitches, an object that got
    # collected -- would otherwise leave target+1234 in the world as the last
    # thing this tool ever wrote, and say nothing about it.
    want_final = original if _restore_original else value
    # The poison must be distinguishable from the value AND from whatever the
    # restore will put back, or a correct bridge gets convicted. See poison_for.
    chosen = poison_for(value, avoid=(float(want_final),)
                        if isinstance(want_final, (int, float)) else ())
    if chosen is None:
        return {
            "verdict": Verdict.WITHHELD.value,
            "detail": f"no poison value can be told apart from {value} at this "
                      "magnitude, so the check was never shown to be capable of "
                      "failing. The write may well have landed; this run cannot "
                      "say the check would have noticed if it had not",
            "original": original,
        }
    poison = chosen
    cleanup: dict = {"state": "not attempted"}

    try:
        pw = b.send(op="write", obj=obj, prop=prop, value=poison)
        poison_landed = near(pw.get("after"), poison)
        pr = b.send(op="read", obj=obj, prop=prop)
        noticed = not (pr.get("ok") and near(pr.get("value"), value))
    except Exception as e:
        cleanup = restore_and_check(b, obj, prop, want_final, poison)
        return {
            "verdict": Verdict.RESTORE_UNVERIFIED.value,
            "detail": f"verification failed partway through ({type(e).__name__}: {e}). "
                      "A poison value may have been written; see cleanup",
            "original": original, "poison_value": poison, "cleanup": cleanup,
        }

    cleanup = restore_and_check(b, obj, prop, want_final, poison)

    # Ordering note: cleanup state is attached to every verdict rather than
    # gating them, because "the check is dead" and "the poison is still in the
    # game" are separate facts and the caller needs both. Only the last two
    # branches let cleanup decide the verdict, because only there is everything
    # else already known to be fine.
    if not poison_landed:
        return {
            "verdict": Verdict.WITHHELD.value,
            "detail": "the poison never applied, so the check was never shown to be "
                      "capable of failing; the apparent success is unproven",
            "original": original, "cleanup": cleanup,
        }
    if not noticed:
        return {
            "verdict": Verdict.DEAD_CHECK.value,
            "detail": "the check survived a poison that provably landed; it cannot "
                      "distinguish success from failure and its verdict means nothing. "
                      "Reads through this bridge cannot be trusted, so the cleanup "
                      "state below is itself suspect",
            "original": original, "cleanup": cleanup,
        }
    if cleanup["state"] == "poisoned":
        return {
            "verdict": Verdict.POISON_STUCK.value,
            "detail": "the write landed and the check is alive, but the restore did "
                      "not take: the game is holding the poison value. Fix this "
                      "before anything else reads that property",
            "original": original, "poison_value": poison, "cleanup": cleanup,
        }
    if cleanup["state"] != "restored":
        # An unreadable read-back is not evidence of a clean world. Reporting
        # CONFIRMED here would be this tool doing the exact thing it exists to
        # catch: turning "I could not tell" into "it worked".
        return {
            "verdict": Verdict.RESTORE_UNVERIFIED.value,
            "detail": "the write and the check both verified, but the cleanup could "
                      "not be read back, so whether the poison is still in the game "
                      "is unknown. Treat that property as suspect until you have "
                      "read it yourself",
            "original": original, "poison_value": poison, "cleanup": cleanup,
        }

    return {
        "verdict": Verdict.CONFIRMED.value,
        "detail": "independently observed, the check was proven able to fail, and "
                  "the poison was confirmed out of the world",
        "value": want_final,
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
