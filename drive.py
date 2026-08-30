"""
Drive the in-game bridge from outside.

Commands: demo, ping, find, read, bench, and probe.

`demo` needs no game and no arguments -- it runs the verification against ten
bridges, most of which lie, and shows which claims survive.

    python drive.py demo

`probe` is the same thing against a real game: it runs a write through the full
verification and prints each step, so the verdict comes with its evidence
rather than on its own.

    python drive.py --data "<game>/.../Mods/GameBridge/data" ping
    python drive.py --data "<game>/.../Mods/GameBridge/data" probe BP_Foo_C --prop Health

See the README for why the verification looks the way it does.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

from verify import Verdict


class FileBridge:
    """Talks to the in-game Lua bridge over the two append-only files."""

    def __init__(self, data_dir: Path, timeout: float = 8.0, poll: float = 0.15):
        self.dir = Path(data_dir)
        self.cmd = self.dir / "cmd.jsonl"
        self.resp = self.dir / "resp.jsonl"
        self.timeout = timeout
        # A game answers on its own tick, so polling faster than this buys
        # nothing against a real bridge. The in-process demo has no tick to
        # wait for, and at 0.15s it spent twelve seconds doing nothing.
        self.poll = poll
        self._id = int(time.time()) % 100000
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cmd.touch(exist_ok=True)
        self.resp.touch(exist_ok=True)
        # Start reading responses from the end, so a previous session's
        # transcript is never mistaken for an answer to this request.
        self._seen = sum(1 for _ in self.resp.open(encoding="utf-8", errors="replace"))

    def send(self, **cmd) -> dict:
        self._id += 1
        cmd["id"] = self._id
        with self.cmd.open("a", encoding="ascii") as f:
            f.write(json.dumps(cmd) + "\n")
        return self._await(self._id)

    def _await(self, want_id: int) -> dict:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            lines = self.resp.open(encoding="utf-8", errors="replace").readlines()
            for line in lines[self._seen:]:
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("id") == want_id:
                    self._seen = len(lines)
                    return obj
            time.sleep(self.poll)
        raise TimeoutError(
            f"no response to id={want_id} within {self.timeout}s. "
            "Is the game running with GameBridge loaded, and is this the right data dir?"
        )

    # verifier-side read: a separate request, never the write's own return value
    def read(self, obj: str, prop: str):
        r = self.send(op="read", obj=obj, prop=prop)
        if not r.get("ok"):
            raise RuntimeError(r.get("err", "read failed"))
        return r["value"]


def near(a: object, x: float) -> bool:
    """Equal, allowing for the quantisation a real property applies.

    The tolerance used to be a flat 1e-6, which convicts honest bridges. Most UE
    float properties are float32: ask for 250.3 and the game stores
    250.3000030518, three times that tolerance away. Both channels then agree
    the value "did not change", and the verdict is FALSE_SUCCESS against a
    bridge that wrote exactly what it was told. Four of seven everyday values
    tested that way -- a systematic false accusation on the tool's main use.

    Relative from here up, absolute near zero. float32 carries about seven
    significant digits, so 1e-6 of the magnitude clears its rounding with room
    while staying far tighter than any real change anyone writes.
    """
    if not isinstance(a, (int, float)) or isinstance(a, bool):
        return False
    return abs(float(a) - x) <= max(1e-6, abs(x) * 1e-6)


def poison_for(value: float, avoid: tuple[float, ...] = ()) -> float | None:
    """Pick a poison that is provably distinguishable from the value it poisons.

    The delta used to be a flat +1234. Two ways that produced a wrong verdict,
    both found by arithmetic rather than by a bridge misbehaving:

    * Above about 1e20, float64 absorbs 1234 entirely: the poison IS the value,
      the check "fails" to notice a change that never happened, and a perfectly
      honest bridge is reported DEAD_CHECK. A false accusation, which is the
      mirror of the false pass this tool exists to prevent and no better.
    * With --restore-original, a property whose original happens to sit exactly
      1234 above the requested value made a correct restore read back as the
      poison, and reported POISON_STUCK against a bridge that did as it was told.

    So the delta scales with magnitude, and any candidate that cannot be told
    apart from the value or from anything in `avoid` is rejected. Returns None
    when no such value exists -- for inf and nan there is nothing to pick, and
    "no poison could be built" is a WITHHELD, never a DEAD_CHECK.
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    v = float(value)
    # Redundant with the loop below -- every candidate built from a nan or an
    # inf is itself nan or inf and gets rejected there, so removing this changes
    # no result. Kept for the short circuit and named as redundant, because a
    # guard nothing can kill should not be left looking load-bearing.
    if v != v or v in (float("inf"), float("-inf")):
        return None

    step = max(1234.0, abs(v) * 1e-6)
    for mult in (1, 8, 64, 512, 4096, 32768):
        for sign in (1.0, -1.0):
            cand = v + sign * step * mult
            if cand != cand or cand in (float("inf"), float("-inf")):
                continue
            if near(cand, v) or any(near(cand, a) for a in avoid):
                continue
            return cand
    return None


