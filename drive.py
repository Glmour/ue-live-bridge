"""
Drive the in-game bridge from outside.

Commands: demo, ping, find, read, bench, and probe.

`demo` needs no game and no arguments -- it runs the verification against three
bridges, one honest and two that lie, and shows which claims survive.

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

    def __init__(self, data_dir: Path, timeout: float = 8.0):
        self.dir = Path(data_dir)
        self.cmd = self.dir / "cmd.jsonl"
        self.resp = self.dir / "resp.jsonl"
        self.timeout = timeout
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
            time.sleep(0.15)
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
    original = b.read(obj, prop)
    print(f"    {prop} = {original!r}")
    if not isinstance(original, (int, float)):
        print("    need a numeric property for the write test")
        return None

    target = float(original) + 7.0
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
        isinstance(w_after, (int, float)) and abs(float(w_after) - target) < 1e-6
    )

    print(f"\n[5] postcondition: independent re-read")
    observed = b.read(obj, prop)
    holds = abs(float(observed) - target) < 1e-6
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
    poison_val = float(target) + 1234.0
    pw = b.send(op="write", obj=obj, prop=prop, value=poison_val)
    poison_landed = (
        isinstance(pw.get("after"), (int, float))
        and abs(float(pw["after"]) - poison_val) < 1e-6
    )
    poisoned = b.read(obj, prop)
    noticed = abs(float(poisoned) - target) >= 1e-6
    print(f"    poison landed at write site: {poison_landed}")
    print(f"    re-read after poison: {poisoned!r}; check "
          f"{'went RED' if noticed else 'still passed'}")
    if not poison_landed:
        print("    poison never applied -- the negative control proves nothing here")

    print(f"\n[8] restore")
    b.send(op="write", obj=obj, prop=prop, value=original)
    restored = b.read(obj, prop)
    # The negative control put a poison value into a live game. Leaving it there
    # is worse than any verdict this function can return, so the restore is
    # checked rather than assumed -- an unchecked cleanup is a dead check too.
    still_poisoned = (
        poison_landed
        and isinstance(restored, (int, float))
        and abs(float(restored) - poison_val) < 1e-6
    )
    print(f"    {prop} = {restored!r}"
          + ("   <-- STILL POISONED" if still_poisoned else ""))

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
    if still_poisoned:
        print("POISON STUCK: the check is alive and the write landed, but the")
        print("restore did not take. The game is holding the poison value.")
        return Verdict.POISON_STUCK
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
     Verdict.CONFIRMED, "writes land", "-"),
    ("silent drop",
     dict(lie=True, deadcheck=False, stale_reads=False),
     Verdict.FALSE_SUCCESS, "acks the write, changes nothing",
     "the independent re-read"),
    ("inconsistent liar",
     dict(lie=True, deadcheck=True, stale_reads=False),
     Verdict.INCONSISTENT_BRIDGE, "fakes the read but not the write site",
     "cross-channel agreement"),
    ("stale reader",
     dict(lie=False, deadcheck=False, stale_reads=True),
     Verdict.DEAD_CHECK, "both channels agree; reads are cached",
     "the negative control, and nothing else"),
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


def _run_scenario(fake, flags: dict, verbose: bool) -> tuple[Verdict | None, str]:
    """Serve one fake bridge in a thread and probe it. Returns (verdict, log)."""
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix="ue-live-demo-") as tmp:
        data = Path(tmp) / "data"
        thread = threading.Thread(
            target=fake.run,
            kwargs=dict(data=data, stop=stop, quiet=True, **flags),
            daemon=True,
        )
        thread.start()
        buf = io.StringIO()
        try:
            # A short timeout: nothing here waits on a game, so an eight-second
            # hang would be the simulator being wedged, not slow I/O.
            b = FileBridge(data, timeout=5.0)
            sink = contextlib.nullcontext() if verbose else contextlib.redirect_stdout(buf)
            with sink:
                verdict = probe(b, "BP_PlayerCharacter_C", "Health")
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
    """Run the verification against four bridges, no game required."""
    try:
        fake = _load_fake_bridge()
    except FileNotFoundError as e:
        print(f"{e}", file=sys.stderr)
        return 2

    print("\nFour bridges. Every one of them reports that the write succeeded.\n")
    rows, wrong = [], 0

    for name, flags, expected, blurb, caught_by in SCENARIOS:
        if verbose:
            print("\n" + "-" * 62)
            print(f"-- {name}: {blurb}")
        got, log = _run_scenario(fake, flags, verbose)
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

    print("\n  Every row after the first claimed a success it did not earn, and")
    print("  each one is stopped by a different layer. The last is the point:")
    print("  both channels agree, every assertion passes, and the only thing")
    print("  separating it from a real success is poisoning the check on")
    print("  purpose and requiring it to go red.\n")

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
