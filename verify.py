"""
Negative-control verification for agent actions against a live game.

The industry answer to agents falsely claiming success is: assert a postcondition
against the authoritative record, from a process the agent cannot write to.

That is necessary and not sufficient. Nobody checks whether the checker works.
A postcondition that can never go red is indistinguishable from one that passes,
and in practice a surprising number of guards are dead on arrival -- they read the
wrong field, they swallow an exception, they compare a string to an int.

So the rule here is stricter:

    A postcondition may not be registered without a poison that provably makes it
    go red. Before any verdict is trusted, every postcondition is run against its
    own poison. A check that survives its poison is a DEAD CHECK and is reported
    as a harness failure, not as a passing test.

This is the same discipline that came out of shipping game mods, where "the hook
attached" and "the hook fires" are separate claims and the gap between them
silently eats days.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol


class Bridge(Protocol):
    """Read-only view of the live game, from the verifier's side.

    The verifier holds a *separate* handle from the one the agent acts through.
    Reading back through the same object the agent wrote to would let a broken
    or lying bridge confirm its own claim.
    """

    def read(self, obj: str, prop: str) -> Any: ...


class Verdict(str, Enum):
    CONFIRMED = "CONFIRMED"      # the agent claimed success and the world agrees
    FALSE_SUCCESS = "FALSE_SUCCESS"   # the agent claimed success, the world disagrees
    HONEST_FAILURE = "HONEST_FAILURE"  # the agent reported failure; not our problem
    DEAD_CHECK = "DEAD_CHECK"    # the postcondition survived its own poison


@dataclass
class Postcondition:
    """A claim about the world, plus proof that the claim can fail.

    check:  reads authoritative state through the verifier's own bridge and
            returns True when the world is in the expected state.
    poison: mutates the world (or the bridge's view of it) into a state that
            MUST make `check` return False. This is the negative control.
    """

    name: str
    check: Callable[[Bridge], bool]
    poison: Callable[[Bridge], None]
    undo_poison: Callable[[Bridge], None]

    def __post_init__(self) -> None:
        if self.poison is None or self.undo_poison is None:
            raise ValueError(
                f"postcondition {self.name!r} has no negative control; "
                "a check you cannot make fail is not a check"
            )


@dataclass
class Result:
    verdict: Verdict
    postcondition: str
    claimed: bool
    observed: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.verdict in (Verdict.CONFIRMED, Verdict.HONEST_FAILURE)


@dataclass
class Verifier:
    bridge: Bridge
    dead_checks: list[str] = field(default_factory=list)

    def prove_alive(self, pc: Postcondition) -> bool:
        """Negative control: poison the world, require the check to notice.

        Runs before any verdict from this postcondition is trusted. Restores the
        world afterwards regardless of outcome.
        """
        try:
            pc.poison(self.bridge)
            noticed = not pc.check(self.bridge)
        finally:
            pc.undo_poison(self.bridge)

        if not noticed:
            self.dead_checks.append(pc.name)
        return noticed

    def verify(self, pc: Postcondition, agent_claimed_success: bool) -> Result:
        """The whole point: separate what the agent *said* from what is *true*.

        `agent_claimed_success` is the agent's own assertion, which is exactly the
        signal the published measurements show to be wrong 45-76% of the time when
        it fails. It is recorded, never trusted.
        """
        if not self.prove_alive(pc):
            return Result(
                Verdict.DEAD_CHECK, pc.name, agent_claimed_success, False,
                "postcondition survived its own poison; verdict withheld",
            )

        observed = pc.check(self.bridge)

        if agent_claimed_success and observed:
            return Result(Verdict.CONFIRMED, pc.name, True, True)
        if agent_claimed_success and not observed:
            return Result(
                Verdict.FALSE_SUCCESS, pc.name, True, False,
                "agent reported success; authoritative state disagrees",
            )
        return Result(
            Verdict.HONEST_FAILURE, pc.name, False, observed,
            "agent did not claim success",
        )


def prop_equals(obj: str, prop: str, want: Any) -> Callable[[Bridge], bool]:
    """The common case: a property should now hold a given value."""
    def _check(b: Bridge) -> bool:
        try:
            return b.read(obj, prop) == want
        except Exception:
            # A check that cannot read is a check that cannot pass. Never
            # swallow this into a True -- that is how dead checks are born.
            return False
    return _check