def target_for(original: float) -> float:
    """A write value that is provably a change from what is already there.

    A flat +7 is not one. At 1e20 the sum IS the original, so the demo row that
    reads "writes land on a 1e20 property" was verifying a write of the value
    already present -- the label and the evidence disagreed, and nothing said so.
    Same lesson as poison_for, one line further up.
    """
    o = float(original)
    step = max(7.0, abs(o) * 1e-4)
    for mult in (1, 8, 64, 512, 4096):
        cand = o + step * mult
        if not near(cand, o):
            return cand
    return o + step * 32768


def restore_and_check(b: FileBridge, obj: str, prop: str,
                      want: float, poison: float) -> dict:
    """Put the value back and find out whether it went back.

    Four states, never a boolean:

      restored  the value we wrote is what is there
      poisoned  the poison is still there
      diverged  the read WORKED and returned a third value -- something else
                owns this property. The poison is provably out, which
                `unknown` does not say
      unknown   the read did not work, so nothing is known either way

    `diverged` exists because the first version folded it into `unknown`, and
    both callers then printed "the cleanup could not be read back" about a read
    that had succeeded. Four facts in three names is the thing this file spends
    its whole time telling other people not to do.

    Lives here rather than in each caller because the CLI and the MCP tool
    already drifted apart once on logic they had both hand-written.
    """
    out: dict = {"state": "unknown", "wanted": want}
    try:
        w = b.send(op="write", obj=obj, prop=prop, value=want)
        out["write_ok"] = bool(w.get("ok"))
        r = b.send(op="read", obj=obj, prop=prop)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out

    if not r.get("ok"):
        out["error"] = r.get("err", "read failed")
        return out

    out["observed"] = r.get("value")
    if near(r.get("value"), poison):
        out["state"] = "poisoned"
    elif near(r.get("value"), want):
        out["state"] = "restored"
    else:
        out["state"] = "diverged"
    return out


