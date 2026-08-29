"""
A simulated live game, standing in for a UE4SS / BepInEx bridge.

Exists so the verification logic can be proven before any game is launched, and
so the two failure modes it targets can be reproduced on demand:

  * a bridge that reports a write succeeded when it did not  (false success)
  * a postcondition that can never fail                      (dead check)

The real bridge speaks the same shape over a command file plus a JSONL response
tail -- the file-IPC pattern already proven in shipped mods -- so swapping this
for the real thing changes the transport, not the verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SimGame:
    """Authoritative world state. Nothing here is visible to the agent directly."""

    objects: dict[str, dict[str, Any]] = field(default_factory=lambda: {
        "BP_PlayerCharacter_C_0": {"Health": 100.0, "MaxHealth": 100.0, "Team": 0},
        "BP_GameState_C_0": {"RoundNumber": 3, "Floor": 7},
    })

    # When True, writes are acknowledged but silently dropped. This is the
    # behaviour a real bridge exhibits when a hook attached to a stale object,
    # when the property name resolved to a different field, or when the game
    # overwrote the value on the next tick.
    drop_writes: bool = False

    def read(self, obj: str, prop: str) -> Any:
        return self.objects[obj][prop]

    def write(self, obj: str, prop: str, value: Any) -> bool:
        """Returns the *claim*, which is not the same thing as the effect."""
        if self.drop_writes:
            return True          # "success"
        self.objects[obj][prop] = value
        return True


@dataclass
class AgentHandle:
    """What the agent acts through. Write access."""
    game: SimGame

    def set_property(self, obj: str, prop: str, value: Any) -> bool:
        return self.game.write(obj, prop, value)


@dataclass
class VerifierHandle:
    """What the verifier reads through. Read-only, and a separate object.

    Sharing a handle with the agent would let a broken bridge confirm its own
    claim, which is the mistake the whole design exists to avoid.
    """
    game: SimGame

    def read(self, obj: str, prop: str) -> Any:
        return self.game.read(obj, prop)
