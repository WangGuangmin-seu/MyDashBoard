"""Pipeline entry point (spec §9.1): fetch → store → snapshot → validate →
alert → notify. Resilient by construction — one collector failing never aborts
the others or the downstream steps; the failure becomes a heartbeat alert.

Usage:
    python -m dashboard                 # full run
    python -m dashboard --no-notify     # run without sending Feishu cards
    python -m dashboard export-schema   # (re)write data/schema.json (frozen contract)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .collectors.registry import ALL_COLLECTORS, all_series_meta
from .contract import CollectorContext, Observation
from .pipeline.alerts import (
    AlertState,
    default_state_path,
    evaluate,
    load_rules,
)
from .pipeline.snapshot import (
    CollectorHealth,
    build_snapshot,
    export_schema,
    validate_snapshot_file,
    write_snapshot,
)
from .pipeline.store import Store

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = ROOT / "rules" / "alerts.yaml"
# The static site is served from web/; the frontend fetches ./data/snapshot.json,
# so the snapshot is written to web/data/. Canonical series/meta/schema live in
# the root data/ dir (git history is the audit trail, spec §5.4).
DEFAULT_WEB_DATA = ROOT / "web" / "data"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader for local runs (CI provides real env vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _collect_secrets() -> dict[str, str]:
    keys = ("EIA_API_KEY", "FEISHU_WEBHOOK_URL", "FEISHU_SECRET")
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


async def _run_collectors(
    ctx: CollectorContext,
) -> tuple[list[Observation], list[CollectorHealth]]:
    results = await asyncio.gather(
        *(c.fetch(ctx) for c in ALL_COLLECTORS), return_exceptions=True
    )
    observations: list[Observation] = []
    health: list[CollectorHealth] = []
    for collector, res in zip(ALL_COLLECTORS, results):
        if isinstance(res, Exception):
            health.append(CollectorHealth(id=collector.id, ok=False, error=f"{type(res).__name__}: {res}"))
            print(f"[collector] {collector.id} FAILED: {res}", file=sys.stderr)
        else:
            observations.extend(res)
            health.append(CollectorHealth(id=collector.id, ok=True))
            print(f"[collector] {collector.id} ok: {len(res)} observations")
    return observations, health


async def cmd_run(args: argparse.Namespace) -> int:
    data_dir = Path(args.data_dir)
    _load_dotenv(ROOT / ".env")
    now = datetime.now(timezone.utc)
    ctx = CollectorContext(now=now, secrets=_collect_secrets())

    # 1) fetch (resilient) ------------------------------------------------
    observations, health = await _run_collectors(ctx)

    # 2) store (append-only) ---------------------------------------------
    store = Store(data_dir)
    written = store.append_observations(observations)
    store.write_meta(all_series_meta())
    print(f"[store] wrote {sum(written.values())} new records across {len(written)} series")

    # 3) snapshot (written to the served web/data dir) --------------------
    snapshot = build_snapshot(store, all_series_meta(), now, health, window=args.window)
    web_data = Path(args.web_data)
    snap_path = write_snapshot(snapshot, web_data)
    print(f"[snapshot] {snap_path}")

    # 4) contract guard — validate snapshot against frozen schema (spec §2.3).
    #    A mismatch is a HARD failure (exit 2): the deploy must be blocked so a
    #    renamed backend field can't silently blank the frontend (spec §12.9).
    schema_path = data_dir / "schema.json"
    if not schema_path.exists():
        export_schema(data_dir)
        print(f"[schema] created {schema_path} (first run)")
    try:
        validate_snapshot_file(snap_path, schema_path)
    except Exception as exc:  # noqa: BLE001
        print(f"[schema] CONTRACT VIOLATION — deploy blocked: {exc}", file=sys.stderr)
        return 2
    print("[schema] snapshot conforms to frozen contract")

    # 5) alerts -----------------------------------------------------------
    exit_code = 0
    if args.rules and Path(args.rules).exists():
        rules = load_rules(args.rules)
        state = AlertState(default_state_path(data_dir))
        alerts = evaluate(snapshot, rules, state, now)
        print(f"[alerts] {len(alerts)} alert(s) to send")
        if alerts and not args.no_notify:
            exit_code = await _notify(alerts)
        state.save()

    return exit_code


async def _notify(alerts) -> int:
    webhook = os.environ.get("FEISHU_WEBHOOK_URL")
    if not webhook:
        print("[notify] FEISHU_WEBHOOK_URL not set — skipping send", file=sys.stderr)
        return 0
    from .notify.feishu import FeishuNotifier

    notifier = FeishuNotifier(webhook, os.environ.get("FEISHU_SECRET"))
    results = await notifier.send_all(alerts)
    failures = [(a, e) for a, e in results if e is not None]
    for a, e in failures:
        print(f"[notify] FAILED {a.rule_id}: {e}", file=sys.stderr)
    return 1 if failures else 0


async def cmd_test_notify(args: argparse.Namespace) -> int:
    """Send one red + one grey sample card to verify Feishu wiring (spec §7.3)."""
    _load_dotenv(ROOT / ".env")
    webhook = os.environ.get("FEISHU_WEBHOOK_URL")
    if not webhook:
        print("FEISHU_WEBHOOK_URL not set (put it in .env or the environment)", file=sys.stderr)
        return 1
    from .contract import DataStatus
    from .notify.feishu import FeishuNotifier
    from .pipeline.alerts import Alert

    samples = [
        Alert(rule_id="test_threshold", kind="threshold", template="red", severity="critical",
              series_id="demo.series", message="测试红卡：数值突破阈值 42（阈值 20）",
              value=42.0, status=DataStatus.CONFIRMED),
        Alert(rule_id="test_heartbeat", kind="heartbeat", template="grey", severity="critical",
              series_id="demo.series", message="测试灰卡：采集器心跳失败"),
    ]
    notifier = FeishuNotifier(webhook, os.environ.get("FEISHU_SECRET"))
    results = await notifier.send_all(samples)
    ok = True
    for a, e in results:
        if e:
            ok = False
            print(f"[test-notify] FAILED {a.template}: {e}", file=sys.stderr)
        else:
            print(f"[test-notify] sent {a.template} card OK")
    return 0 if ok else 1


def cmd_export_schema(args: argparse.Namespace) -> int:
    path = export_schema(Path(args.data_dir))
    print(f"[schema] wrote {path}")
    return 0


_COMMANDS = {"run", "export-schema", "test-notify"}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data-dir", default=str(ROOT / "data"))

    p = argparse.ArgumentParser(prog="dashboard", parents=[common])
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="full pipeline (default)")
    run.add_argument("--rules", default=str(DEFAULT_RULES))
    run.add_argument("--web-data", dest="web_data", default=str(DEFAULT_WEB_DATA))
    run.add_argument("--window", type=int, default=180)
    run.add_argument("--no-notify", action="store_true")
    run.set_defaults(func=cmd_run)

    exp = sub.add_parser("export-schema", parents=[common], help="(re)write frozen data/schema.json")
    exp.set_defaults(func=cmd_export_schema)

    tn = sub.add_parser("test-notify", parents=[common], help="send sample Feishu cards")
    tn.set_defaults(func=cmd_test_notify)
    return p


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to GBK; force UTF-8 so Chinese paths/messages and
    # any non-CJK glyphs print without UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default to the `run` subcommand when none is given.
    if not any(tok in _COMMANDS for tok in argv):
        argv = ["run", *argv]
    args = build_parser().parse_args(argv)
    func = args.func
    if asyncio.iscoroutinefunction(func):
        return asyncio.run(func(args))
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