def probe(b: FileBridge, cls: str, prop: str) -> Verdict | None:
    """Run one write through the whole chain, printing the evidence.

    Returns the verdict, or None when the run never reached a write -- "no
    verdict" and "a bad verdict" are different facts, and a caller that
    cannot tell them apart will read a setup failure as a clean bridge.
    """
    print(f"\n[1] ping")
    r = b.send(op="ping")
    print(f"    world={r.get('world')}  travelling={r.get('travelling')}")
    if not r.get("ok"):
        print("    bridge did not answer ok"); return None

    print(f"\n[2] find {cls}")
    r = b.send(op="find", **{"class": cls}, limit=5)
    objs = r.get("objects") or []
    print(f"    found {r.get('count')}: {objs[:3]}")
    if not objs:
        print("    nothing found -- pick a class that exists in the current level")
        return None
    obj = objs[0]

    print(f"\n[3] read {prop}")
    r3 = b.send(op="read", obj=obj, prop=prop)
    if not r3.get("ok"):
        # The MCP tool answers UNREADABLE here. This used to raise, so the same
        # bridge response produced a verdict on one path and a traceback on the
        # other -- the drift that sharing a verdict vocabulary was meant to end.
        print(f"    unreadable: {r3.get('err', 'read failed')}")
        return Verdict.UNREADABLE
    if "value" not in r3:
        # ok:true with no value key -- what the Lua side emits for a property
        # that is currently nil, because jval drops nil-valued keys. A real
        # bridge produces it routinely, and this used to raise KeyError.
        print("    the bridge answered ok but sent no value (property is nil?)")
        return Verdict.UNREADABLE
    original = r3["value"]
    print(f"    {prop} = {original!r}")
    if not isinstance(original, (int, float)):
        print("    need a numeric property for the write test")
        return None

    target = target_for(original)
    print(f"\n[4] write {prop} <- {target}")
    w = b.send(op="write", obj=obj, prop=prop, value=target)
    claimed = bool(w.get("wrote"))
    w_before, w_after = w.get("before"), w.get("after")
    print(f"    bridge claimed: {claimed}   (before={w_before} after={w_after})")

    # The write response carries its own observation, taken at the write site.
    # Treat it as a second, independent channel rather than as decoration: when
    # it disagrees with the re-read below, one of the two is fabricated, and
    # that disagreement is worth more than either reading alone.
    landed_at_write = (
        near(w_after, target)
    )

    print(f"\n[5] postcondition: independent re-read")
    r5 = b.send(op="read", obj=obj, prop=prop)
    observed = r5.get("value") if r5.get("ok") else None
    holds = near(observed, target)
    print(f"    re-read {prop} = {observed!r}  -> {'target' if holds else 'NOT target'}")

    print(f"\n[6] cross-channel agreement")
    print(f"    write-site says landed: {landed_at_write}    re-read says landed: {holds}")
    contradiction = landed_at_write != holds
    if contradiction:
        print("    CONTRADICTION -- the two channels disagree; at least one is lying")

    print(f"\n[7] negative control: prove the check can go red")
    # The poison must be shown to have LANDED before its result means anything.
    # A poison that silently fails to apply makes a dead check look alive, which
    # is how this harness fooled itself on the first run.
    #
    # From here to the restore there is a poison value in a live game, so it
    # runs under try/finally. A timeout while the game hitches, or an object
    # collected mid-probe, would otherwise make the poison write the last thing
    # this tool ever did to that property -- and say nothing about it.
    chosen = poison_for(target, avoid=(float(original),))
    if chosen is None:
        print(f"\n[7] negative control")
        print(f"    no poison can be told apart from {target!r} at this magnitude,")
        print("    so the check was never shown to be capable of failing")
        print("\n" + "=" * 58)
        print("VERDICT WITHHELD: no distinguishable poison exists for this value.")
        print("The write may well have landed; nothing here proves the check could")
        print("have said otherwise.")
        return Verdict.WITHHELD
    poison_val = chosen
    poison_landed = noticed = False
    interrupted: Exception | None = None
    try:
        pw = b.send(op="write", obj=obj, prop=prop, value=poison_val)
        poison_landed = (
            isinstance(pw.get("after"), (int, float))
            and near(pw["after"], poison_val)
        )
        pr = b.send(op="read", obj=obj, prop=prop)
        if not pr.get("ok"):
            raise RuntimeError(pr.get("err", "read failed"))
        poisoned = pr["value"]
        noticed = not near(poisoned, target)
        print(f"    poison landed at write site: {poison_landed}")
        print(f"    re-read after poison: {poisoned!r}; check "
              f"{'went RED' if noticed else 'still passed'}")
        if not poison_landed:
            print("    poison never applied -- the negative control proves nothing here")
    except Exception as e:
        interrupted = e
        print(f"    INTERRUPTED: {type(e).__name__}: {e}")
    finally:
        # `finally`, not a trailing statement. The comment above said try/finally
        # while the code was try/except, and the difference is exactly the case
        # that matters: KeyboardInterrupt is not an Exception, so Ctrl-C during
        # the eight-second poll -- which is precisely what someone does when the
        # game hitches -- skipped the restore entirely and left the poison in a
        # live game, silently. The one thing this tool promises never to do.
        print(f"\n[8] restore")
        cleanup = restore_and_check(b, obj, prop, original, poison_val)
    print(f"    {prop} = {cleanup.get('observed', '<unreadable>')!r}"
          f"   [{cleanup['state']}]"
          + (f"  {cleanup['error']}" if cleanup.get("error") else ""))
    if cleanup["state"] == "poisoned":
        print("    STILL POISONED -- the game is holding the poison value")
    elif cleanup["state"] == "diverged":
        print("    the poison is out, but something else owns this property now")
    elif cleanup["state"] == "unknown":
        print("    cleanup could not be read back; whether the poison is out is unknown")

    if interrupted is not None:
        print("\n" + "=" * 58)
        print("RESTORE UNVERIFIED: verification was interrupted after the poison")
        print(f"was written ({type(interrupted).__name__}). No verdict about the")
        print("write is available, and the cleanup state above is what matters.")
        return Verdict.RESTORE_UNVERIFIED

    print("\n" + "=" * 58)
    # Order matters. The negative control is what licenses trusting a PASS; it is
    # not needed to trust a FAIL. Two independent channels agreeing that nothing
    # changed is already conclusive, so that verdict is reported on its own.
    if not claimed:
        print("HONEST FAILURE: the bridge reported the write did not succeed, and")
        print("did not claim otherwise. Nothing to catch here -- fix the write.")
        return Verdict.HONEST_FAILURE
    if contradiction:
        print("INCONSISTENT BRIDGE: the write site and the re-read disagree about")
        print("whether the value changed. Either a channel is lying, or the property")
        print("is volatile and the game rewrote it between the two reads. Both are")
        print("reasons not to trust a success verdict; only the first is a bug.")
        return Verdict.INCONSISTENT_BRIDGE
    if claimed and not holds:
        print("FALSE SUCCESS caught: the bridge reported a write it did not make.")
        print("Both channels agree the value never changed.")
        return Verdict.FALSE_SUCCESS
    if not poison_landed:
        print("VERDICT WITHHELD: the poison did not apply, so the check was never")
        print("shown to be capable of failing. The apparent pass is unproven.")
        return Verdict.WITHHELD
    if not noticed:
        print("DEAD CHECK: the check survived a poison that provably landed.")
        return Verdict.DEAD_CHECK
    if cleanup["state"] == "poisoned":
        print("POISON STUCK: the check is alive and the write landed, but the")
        print("restore did not take. The game is holding the poison value.")
        return Verdict.POISON_STUCK
    if cleanup["state"] == "diverged":
        print("RESTORE UNVERIFIED: the write and the check both verified and the")
        print("poison is provably out, but the property now holds a third value.")
        print(f"Something else writes it: observed {cleanup.get('observed')!r}.")
        return Verdict.RESTORE_UNVERIFIED
    if cleanup["state"] != "restored":
        print("RESTORE UNVERIFIED: everything verified except the cleanup, which")
        print("could not be read back. An unreadable read is not a clean world --")
        print("treat that property as suspect until you have read it yourself.")
        return Verdict.RESTORE_UNVERIFIED
    print("Chain verified end to end, and the check is provably alive.")
    return Verdict.CONFIRMED


