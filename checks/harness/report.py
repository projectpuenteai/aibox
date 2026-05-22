"""Generate HTML + Markdown reports for a check run."""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

from .store import ResultStore

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKS_DIR = REPO_ROOT / "aibox" / "checks"


def render_markdown(summary: dict) -> str:
    run = summary["run"]
    started = datetime.fromtimestamp(run.get("started_at", 0)).isoformat(timespec="seconds")
    lines = [
        f"# Check run #{run.get('id')} — {started}",
        "",
        f"- host: `{run.get('host')}`",
        f"- git sha: `{run.get('git_sha')}`",
        f"- env hash: `{run.get('env_hash')}`",
        f"- gpu: `{run.get('gpu_info')}`",
        f"- invocation: `{run.get('invocation')}`",
        "",
        "## Checks",
        "",
        "| ID | Suite | Name | Status | Outcome | Duration (s) |",
        "|----|-------|------|--------|---------|--------------|",
    ]
    for c in summary["checks"]:
        dur = c.get("duration")
        dur_s = f"{dur:.2f}" if isinstance(dur, (int, float)) else ""
        lines.append(
            f"| {c['check_id']} | {c['suite']} | {c['name']} | {c['status']} "
            f"| {c['outcome']} | {dur_s} |"
        )
    lines.append("")
    lines.append("## Metrics")
    lines.append("")
    if not summary["metrics"]:
        lines.append("_no metrics recorded_")
    else:
        lines.append("| Check | Metric | Value | Unit |")
        lines.append("|-------|--------|-------|------|")
        for m in summary["metrics"]:
            val = m["value"] if m["value"] is not None else m["value_text"]
            lines.append(f"| {m['check_id']} | {m['name']} | {val} | {m['unit']} |")
    return "\n".join(lines) + "\n"


def render_html(summary: dict) -> str:
    run = summary["run"]
    started = datetime.fromtimestamp(run.get("started_at", 0)).isoformat(timespec="seconds")
    rows_checks = "\n".join(
        f"<tr class='out-{html.escape(str(c['outcome']))}'>"
        f"<td>{html.escape(str(c['check_id']))}</td>"
        f"<td>{html.escape(str(c['suite']))}</td>"
        f"<td>{html.escape(str(c['name']))}</td>"
        f"<td>{html.escape(str(c['status']))}</td>"
        f"<td>{html.escape(str(c['outcome']))}</td>"
        f"<td>{('%.2f' % c['duration']) if isinstance(c.get('duration'), (int, float)) else ''}</td>"
        f"<td>{html.escape(str(c.get('error') or ''))}</td>"
        f"</tr>"
        for c in summary["checks"]
    )
    rows_metrics = "\n".join(
        f"<tr><td>{html.escape(str(m['check_id']))}</td>"
        f"<td>{html.escape(str(m['name']))}</td>"
        f"<td>{html.escape(str(m['value']) if m['value'] is not None else m['value_text'] or '')}</td>"
        f"<td>{html.escape(str(m['unit']))}</td>"
        f"<td>{html.escape(str(m.get('tags') or ''))}</td></tr>"
        for m in summary["metrics"]
    )
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Check run #{run.get('id')}</title>
<style>
 body{{font-family:system-ui,Segoe UI,Arial;margin:24px;color:#222;}}
 h1{{margin-top:0}}
 table{{border-collapse:collapse;margin:12px 0;width:100%;}}
 th,td{{border:1px solid #ccc;padding:4px 8px;font-size:13px;text-align:left;}}
 th{{background:#f4f4f4;}}
 tr.out-ok td{{background:#eaffea;}}
 tr.out-stub td{{background:#f4f4ff;}}
 tr.out-skipped td{{background:#fafafa;color:#888;}}
 tr.out-fail td{{background:#ffecec;}}
 tr.out-error td{{background:#ffd6d6;}}
 tr.out-dry-run td{{background:#fff8e1;}}
 .meta{{font-family:Consolas,monospace;font-size:12px;color:#555;}}
</style></head><body>
<h1>Check run #{run.get('id')}</h1>
<div class="meta">
 host: {html.escape(str(run.get('host')))} ·
 git: {html.escape(str(run.get('git_sha')))} ·
 env: {html.escape(str(run.get('env_hash')))} ·
 gpu: {html.escape(str(run.get('gpu_info')))}<br>
 started: {started} ·
 invocation: <code>{html.escape(str(run.get('invocation')))}</code>
</div>
<h2>Checks</h2>
<table><thead><tr>
 <th>ID</th><th>Suite</th><th>Name</th><th>Status</th><th>Outcome</th><th>Duration (s)</th><th>Error</th>
</tr></thead><tbody>
{rows_checks}
</tbody></table>
<h2>Metrics</h2>
<table><thead><tr>
 <th>Check</th><th>Metric</th><th>Value</th><th>Unit</th><th>Tags</th>
</tr></thead><tbody>
{rows_metrics or '<tr><td colspan=5><em>no metrics</em></td></tr>'}
</tbody></table>
</body></html>
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="aibox.checks.harness.report")
    p.add_argument("--run-id", type=int, help="specific run id")
    p.add_argument("--latest", action="store_true", help="report on the most recent run")
    p.add_argument("--format", choices=["html", "md", "both"], default="both")
    p.add_argument("--out", help="output directory (default: aibox/checks/reports/<runid>)")
    args = p.parse_args(argv)
    store = ResultStore(CHECKS_DIR / "results.db")
    run_id = args.run_id or store.latest_run_id()
    if not run_id:
        print("no runs in result store", file=sys.stderr)
        return 1
    summary = store.run_summary(run_id)
    out_dir = Path(args.out) if args.out else CHECKS_DIR / "reports" / f"run_{run_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.format in ("md", "both"):
        (out_dir / "report.md").write_text(render_markdown(summary), encoding="utf-8")
    if args.format in ("html", "both"):
        (out_dir / "index.html").write_text(render_html(summary), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    print(f"wrote report to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
