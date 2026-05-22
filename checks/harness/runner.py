"""CLI runner: discover checks, execute, write results."""
from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
import time
from pathlib import Path

from .base import REGISTRY, Check
from .context import RunContext
from .store import ResultStore

SUITES = [
    "stress",
    "growth",
    "integrity",
    "perf",
    "hardware",
    "network",
    "security",
    "recovery",
    "quality",
    "observability",
]

REPO_ROOT = Path(__file__).resolve().parents[3]  # C:\AIBox
CHECKS_DIR = REPO_ROOT / "aibox" / "checks"


def _safe(s: str) -> str:
    """Strip non-cp1252-encodable characters so we never crash on Windows consoles.

    The full string is preserved in results.db; this only affects terminal output.
    """
    try:
        s.encode(sys.stdout.encoding or "cp1252")
        return s
    except (UnicodeEncodeError, LookupError):
        return s.encode("ascii", "replace").decode("ascii")


def discover() -> None:
    """Import every aibox.checks.<suite>.* module so @register fires."""
    import aibox.checks as root
    for suite in SUITES:
        try:
            pkg = importlib.import_module(f"aibox.checks.{suite}")
        except ModuleNotFoundError:
            continue
        if not hasattr(pkg, "__path__"):
            continue
        for mod_info in pkgutil.iter_modules(pkg.__path__):
            if mod_info.name.startswith("_"):
                continue
            importlib.import_module(f"aibox.checks.{suite}.{mod_info.name}")


def select_checks(args) -> list[type[Check]]:
    discover()
    items = sorted(REGISTRY.values(), key=lambda c: (c.SUITE, c.CHECK_ID))
    if args.id:
        items = [c for c in items if c.CHECK_ID == args.id]
        if not items:
            print(f"no check matches id={args.id}", file=sys.stderr)
            sys.exit(2)
    elif args.suite and args.suite != "all":
        items = [c for c in items if c.SUITE == args.suite]
    return items


def print_status() -> None:
    discover()
    real = stub = 0
    print(f"{'ID':<6}  {'SUITE':<14}  {'STATUS':<6}  NAME")
    print("-" * 72)
    for cls in sorted(REGISTRY.values(), key=lambda c: (c.SUITE, c.CHECK_ID)):
        flag = "DESTR" if cls.DESTRUCTIVE else ""
        print(f"{cls.CHECK_ID:<6}  {cls.SUITE:<14}  {cls.STATUS:<6}  {cls.NAME} {flag}")
        if cls.STATUS == "real":
            real += 1
        else:
            stub += 1
    print("-" * 72)
    print(f"  total: {real + stub}    real: {real}    stub: {stub}")


def run(args) -> int:
    classes = select_checks(args)
    if not classes:
        print("no checks selected", file=sys.stderr)
        return 1
    store = ResultStore(CHECKS_DIR / "results.db")
    ctx = RunContext.begin(
        repo_root=REPO_ROOT,
        store=store,
        invocation=" ".join(sys.argv[1:]) or "(no args)",
        dry_run=args.dry_run,
        quick=args.quick,
        full=args.full,
        soak=args.soak,
        i_mean_it=args.i_mean_it,
        unattended=args.unattended,
        notes=args.notes or "",
    )
    print(f"[run #{ctx.run_id}] {len(classes)} checks  (dry-run={args.dry_run})")
    print(f"  host={ctx.host_summary()}")
    print(f"  gpu={ctx._git_sha if False else ''}")
    summary_counts = {"ok": 0, "stub": 0, "skipped": 0, "fail": 0, "error": 0, "dry-run": 0}
    t0 = time.time()
    for cls in classes:
        inst = cls()
        check_db_id = store.start_check(
            ctx.run_id, cls.SUITE, cls.CHECK_ID, cls.NAME, cls.STATUS,
        )
        ctx.current_check_db_id = check_db_id
        ctx.current_suite = cls.SUITE
        ctx.current_check_id = cls.CHECK_ID
        t_check = time.time()
        res = inst.execute(ctx)
        # If the check returned metrics, persist them.
        for name, value in (res.metrics or {}).items():
            ctx.metric(name, value)
        store.end_check(check_db_id, res.outcome, res.error)
        summary_counts[res.outcome] = summary_counts.get(res.outcome, 0) + 1
        elapsed = time.time() - t_check
        marker = {
            "ok": "+", "stub": ".", "skipped": "-",
            "fail": "X", "error": "!", "dry-run": "?",
        }.get(res.outcome, "?")
        line = (f"  {marker} [{cls.CHECK_ID:<5}] {cls.SUITE:<13} {cls.NAME:<40} "
                f"{res.outcome:<8} {elapsed:5.2f}s  {res.summary[:80]}")
        print(_safe(line))
        if res.error:
            print(_safe(f"      ! {res.error.splitlines()[0]}"))
    ctx.end()
    dt = time.time() - t0
    print(f"\nrun #{ctx.run_id} finished in {dt:.1f}s")
    print("  " + "  ".join(f"{k}={v}" for k, v in summary_counts.items() if v))
    print(f"  results: {CHECKS_DIR / 'results.db'}")
    return 0 if summary_counts.get("fail", 0) == 0 and summary_counts.get("error", 0) == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="aibox.checks.harness.runner")
    p.add_argument("target", nargs="?", default="all",
                   help="'all' or a suite name (alias for --suite)")
    p.add_argument("--suite", choices=["all", *SUITES], help="run a single suite")
    p.add_argument("--id", help="run a single check by ID (e.g. 6.1)")
    p.add_argument("--dry-run", action="store_true",
                   help="don't actually run checks, just print what would happen")
    p.add_argument("--quick", action="store_true", help="faster, less thorough variants")
    p.add_argument("--full", action="store_true", help="full variants (longer)")
    p.add_argument("--soak", action="store_true", help="enable soak-test mode (24h+ runs)")
    p.add_argument("--i-mean-it", action="store_true",
                   help="permit destructive checks (kill containers, fill disk, etc.)")
    p.add_argument("--unattended", action="store_true",
                   help="no interactive prompts; fail-closed instead")
    p.add_argument("--notes", help="free-text notes attached to this run")
    p.add_argument("--status", action="store_true",
                   help="print the real/stub matrix and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.status:
        print_status()
        return 0
    if not args.suite:
        args.suite = args.target
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