# ---------------------------------------------------------------- demo mode

# Three bridges, one honest and two that lie in different ways, and the verdict
# each one has to earn. This table is the demo's own negative control: if a
# change ever stops the harness catching a liar, the run goes red here rather
# than printing a reassuring transcript. A demo that cannot fail would be an
# odd thing to ship with this particular argument.
SCENARIOS = [
    ("honest bridge",
     dict(lie=False, deadcheck=False, stale_reads=False),
     Verdict.CONFIRMED, "writes land", "-", "Health"),
    ("silent drop",
     dict(lie=True, deadcheck=False, stale_reads=False),
     Verdict.FALSE_SUCCESS, "acks the write, changes nothing",
     "the independent re-read", "Health"),
    ("inconsistent liar",
     dict(lie=True, deadcheck=True, stale_reads=False),
     Verdict.INCONSISTENT_BRIDGE, "fakes the read but not the write site",
     "cross-channel agreement", "Health"),
    ("stale reader",
     dict(lie=False, deadcheck=False, stale_reads=True),
     Verdict.DEAD_CHECK, "both channels agree; reads are cached",
     "the negative control, and nothing else", "Health"),

    # The five below exist because a review found that three of the seven
    # verdict branches had no scenario at all: their guards could rot and this
    # demo would still print a clean table. A demo whose own negative control
    # covers four sevenths of the thing it is demonstrating is not much of one.
    ("honest bridge that fails",
     dict(wrote_false=True),
     Verdict.HONEST_FAILURE, "says plainly that it did not write",
     "reading the field it set", "Health"),
    ("poison refused",
     dict(refuse_nth_write=2),
     Verdict.WITHHELD, "drops the poison write only",
     "requiring the poison to land first", "Health"),
    ("restore refused",
     dict(refuse_nth_write=3),
     Verdict.POISON_STUCK, "verifies fine, then keeps the poison",
     "reading the cleanup back", "Health"),
    ("object vanishes mid-probe",
     dict(vanish_after_write=2),
     Verdict.RESTORE_UNVERIFIED, "stops answering reads after the poison",
     "restoring anyway, then reporting it could not tell", "Health"),
    # Same verdict, different road. RESTORE_UNVERIFIED is reachable two ways --
    # interrupted mid-probe, or completed but with an unreadable cleanup -- and
    # a scenario for one of them leaves the other's guard free to rot.
    ("cleanup unreadable",
     dict(vanish_after_write=3),
     Verdict.RESTORE_UNVERIFIED, "answers everything until the restore",
     "refusing to call an unreadable world clean", "Health"),

    # Not a lying bridge at all -- an honest one, on a value large enough that
    # float64 swallows a flat +1234 poison whole. The harness used to convict
    # it of a DEAD_CHECK: the poison never differed from the value, so of
    # course the check did not notice. A false accusation is the mirror of a
    # false pass, and this repository has no business shipping either.
    ("honest bridge, huge value",
     dict(lie=False, deadcheck=False, stale_reads=False),
     Verdict.CONFIRMED, "writes land on a 1e20 property",
     "scaling the poison to the magnitude", "HugeCounter"),
]


