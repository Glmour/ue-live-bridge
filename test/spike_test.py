"""
Verification logic, run off-engine against a simulated game that can be told to lie.

Four scenarios: honest bridge, lying bridge, dead check, and both at once. The
last is the one that matters -- a broken checker against a lying bridge is what
produces a confident and entirely false pass, and it is what the negative control
exists to catch.

    python test/spike_test.py
"""

from __future__ import annotations

import sys

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from sim_game import AgentHandle, SimGame, VerifierHandle
from verify import Postcondition, Verdict, Verifier, prop_equals

OBJ = "BP_PlayerCharacter_C_0"
PROP = "Health"
TARGET = 250.0

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'PASS ' if ok else 'FAIL '} {label}: {got}" + ("" if ok else f"  (want {want})"))
    if not ok:
        failures.append(label)


def live_postcondition(game: SimGame) -> Postcondition:
    """A real check with a real negative control."""
    saved: dict = {}

    def poison(_b) -> None:
        saved["v"] = game.objects[OBJ][PROP]
        game.objects[OBJ][PROP] = -99999.0     # a value the check must reject

    def undo(_b) -> None:
        game.objects[OBJ][PROP] = saved["v"]

    return Postcondition(
        name="health_is_target",
        check=prop_equals(OBJ, PROP, TARGET),
        poison=poison,
        undo_poison=undo,
    )


def dead_postcondition(game: SimGame) -> Postcondition:
    """A check that can never fail.

    Deliberately written the way real dead checks get written: it means to
    compare the value, but the comparison was lost in a refactor and what is
    left just confirms the property exists.
    """
    saved: dict = {}

    def poison(_b) -> None:
        saved["v"] = game.objects[OBJ][PROP]
        game.objects[OBJ][PROP] = -99999.0

    def undo(_b) -> None:
        game.objects[OBJ][PROP] = saved["v"]

    def broken_check(b) -> bool:
        try:
            b.read(OBJ, PROP)
            return True          # <-- reads, then asserts nothing
        except Exception:
            return False

    return Postcondition(
        name="health_is_target__but_broken",
        check=broken_check,
        poison=poison,
        undo_poison=undo,
    )


def scenario(label: str, *, drop_writes: bool, dead: bool):
    game = SimGame(drop_writes=drop_writes)
    agent = AgentHandle(game)
    verifier = Verifier(bridge=VerifierHandle(game))
    pc = dead_postcondition(game) if dead else live_postcondition(game)

    claimed = agent.set_property(OBJ, PROP, TARGET)   # the agent's own assertion
    result = verifier.verify(pc, agent_claimed_success=claimed)

    print(f"\n[{label}]")
    print(f"  bridge={'LYING' if drop_writes else 'honest'}  check={'DEAD' if dead else 'live'}")
    print(f"  agent claimed success : {claimed}")
    print(f"  actual value in world : {game.objects[OBJ][PROP]}")
    return game, pc, verifier, result


def main() -> int:
    print("negative-control verification spike\n" + "=" * 52)

    # 1 -------------------------------------------------------------------
    _, _, _, r = scenario("1. honest bridge, live check", drop_writes=False, dead=False)
    check("verdict", r.verdict, Verdict.CONFIRMED)

    # 2 -------------------------------------------------------------------
    _, _, _, r = scenario("2. LYING bridge, live check", drop_writes=True, dead=False)
    check("verdict", r.verdict, Verdict.FALSE_SUCCESS)
    check("agent claimed success", r.claimed, True)
    check("world disagreed", r.observed, False)

    # 3 -------------------------------------------------------------------
    _, _, v, r = scenario("3. honest bridge, DEAD check", drop_writes=False, dead=True)
    check("verdict", r.verdict, Verdict.DEAD_CHECK)
    check("dead check named", v.dead_checks, ["health_is_target__but_broken"])

    # 4 -------------------------------------------------------------------
    game, pc, v, r = scenario("4. LYING bridge, DEAD check  <- the money shot",
                              drop_writes=True, dead=True)
    check("verdict", r.verdict, Verdict.DEAD_CHECK)

    # What the standard approach -- assert postconditions, no negative control --
    # would have emitted for this exact scenario:
    naive_says_ok = pc.check(v.bridge)
    print(f"\n  without the negative control, this run reports: "
          f"{'CONFIRMED (FALSE)' if naive_says_ok else 'caught'}")
    print(f"  actual value in world is still {game.objects[OBJ][PROP]}, "
          f"never {TARGET}")
    check("naive postcondition would have passed a false success", naive_says_ok, True)

    print("\n" + "=" * 52)
    if failures:
        print(f"FAILED: {len(failures)} check(s): {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
