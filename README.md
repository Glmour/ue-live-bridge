# ue-live-bridge

**Drive a running Unreal Engine game from outside — and find out whether what you asked for actually happened.**

A [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) Lua mod exposes the live game over a pair of append-only files. A Python driver on the other side reads and writes UObject properties, calls UFunctions, and — the part that matters — refuses to report success it cannot prove.

It speaks MCP, so an agent can drive the game directly — and the write tool
returns a verdict rather than a boolean, so an agent cannot report a state
change it did not make.

No engine source, no game source, no C++, no sockets.

```
$ python drive.py --data "<game>/Binaries/Win64/ue4ss/Mods/GameBridge/data" probe BP_ChangeManager_C --prop DEBUG_ANOMALY_TYPE

[1] ping
    world=World /Game/_Exit8/Level/L_Game.L_Game  travelling=False
[2] find BP_ChangeManager_C
    found 1: ['BP_ChangeManager_C /Game/_Exit8/Level/L_Game1...BP_ChangeActorManager_C_1']
[3] read DEBUG_ANOMALY_TYPE
    DEBUG_ANOMALY_TYPE = 36
[4] write DEBUG_ANOMALY_TYPE <- 43.0
    bridge claimed: True   (before=36 after=43)
[5] postcondition: independent re-read
    re-read DEBUG_ANOMALY_TYPE = 43  -> target
[6] cross-channel agreement
    write-site says landed: True    re-read says landed: True
[7] negative control: prove the check can go red
    poison landed at write site: True
    re-read after poison: 1277; check went RED
[8] restore
    DEBUG_ANOMALY_TYPE = 36

Chain verified end to end, and the check is provably alive.
```

That is a real transcript against The Exit 8, not a mock.

---

## Why steps 6 and 7 exist

Automated callers — agents especially — report success they did not achieve. This is measured, not folklore: across published trajectory studies, 45–48% of agent failures in single-control settings end with the agent asserting it finished, and among coding-agent runs that wrote an explicit structured success flag, **75.8% of those claims were wrong**. LLM judges do not catch it; across five judges and five prompt strategies none exceeded AUROC 0.65, and on raw call traces they sat near a coin flip, because judges reward assertion vocabulary rather than verified state change.

The standard fix is right as far as it goes: assert a postcondition against the authoritative record, from a process the caller cannot write to. This tool does that at step 5.

But a postcondition that can never fail is indistinguishable from one that passes, and dead checks are common — a comparison lost in a refactor, an exception swallowed into a `True`, a field name that silently resolves to something else. So step 7 poisons the world and requires the check to notice. **A check that survives its own poison is reported as a harness failure, not as a passing test.**

### The bug this design found in itself

The first version of the negative control ran the poison through the same write path as the action. Against a bridge that acknowledged writes without performing them *and* faked the read-back, the harness printed:

```
[5] re-read Health = 107.0  -> CONFIRMED
[6] poisoned to 1341.0; check went RED (alive)
    Chain verified end to end, and the check is provably alive.
```

The value was 100.0 the entire time. The poison had not landed either, and its failure to land is exactly what made the faked read look like it had changed.

Two rules came out of that, and they are the whole design now:

1. **A channel cannot verify itself.** The write response carries its own before/after observation; treat it as a second independent channel and withhold any verdict when the two disagree, rather than picking one to believe.
2. **A negative control proves nothing until the poison is shown to have landed.** And the negative control is what licenses trusting a *pass* — a failure that two independent channels agree on is already conclusive without it.

## Verdicts

The CLI and the MCP tool report the same five outcomes.

| MCP `verdict` | CLI line | Meaning | Exit |
|---|---|---|---|
| `CONFIRMED` | Chain verified end to end | Both channels agree, and the check was proven capable of failing | 0 |
| `FALSE_SUCCESS` | FALSE SUCCESS caught | The bridge claimed a write it did not make; both channels agree | 1 |
| `INCONSISTENT_BRIDGE` | INCONSISTENT BRIDGE | Write site and re-read disagree; no verdict is available | 1 |
| `WITHHELD` | VERDICT WITHHELD | The poison never applied, so the apparent pass is unproven | 1 |
| `DEAD_CHECK` | DEAD CHECK | The check survived a poison that provably landed | 1 |

Only the first is a success. The other four are distinct facts, and collapsing
them into "failed" throws away the part that tells you what to do next.

