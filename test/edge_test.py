"""
The cases a fake bridge that speaks perfect JSON never produces.

Every scenario in the demo goes through `json.dumps`, full float64 precision,
always with a `value` key, and never interrupted. A real game does none of those
things, and each fix below closes a gap that only shows up off that path. Each
one also gets a test here, because the review that found them also found that
the previous round's fixes had no way to fail.

    python test/edge_test.py
"""

from __future__ import annotations

import pathlib
import struct
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from drive import near, probe, restore_and_check, target_for  # noqa: E402
from verify import Verdict  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS ' if ok else 'FAIL '} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def f32(x: float) -> float:
    """What a float32 UE property stores when you ask for x."""
    return struct.unpack("f", struct.pack("f", x))[0]


class StubBridge:
    """A scripted bridge. Each op pops the next canned response."""

    def __init__(self, script, raise_at=None, raise_with=None):
        self.script = list(script)
        self.raise_at = raise_at
        self.raise_with = raise_with or RuntimeError("stub")
        self.sent = []

    def send(self, **cmd):
        self.sent.append(cmd)
        if self.raise_at is not None and len(self.sent) == self.raise_at:
            raise self.raise_with
        return self.script.pop(0) if self.script else {"ok": False, "err": "script empty"}

    def read(self, obj, prop):
        r = self.send(op="read", obj=obj, prop=prop)
        if not r.get("ok"):
            raise RuntimeError(r.get("err", "read failed"))
        return r["value"]


def main() -> int:
    print("edge cases\n" + "=" * 62)

    print("\n[1] float32 quantisation is not a failed write")
    # An honest bridge writing 250.3 to a float32 property stores 250.3000030518.
    # Under a flat 1e-6 tolerance both channels agreed it "did not change" and
    # the verdict was FALSE_SUCCESS -- against a bridge that did as it was told.
    for v in (100.7, 250.3, 1000.55, 5000.13, 42.0, 0.5, -318.42):
        check(f"asked {v!r}, game stored {f32(v)!r}", near(f32(v), v))

    print("\n[2] and a real change is still a change")
    for v, other in ((100.0, 107.0), (100.0, 100.001), (0.0, 1e-5),
                     (1e6, 1e6 + 10.0), (250.3, 250.31)):
        check(f"{v!r} is not {other!r}", not near(other, v))

    print("\n[3] ok:true with no value key is a verdict, not a KeyError")
    # The Lua side emits exactly this for a property that is currently nil:
    # jval drops nil-valued keys, so `{ok=true, vtype="nil"}` goes on the wire.
    b = StubBridge([
        {"ok": True, "world": "L", "travelling": False},
        {"ok": True, "count": 1, "objects": ["BP_X_C /x.x:y"]},
        {"ok": True, "vtype": "nil"},                     # no "value"
    ])
    try:
        v = probe(b, "BP_X_C", "Health")
        check("probe returns UNREADABLE", v is Verdict.UNREADABLE, f"got {v}")
    except KeyError as e:
        check("probe returns UNREADABLE", False, f"raised KeyError({e})")

    print("\n[4] the restore runs even when the poison step is interrupted")
    # KeyboardInterrupt is not an Exception. `except Exception` let Ctrl-C during
    # the eight-second poll skip the restore and leave the poison in a live game.
    b = StubBridge(
        script=[
            {"ok": True, "world": "L", "travelling": False},
            {"ok": True, "count": 1, "objects": ["BP_X_C /x.x:y"]},
            {"ok": True, "value": 100.0},                       # [3] read
            {"ok": True, "wrote": True, "before": 100.0, "after": 107.0},
            {"ok": True, "value": 107.0},                       # [5] re-read
            {"ok": True, "wrote": True, "before": 107.0, "after": 1341.0},
            {"ok": True, "wrote": True, "before": 1341.0, "after": 100.0},  # restore
            {"ok": True, "value": 100.0},                       # cleanup read
        ],
        raise_at=7,                       # the poison re-read
        raise_with=KeyboardInterrupt(),
    )
    interrupted = False
    try:
        probe(b, "BP_X_C", "Health")
    except KeyboardInterrupt:
        interrupted = True
    writes = [c for c in b.sent if c.get("op") == "write"]
    check("the interrupt still propagates", interrupted)
    check("a restore write was issued anyway", len(writes) >= 3,
          f"{len(writes)} writes: {[w.get('value') for w in writes]}")
    check("and it wrote the original back",
          any(near(w.get("value"), 100.0) for w in writes[2:]))

    print("\n[5] a write target is a change, at every magnitude")
    # A flat +7 is not one: at 1e20 the sum IS the original, so the demo row
    # reading "writes land on a 1e20 property" was checking a write of the value
    # already there. The label and the evidence disagreed and nothing said so.
    for o in (0.0, 100.0, 1e6, 1e15, 1e18, 1e20, 1e21, -1e20):
        t = target_for(o)
        check(f"original {o!r} -> target {t!r}", not near(t, o) and t != o)
    check("a flat +7 would NOT be a change at 1e20", 1e20 + 7.0 == 1e20,
          "which is the whole reason target_for exists")

    print("\n[6] restore_and_check tells the four states apart")
    cases = [
        ("restored", [{"ok": True, "wrote": True, "after": 100.0},
                      {"ok": True, "value": 100.0}]),
        ("poisoned", [{"ok": True, "wrote": True, "after": 1341.0},
                      {"ok": True, "value": 1341.0}]),
        ("unknown", [{"ok": True, "wrote": True, "after": 100.0},
                     {"ok": False, "err": "object not found"}]),
        # The read WORKED and returned a third value. Folding this into
        # "unknown" made both callers report "could not be read back" about a
        # read that had succeeded, and hid that the poison was provably out.
        ("diverged", [{"ok": True, "wrote": True, "after": 100.0},
                      {"ok": True, "value": 63.5}]),
    ]
    for want, script in cases:
        got = restore_and_check(StubBridge(script), "o", "p", 100.0, 1341.0)
        check(f"state is {want!r}", got["state"] == want, f"got {got['state']!r}")

    print("\n" + "=" * 62)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