def _load_fake_bridge():
    """Import the simulator by path, so the demo works from any directory."""
    path = Path(__file__).resolve().parent / "test" / "fake_bridge.py"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing; the demo needs the simulator from this repo"
        )
    spec = importlib.util.spec_from_file_location("fake_bridge", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_scenario(fake, flags: dict, verbose: bool,
                  prop: str = "Health") -> tuple[Verdict | None, str]:
    """Serve one fake bridge in a thread and probe it. Returns (verdict, log)."""
    stop, ready = threading.Event(), threading.Event()
    with tempfile.TemporaryDirectory(prefix="ue-live-demo-") as tmp:
        data = Path(tmp) / "data"
        thread = threading.Thread(
            target=fake.run,
            kwargs=dict(data=data, stop=stop, quiet=True, ready=ready, **flags),
            daemon=True,
        )
        thread.start()
        # The simulator skips whatever is in the command file when it starts.
        # Sending before it has taken that mark means the first command is read
        # as a leftover and never answered -- the scenario then times out and
        # the demo reports itself as having failed its own negative control,
        # which is a lie about a lie detector.
        if not ready.wait(timeout=5.0):
            stop.set()
            thread.join(timeout=3.0)
            return None, "the simulator never signalled ready"
        buf = io.StringIO()
        try:
            # A short timeout: nothing here waits on a game, so an eight-second
            # hang would be the simulator being wedged, not slow I/O.
            b = FileBridge(data, timeout=5.0, poll=0.01)
            sink = contextlib.nullcontext() if verbose else contextlib.redirect_stdout(buf)
            with sink:
                verdict = probe(b, "BP_PlayerCharacter_C", prop)
        except TimeoutError as e:
            return None, f"{buf.getvalue()}\nTIMEOUT: {e}"
        finally:
            stop.set()
            # Join before the temp dir is removed: on Windows an open handle in
            # the serving thread makes the cleanup fail, and the failure would
            # land in the middle of the demo output.
            thread.join(timeout=3.0)
    return verdict, buf.getvalue()


def demo(verbose: bool = False) -> int:
    """Run the verification against ten bridges, no game required."""
    try:
        fake = _load_fake_bridge()
    except FileNotFoundError as e:
        print(f"{e}", file=sys.stderr)
        return 2

    print("\nTen bridges. All but one of them report a write that succeeded.\n")
    rows, wrong = [], 0

    for name, flags, expected, blurb, caught_by, prop in SCENARIOS:
        if verbose:
            print("\n" + "-" * 62)
            print(f"-- {name}: {blurb}")
        got, log = _run_scenario(fake, flags, verbose, prop)
        ok = got == expected
        if not ok:
            wrong += 1
            if not verbose:
                print(f"\n-- unexpected result for {name}, full transcript:\n{log}")
        rows.append((name, blurb, got, expected, ok, caught_by))

    w = max(len(r[0]) for r in rows)
    d = max(len(r[1]) for r in rows)
    v = max(len(r[3].value) for r in rows)
    print(f"\n  {'bridge'.ljust(w)}  {'what it does'.ljust(d)}  "
          f"{'verdict'.ljust(v)}  caught by")
    print(f"  {'-' * w}  {'-' * d}  {'-' * v}  {'-' * 34}")
    for name, blurb, got, expected, ok, caught_by in rows:
        shown = (got.value if got is not None else "NO VERDICT").ljust(v)
        tail = caught_by if ok else f"!! expected {expected.value}"
        print(f"  {name.ljust(w)}  {blurb.ljust(d)}  {shown}  {tail}")

    print("\n  Each bridge is stopped by a different layer, and every verdict the")
    print("  harness can reach appears above -- so breaking any one guard turns")
    print("  this table red rather than leaving it quietly wrong. 'stale reader'")
    print("  is the one to look at: both channels agree, every assertion passes,")
    print("  and only poisoning the check on purpose separates it from a real")
    print("  success.\n")

    if wrong:
        print(f"  {wrong} scenario(s) did not produce the expected verdict.")
        print("  That is this demo failing its own negative control.\n")
        return 1
    if not verbose:
        print("  Run with --verbose to see the eight steps behind each verdict.\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="GameBridge data directory (not needed for demo)")
    ap.add_argument("cmd", choices=["demo", "ping", "find", "read", "probe", "bench"])
    ap.add_argument("arg", nargs="?", default="BP_PlayerCharacter_C")
    ap.add_argument("--prop", default="Health")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="demo: print the full step-by-step for every scenario")
    a = ap.parse_args()

    if a.cmd == "demo":
        return demo(a.verbose)

    if not a.data:
        ap.error(f"--data is required for '{a.cmd}' (try 'demo' for a run without a game)")

    b = FileBridge(Path(a.data))
    if a.cmd == "ping":
        print(json.dumps(b.send(op="ping"), indent=1))
    elif a.cmd == "find":
        print(json.dumps(b.send(op="find", **{"class": a.arg}, limit=25), indent=1))
    elif a.cmd == "read":
        print(b.read(a.arg, a.prop))
    elif a.cmd == "bench":
        r = b.send(op="bench", rounds=7, **{"class": a.arg if a.arg != "BP_PlayerCharacter_C" else "Object"})
        print(json.dumps(r, indent=1))
    else:
        verdict = probe(b, a.arg, a.prop)
        if verdict is None:
            return 2          # never reached a write; not a verdict about one
        return 0 if verdict is Verdict.CONFIRMED else 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except TimeoutError as e:
        print(f"\nTIMEOUT: {e}")
        sys.exit(2)
