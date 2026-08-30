#!/usr/bin/env python3
"""
Check that the Lua sources actually parse.

This exists because the obvious one-liner does not work:

    L.eval("load")(src, name)          # WRONG

Lua's `load` returns `nil, errmsg` on failure. Without
`unpack_returned_tuples=True` lupa hands that back as a two-element tuple,
and a non-empty tuple is truthy in Python -- so `if chunk:` passes for a file
that does not compile. A syntax checker that reports OK for broken input is
not a syntax checker, and this one shipped that way for a while.

So: unpack the returns, look at the first one, and surface the error text.
`--self-test` feeds the checker deliberately broken Lua and requires it to go
red, because the whole point is that this check must be capable of failing.

    python test/lua_syntax.py
    python test/lua_syntax.py --self-test
"""

from __future__ import annotations

import pathlib
import sys

from lupa import LuaRuntime

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGETS = [
    ROOT / "bridge" / "GameBridge" / "Scripts" / "main.lua",
]

BROKEN = 'local s = "([^\nunfinished'


def compiles(src: str, name: str) -> tuple[bool, str]:
    """(ok, error). Unpacks Lua's (chunk, err) so a failure is visible."""
    lua = LuaRuntime(unpack_returned_tuples=True)
    res = lua.eval("load")(src, name)
    if isinstance(res, tuple):
        chunk, err = (res + (None, None))[:2]
    else:
        chunk, err = res, None
    return (chunk is not None), (str(err) if err else "")


def main() -> int:
    if "--self-test" in sys.argv:
        ok, err = compiles(BROKEN, "broken")
        print(f"negative control: broken Lua -> ok={ok}  err={err[:70]!r}")
        if ok:
            print("FAIL: the checker accepted invalid Lua. It cannot detect anything.")
            return 1
        print("checker is capable of failing\n")

    bad = 0
    for path in TARGETS:
        if not path.exists():
            print(f"  MISS  {path.relative_to(ROOT)}")
            bad += 1
            continue
        ok, err = compiles(path.read_text(encoding="utf-8"), path.name)
        print(f"  {'OK   ' if ok else 'FAIL '} {path.relative_to(ROOT)}"
              + ("" if ok else f"\n        {err}"))
        if not ok:
            bad += 1

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