## Install

Requires UE4SS in the target game.

```bash
cp -r bridge/GameBridge "<game>/Binaries/Win64/ue4ss/Mods/"
mkdir -p "<game>/Binaries/Win64/ue4ss/Mods/GameBridge/data"
# add "GameBridge : 1" to Mods/mods.txt, above the Keybinds line
```

Then, with the game running and in a level:

```bash
python drive.py --data "<game>/.../Mods/GameBridge/data" ping
python drive.py --data "<game>/.../Mods/GameBridge/data" find BP_PlayerCharacter_C
python drive.py --data "<game>/.../Mods/GameBridge/data" probe BP_PlayerCharacter_C --prop Health
```

Blueprint classes take the `_C` suffix — `BP_ChangeManager_C`, not `BP_ChangeManager`.

## Use it from an agent (MCP)

```bash
pip install "mcp>=2"
claude mcp add ue-live -- python /abs/path/mcp_server.py --data "/abs/path/to/GameBridge/data"
```

Five tools: `ping`, `find_objects`, `read_property`, `call_function`, `write_property`.

`write_property` is the one that matters. It never returns a bare boolean; it
returns one of the verdicts below, and the server's instructions tell the client
to treat anything other than `CONFIRMED` as the write not having happened —
including `WITHHELD`, because *unproven* is not *succeeded*.

```json
{
  "verdict": "CONFIRMED",
  "detail": "independently observed, and the check was proven able to fail",
  "value": 21.0,
  "original": 36
}
```

That transcript is from The Exit 8, through the MCP tool, against the running game.

## Tests

The verification logic runs off-engine, against a simulated game that can be told to lie:

```bash
python test/spike_test.py                              # 4 scenarios, no game needed
python test/mcp_test.py                                # MCP verdicts, all three bridges
python test/fake_bridge.py --data ./_t --lie           # then: python drive.py --data ./_t probe
```

`fake_bridge.py` reproduces both failure modes on demand (`--lie`, `--deadcheck`), which is how the design flaw above was found and how the fix was confirmed.

## Design notes

Each of these was paid for:

- **The command file is never rewound.** A line cursor is set to the file's current length at load, so commands left over from a previous session are not replayed. Without this, whoever starts the game executes the leftovers — a stale write lands minutes after anyone asked for it, and the symptom looks nothing like the cause.
- **`resolve` caches, and re-validates on every hit.** The engine recycles object slots, so a pointer that still passes `IsValid()` can belong to something else; the cached name is checked again before use. `find` populates the cache with objects it is already holding, so the normal find-then-act flow does no scanning at all — measured at **7 resolve hits, 0 full scans** across a complete probe run.
- **The cache is dropped during travel.** Objects do not survive a level change, and a stale entry that happened to re-validate would hand back an object from the old world.
- **Arrays are tagged.** An empty Lua table is ambiguous between `{}` and `[]`; without a tag an empty result set serialises as an object and breaks the reader's indexing.
- **Every command runs inside `pcall`.** A malformed request must not take the poll loop down with it.
- **File names are pure ASCII.** Lua's `io.open` goes through the system ANSI codepage, and a non-ASCII path fails to open without saying so.

## What was verified, and what wasn't

**Verified** — The Exit 8 (UE 5.2), real session, transcript above:

- Mod loads, poll loop runs, **0 loop errors across 314 ticks**
- `ping` / `find` / `read` / `write` / `call` against live objects
- Round trip ~200 ms including Python process startup
- Negative control confirmed alive; game state restored afterwards
- MCP tools end to end against the live game; `write_property` returned CONFIRMED
- Off-engine: 4/4 spike scenarios and 3/3 MCP verdicts, including the adversarial ones

**Not verified:**

- **Large worlds.** The Exit 8 is one corridor with a single player and controller. The cache means the common path does no scanning, but a cache miss still falls back to a linear sweep of the whole object array, and that has never been measured on an open-world title.
- **Long sessions.** The longest run here is minutes, not hours.
- **Level transitions.** The cache is dropped on travel by construction, but the behaviour has not been exercised across an actual transition.
- **Engines other than 5.2.**

## Scope

This drives games you own, for modding and tooling. It is not an anti-cheat bypass, it does not touch competitive multiplayer, and the bridge is deliberately an open local channel with no authentication — do not expose it to anything you do not control.

## License

MIT. See [LICENSE](LICENSE).
