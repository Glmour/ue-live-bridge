"""
Verify that write_property returns the right verdict for each kind of bridge.

The tool's whole value is that it will not say CONFIRMED unless it earned it, so
this drives it against a bridge that is honest, one that lies, and one that lies
about the read-back too, and asserts the verdict each time.

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
]

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


def main() -> int:
    print("write_property verdict test\n" + "=" * 60)
    for i, (label, flags, want) in enumerate(CASES):
        run_case(label, flags, want, i)
    print("=" * 60)
    if failures:
        print(f"FAILED: {', '.join(failures)}")
        return 1
    print("all verdicts correct")
    return 0


if __name__ == "__main__":
    sys.exit(main())
