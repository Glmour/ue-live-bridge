"""
Verify that write_property returns the right verdict for each kind of bridge.

The tool's whole value is that it will not say CONFIRMED unless it earned it, so
this drives it against every bridge the simulator can be -- honest, silently
dropping writes, lying
about the read-back, and serving stale reads -- and asserts the verdict each
time. The last one is the only case the negative control alone can catch.

    python test/mcp_test.py
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OBJ = ("BP_PlayerCharacter_C /Game/Maps/L_Test.L_Test:"
       "PersistentLevel.BP_PlayerCharacter_C_0")
PROP = "Health"

CASES = [
    ("honest bridge",              [],                        "CONFIRMED"),
    ("lying bridge",               ["--lie"],                 "FALSE_SUCCESS"),
    ("lying bridge + faked reads", ["--lie", "--deadcheck"],  "INCONSISTENT_BRIDGE"),
    # Both channels agree and every assertion passes. Only the poison exposes it.
    ("stale reader",               ["--stale-reads"],         "DEAD_CHECK"),
    # A review found three verdict branches with no case at all -- their guards
    # could rot and this file would still print "all verdicts correct". A probe
    # writes three times: target, poison, restore.
    ("honest bridge that fails",   ["--wrote-false"],             "HONEST_FAILURE"),
    ("poison refused",             ["--refuse-nth-write", "2"],   "WITHHELD"),
    ("restore refused",            ["--refuse-nth-write", "3"],   "POISON_STUCK"),
    ("cleanup unreadable",         ["--vanish-after-write", "3"], "RESTORE_UNVERIFIED"),
    # A property that is currently nil: the Lua side sends ok:true with no
    # `value` key at all, because jval drops nil-valued keys. Both front ends
    # used to raise KeyError on it, and a traceback is not a verdict.
    ("property reads as nil",      ["--nil-value"],               "UNREADABLE"),
]

# Values no comparison can verify afterwards, refused before anything is
# written. The guard for this was unreachable when first added: inf and nan
# died at an earlier check, so it could never fire.
NONFINITE = [("+inf", float("inf")), ("nan", float("nan"))]

failures: list[str] = []


def run_case(label: str, flags: list[str], want: str, i: int) -> None:
    data = ROOT / f"_mcp_t{i}"
    if data.exists():
        for f in data.iterdir():
            f.unlink()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "test" / "fake_bridge.py"), "--data", str(data), *flags],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(1.5)
        # Import fresh so the module-level bridge handle points at this case's dir.
        for m in ("mcp_server", "drive"):
            sys.modules.pop(m, None)
        import mcp_server
        mcp_server.DATA_DIR = str(data)
        mcp_server._bridge = None

        got = mcp_server.write_property(OBJ, PROP, 250.0)
        verdict = got.get("verdict")
        ok = verdict == want
        print(f"  {'PASS ' if ok else 'FAIL '} {label:<28} -> {verdict}")
        if not ok:
            print(f"        want {want}; detail: {got.get('detail')}")
            failures.append(label)
        elif got.get("detail"):
            print(f"        {got['detail'][:78]}")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        for f in data.iterdir():
            f.unlink()
        data.rmdir()


def run_nonfinite(label: str, value: float) -> None:
    """No bridge needed: this is refused before a single command is sent."""
    for m in ("mcp_server", "drive"):
        sys.modules.pop(m, None)
    import mcp_server
    mcp_server.DATA_DIR = str(ROOT / "_nonexistent_on_purpose")
    mcp_server._bridge = None
    got = mcp_server.write_property(OBJ, PROP, value)
    ok = got.get("verdict") == "WITHHELD"
    print(f"  {'PASS ' if ok else 'FAIL '} refuses to write {label:<14} -> "
          f"{got.get('verdict')}")
    if not ok:
        failures.append(f"non-finite {label}")


def main() -> int:
    print("write_property verdict test\n" + "=" * 60)
    for i, (label, flags, want) in enumerate(CASES):
        run_case(label, flags, want, i)
    for label, value in NONFINITE:
        run_nonfinite(label, value)
    print("=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all verdicts correct")
    return 0


if __name__ == "__main__":
    sys.exit(main())
