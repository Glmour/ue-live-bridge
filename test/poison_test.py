"""
The poison chooser, on its own.

The demo covers the case that produced this code -- a 1e20 property where a flat
+1234 vanishes into float64 and an honest bridge gets convicted of a DEAD_CHECK.
It does not cover the rest of `poison_for`: the demo never runs with
--restore-original, so the clause that keeps a poison away from the restore
target has nothing exercising it, and removing that clause leaves the demo
green. A guard nothing can kill is the thing this repository is about.

    python test/poison_test.py
"""

from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from drive import near, poison_for  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS ' if ok else 'FAIL '} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def main() -> int:
    print("poison_for\n" + "=" * 58)

    # The bug this was written for. 1e20 + 1234 == 1e20 in float64, so the old
    # poison was the value, the check "failed" to notice it, and the verdict
    # was DEAD_CHECK against a bridge that had done nothing wrong.
    print("\n[1] a poison must differ from the value it poisons")
    for v in (0.0, 1.0, 100.0, 1e6, 1e15, 1e18, 1e20, 1e21, 1e300,
              -1e20, -100.0, 0.001):
        p = poison_for(v)
        ok = p is not None and p != v and not near(p, v)
        check(f"value {v!r}", ok, f"poison={p!r}")

    print("\n[2] and from whatever the restore will put back")
    # The --restore-original collision: original == value + 1234 made a correct
    # restore read back as the poison, and reported POISON_STUCK.
    for value, avoid in ((100.0, 1334.0), (0.0, 1234.0), (5.0, 1239.0),
                         (1e6, 1000000.0 + 1234.0)):
        p = poison_for(value, avoid=(avoid,))
        ok = p is not None and not near(p, avoid) and not near(p, value)
        check(f"value {value!r}, restore target {avoid!r}", ok, f"poison={p!r}")

    print("\n[3] when no poison can be told apart, say so rather than guess")
    for v in (float("inf"), float("-inf"), float("nan"), "not a number", None, True):
        p = poison_for(v)  # type: ignore[arg-type]
        check(f"{v!r} -> None", p is None, f"got {p!r}")

    print("\n[4] the value 1e20 specifically, since that is where it broke")
    v = 1e20
    check("flat +1234 is absorbed at 1e20", v + 1234.0 == v,
          "which is why the delta cannot be a constant")
    p = poison_for(v)
    check("poison_for still finds one", p is not None and p != v, f"poison={p!r}")
    check("and it survives a round trip through float", float(repr(p)) != v)

    print("\n[5] nothing returned is ever nan or inf")
    for v in (1e308, -1e308, 1.7e308):
        p = poison_for(v)
        ok = p is None or (not math.isnan(p) and not math.isinf(p))
        check(f"value {v!r}", ok, f"poison={p!r}")

    print("\n" + "=" * 58)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
