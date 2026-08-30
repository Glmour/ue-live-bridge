# ue-live-bridge

**Every check you rely on could be dead. This one poisons itself to prove it isn't — demonstrated against a commercial game, because a running game is a state you cannot fake.**

Automated callers report success they did not achieve. Measurably: **45–48%** of agent failures close with the agent asserting it finished, **75.8%** of explicit success flags written by coding agents were wrong, and LLM judges catch neither — no configuration across five judges exceeded AUROC 0.65 ([arXiv:2606.09863](https://arxiv.org/abs/2606.09863)).

The standard fix is to assert a postcondition against the authoritative record, from a process the caller cannot write to. That is right, and it is not enough: **a postcondition that can never fail is indistinguishable from one that passes.** Nobody checks the checker.

So this harness poisons every check on purpose and requires it to go red. A check that survives its own poison is reported as a harness failure, not as a passing test.

## Thirty seconds, no game required

```bash
git clone https://github.com/Glmour/ue-live-bridge && cd ue-live-bridge
python drive.py demo
```

```
Ten bridges. All but one of them report a write that succeeded.


  bridge                     what it does                            verdict              caught by
  -------------------------  --------------------------------------  -------------------  ----------------------------------
  honest bridge              writes land                             CONFIRMED            -
  silent drop                acks the write, changes nothing         FALSE_SUCCESS        the independent re-read
  inconsistent liar          fakes the read but not the write site   INCONSISTENT_BRIDGE  cross-channel agreement
  stale reader               both channels agree; reads are cached   DEAD_CHECK           the negative control, and nothing else
  honest bridge that fails   says plainly that it did not write      HONEST_FAILURE       reading the field it set
  poison refused             drops the poison write only             WITHHELD             requiring the poison to land first
  restore refused            verifies fine, then keeps the poison    POISON_STUCK         reading the cleanup back
  object vanishes mid-probe  stops answering reads after the poison  RESTORE_UNVERIFIED   restoring anyway, then reporting it could not tell
  cleanup unreadable         answers everything until the restore    RESTORE_UNVERIFIED   refusing to call an unreadable world clean
  honest bridge, huge value  writes land on a 1e20 property          CONFIRMED            scaling the poison to the magnitude
```

Standard library only, nothing to install, about two seconds. Each bridge is stopped by a different layer, and **`stale reader` is the one to look at**: both channels agree, every assertion passes, and only poisoning the check on purpose exposes it.

The demo is also its own negative control. It asserts the expected verdict for every row, and every verdict the harness can produce appears in the table — so disabling any one guard turns this red rather than leaving it quietly wrong. Break the poison step, for instance, and the stale reader collects a confident pass:

```
  stale reader       both channels agree; reads are cached  CONFIRMED            !! expected DEAD_CHECK

  1 scenario(s) did not produce the expected verdict.
  That is this demo failing its own negative control.
```

That property is checked rather than claimed: disabling each guard in turn — poison-landed, poison-noticed, cross-channel, false-success, honest-failure, poison-stuck, restore-unverified, interrupted-probe, the poison chooser — turns the demo or `test/poison_test.py` red. It did not always. A review found that three branches had no scenario at all and could have rotted in silence, which is how the last four rows got here.

`--verbose` prints the eight steps behind each verdict.

## The same harness against a running game

A [UE4SS](https://github.com/UE4SS-RE/RE-UE4SS) Lua mod exposes the live game over a pair of append-only files. A Python driver on the other side reads and writes UObject properties, calls UFunctions, and — the part that matters — refuses to report success it cannot prove. It speaks MCP, so an agent can drive the game directly, and the write tool returns a verdict rather than a boolean.

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

That is a real transcript against The Exit 8 (UE 5.2), not a mock.

**Why demonstrate this on a game?** Verification claims are usually shown on synthetic benchmarks, where the authoritative state is whatever the benchmark says it is. A running commercial game is adversarial in the way that matters: you do not own the state, you cannot fake it, it recycles object slots underneath you, and it will happily report that a write landed when it did not. Every failure mode in the demo above was met here first.

---

## Why steps 6 and 7 exist

The figures in the opening come from [*From Confident Closing to Silent Failure*](https://arxiv.org/abs/2606.09863), across 9,876 tau2-bench and 1,879 AppWorld trajectories. The same paper carries the number that shaped this tool: in its one dual-control domain, where a second party independently confirms the state change, false success drops to **3%**. A 15x difference from architecture rather than from a better model.

Step 5 is that second party — a postcondition read through a handle the actor does not write to.

Step 7 is the part nobody does. Dead checks are ordinary: a comparison lost in a refactor, an exception swallowed into a `True`, a field name that silently resolves to something else. A guard that cannot fail looks exactly like a guard that passes, so step 7 poisons the world and requires the check to notice.

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

The CLI and the MCP tool report the same seven verification outcomes.

| MCP `verdict` | CLI line | Meaning | Exit |
|---|---|---|---|
| `CONFIRMED` | Chain verified end to end | Both channels agree, and the check was proven capable of failing | 0 |
| `FALSE_SUCCESS` | FALSE SUCCESS caught | The bridge claimed a write it did not make; both channels agree | 1 |
| `INCONSISTENT_BRIDGE` | INCONSISTENT BRIDGE | Write site and re-read disagree; no verdict is available | 1 |
| `WITHHELD` | VERDICT WITHHELD | The poison never applied, so the apparent pass is unproven | 1 |
| `DEAD_CHECK` | DEAD CHECK | The check survived a poison that provably landed | 1 |
| `POISON_STUCK` | POISON STUCK | Everything verified, but the restore did not take and the game is still holding the poison | 1 |
| `RESTORE_UNVERIFIED` | RESTORE UNVERIFIED | The cleanup could not be read back, so whether the poison is out is unknown | 1 |

The MCP tool adds two that come before verification can even start: `UNREADABLE`
(the property could not be read) and `WRITE_REJECTED` (the bridge refused the write),
plus `HONEST FAILURE` on the CLI when the bridge reports the write failed and does not
pretend otherwise.

Only the first is a success. The rest are distinct facts, and collapsing them
into "failed" throws away the part that tells you what to do next. `POISON_STUCK`
in particular is not a verification result at all — it is the harness reporting
that it left a mess, which is the one thing worse than a wrong verdict.

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
python drive.py --data "<game>/.../Mods/GameBridge/data" bench    # what a cache miss costs here
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

**Verification is not free, and it is not read-only.** Proving the check can fail means
actually making it fail, so one verified write is four writes to the live game: the target
value, a poison value (`target + 1234`), and the final value — with the poison visible in
the game for roughly half a second while the bridge round-trips. That is fine for a config
field and not fine for health, position, or anything the game consumes every tick. Read the
property first and decide whether the excursion is acceptable before you write it.

## Tests

The verification logic runs off-engine, against a simulated game that can be told to lie:

```bash
python drive.py demo                                   # ten bridges end to end, no game
python test/poison_test.py                             # the poison chooser, on its own
python test/spike_test.py                              # verification logic in isolation
python test/mcp_test.py                                # the same four bridges through MCP
python test/lua_syntax.py --self-test                  # the syntax checker, on a broken file
python test/fake_bridge.py --data ./_t --lie           # then: python drive.py --data ./_t probe
```

`fake_bridge.py` reproduces every failure mode on demand: `--lie` drops writes
silently, `--deadcheck` additionally fakes the read-back, `--stale-reads` serves
both channels a consistent story from a cache — the only one of those that the
negative control alone can catch — and `--wrote-false`, `--refuse-nth-write N`
and `--vanish-after-write N` reach the branches for an honest failure, a poison
that never lands, a restore that does not take, and a cleanup that cannot be
read. The first two are how the design flaw below was found; the rest exist
because a review pointed out that three verdicts had nothing exercising them.

Every check in this repository is required to fail on demand, including the ones
that check the checks. `lua_syntax.py --self-test` feeds itself deliberately
broken Lua and fails if it does not go red — it was written because the previous
syntax check reported `OK` for every file, good or broken, and had been doing so
for an entire session.


## What a cache miss actually costs

`bench` searches for a name that cannot exist, which forces the complete worst-case sweep.

| Game | State | UObjects | `FindAllOf("Object")` | Full scan | Per object |
|---|---|---|---|---|---|
| The Exit 8 (one corridor) | **in level** | 23,919 | 11 ms | 29.6 ms | 1.24 µs |
| Escape the Backrooms (26 GB) | **main menu only** | 29,784 | 13 ms | 37.6 ms | 1.26 µs |

Two things fall out of that, and the second is the one that changed the design.

**Object count is dominated by the engine and loaded assets, not by world size.** A 26 GB game sitting on its *main menu*, with zero Characters spawned, carries 25% more UObjects than another title's fully loaded playable level. Do not reason about this cost from how big the map looks.

**The cost is linear at ~1.25 µs per object, on the game thread.** That is 30 ms where it is cheapest, and it scales from there — roughly 125 ms at 100k objects, a quarter of a second at 200k. Every millisecond of it is a frame hitch.

So the scan is off by default. Spending a visible stall is a decision the caller should make deliberately, not something the resolver does behind their back because a name was not in a table.

*Caveat, since it changes what the second row proves: Escape the Backrooms was measured at its main menu, not in a level. It bounds the engine's baseline, not a loaded open world. The per-object figure is the number that transfers.*

## Design notes

Each of these was paid for:

- **The command file is never rewound.** A line cursor is set to the file's current length at load, so commands left over from a previous session are not replayed. Without this, whoever starts the game executes the leftovers — a stale write lands minutes after anyone asked for it, and the symptom looks nothing like the cause.
- **`resolve` caches, re-validates on every hit, and refuses to scan by default.** The engine recycles object slots, so a pointer that still passes `IsValid()` can belong to something else; the cached name is checked again before use. `find` populates the cache from objects it is already holding, so the normal find-then-act flow does no scanning at all — measured at **7 resolve hits, 0 full scans** across a complete probe run. A cache miss returns an error telling you to call `find` first, rather than silently spending a frame hitch; `allow_scan=1` opts in. See the measurement below for why.
- **The cache is dropped when the world changes, and the guard has a negative control.**
  Objects do not survive a level change, and a stale entry that happened to re-validate
  would hand back an object from the old world. The guard compares the world's full name
  against the previous tick, because the obvious check does not work: `IsInSeamlessTravel`
  is a plain C++ method, not a UFUNCTION, so reflection cannot reach it. Read as a
  property it resolves to a userdata, and `userdata == true` is false forever — a guard
  that could never fire, in a repository whose entire argument is that such a guard is
  not a guard. `ping` now reports `travel_guard_alive`, which poisons the tracked world
  name on every status call and requires the guard to trip.
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

- **A genuinely large loaded world.** Both measurements above are bounded: one small game in a level, one large game at a menu. The linear per-object cost should hold, but no open world with a fully streamed-in level has been measured.
- **Long sessions.** The longest run here is minutes, not hours.
- **Level transitions.** The cache is dropped on travel by construction, but the behaviour has not been exercised across an actual transition.
- **Engines other than 5.2.**

## Scope

This drives games you own, for modding and tooling. It is not an anti-cheat bypass, it does not touch competitive multiplayer, and the bridge is deliberately an open local channel with no authentication — do not expose it to anything you do not control.

## License

MIT. See [LICENSE](LICENSE).
