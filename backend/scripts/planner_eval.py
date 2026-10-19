"""Compare planner quality before and after the planner v2 changes, from two SQLite databases. Read-only.

Run from the backend folder:
    python -m scripts.planner_eval --pair "Seeded scenarios=before.db=after.db" [--pair ...] [--write ../docs/planner_report.md]

Works on databases from before the ``source`` column existed (every plan there was raw LLM output).
Metrics (all per database):
- valid plan rate per sample: raw LLM output, and after the prune step;
- share of alerts with at least one valid plan: LLM only, LLM + prune, LLM + prune + rule-based baseline;
- duplicate rate and hallucination rate (non-existent drivers / sessions / trainers), both on raw LLM output only;
- best LLM plan score against the baseline's score, per alert.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

SECTION = "## Before vs after (planner v2)"


def load(path: str, min_run_id: int = 0) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("pragma table_info(plans)")}
    has_source = "source" in cols
    has_score = "score_total" in cols or "score" in cols
    score_col = "score_total" if "score_total" in cols else ("score" if "score" in cols else "NULL")
    rows = con.execute(
        f"select id, alert_id, run_id, outcome, is_valid, is_duplicate, fabricated_ids, {score_col} as score, "
        f"{'source' if has_source else repr('llm')} as source from plans where run_id is not null"
    ).fetchall()
    rows = [r for r in rows if r["run_id"] >= min_run_id]
    runs = [r for r in con.execute("select id, alert_id, n_requested, model, prompt_version from planner_runs order by id") if r["id"] >= min_run_id]
    alerts = {r["id"]: r for r in con.execute("select a.id, c.code, a.shortfall_type from alerts a join courses c on c.id = a.course_id")}
    con.close()
    return {"rows": rows, "runs": runs, "alerts": alerts, "has_source": has_source, "has_score": has_score}


def metrics(data: dict) -> dict:
    rows = data["rows"]
    llm = [r for r in rows if r["source"] == "llm"]
    pruned = [r for r in rows if r["source"] == "llm_pruned" and r["outcome"] == "valid"]
    baseline = [r for r in rows if r["source"] == "greedy_baseline" and r["outcome"] == "valid"]
    raw_valid = [r for r in llm if r["outcome"] == "valid" and not r["is_duplicate"]]
    n = len(llm)

    def fabricated(r) -> bool:
        try:
            return bool(json.loads(r["fabricated_ids"] or "[]"))
        except ValueError:
            return False

    alerts_planned = {r["alert_id"] for r in data["runs"]}
    llm_ok = {r["alert_id"] for r in raw_valid}
    prune_ok = llm_ok | {r["alert_id"] for r in pruned}
    all_ok = prune_ok | {r["alert_id"] for r in baseline}
    best: dict[int, dict] = {}
    for alert_id in alerts_planned:
        llm_scores = [r["score"] for r in rows if r["alert_id"] == alert_id and r["source"] in ("llm", "llm_pruned")
                      and r["outcome"] == "valid" and r["score"] is not None]
        base_scores = [r["score"] for r in baseline if r["alert_id"] == alert_id and r["score"] is not None]
        best[alert_id] = {"llm": max(llm_scores) if llm_scores else None, "baseline": max(base_scores) if base_scores else None}
    return {
        "samples": n,
        "valid_raw": len([r for r in llm if r["outcome"] == "valid"]),
        "valid_after_prune": len([r for r in llm if r["outcome"] == "valid"]) + len(pruned),
        "duplicates": len([r for r in llm if r["is_duplicate"]]),
        "hallucinated": len([r for r in llm if fabricated(r)]),
        "alerts": len(alerts_planned), "alerts_llm": len(llm_ok), "alerts_prune": len(prune_ok), "alerts_all": len(all_ok),
        "best": best,
    }


def pct(a: int, b: int) -> str:
    return f"{100 * a / b:.0f}% ({a} of {b})" if b else "n/a"


def render(label: str, before: dict, after: dict, after_data: dict) -> str:
    b, a = metrics(before), metrics(after)
    run = after["runs"][0] if after["runs"] else None
    lines = [
        f"### {label}", "",
        f"Model: `{run['model'] if run else '?'}`, {a['samples']} samples after versus {b['samples']} before, {a['alerts']} alerts.", "",
        "| Metric | Before | After |", "|---|---|---|",
        f"| Valid plan rate per sample, raw LLM output | {pct(b['valid_raw'], b['samples'])} | {pct(a['valid_raw'], a['samples'])} |",
        f"| Valid plan rate per sample, after the prune step | {pct(b['valid_after_prune'], b['samples'])} | {pct(a['valid_after_prune'], a['samples'])} |",
        f"| Alerts with at least one valid plan, LLM only | {pct(b['alerts_llm'], b['alerts'])} | {pct(a['alerts_llm'], a['alerts'])} |",
        f"| Alerts with at least one valid plan, LLM + prune | {pct(b['alerts_prune'], b['alerts'])} | {pct(a['alerts_prune'], a['alerts'])} |",
        f"| Alerts with at least one valid plan, LLM + prune + rule-based baseline | {pct(b['alerts_all'], b['alerts'])} | {pct(a['alerts_all'], a['alerts'])} |",
        f"| Duplicate rate (raw LLM samples) | {pct(b['duplicates'], b['samples'])} | {pct(a['duplicates'], a['samples'])} |",
        f"| Hallucination rate (non-existent ids, raw LLM output) | {pct(b['hallucinated'], b['samples'])} | {pct(a['hallucinated'], a['samples'])} |",
        "", "Best plan score per alert (0 to 1, from the ranking; blank = none):", "",
        "| Alert | Course | Shortfall | LLM best before | LLM best after | Baseline (after) |", "|---|---|---|---|---|---|",
    ]

    def show(v):
        return f"{v:.2f}" if v is not None else "–"

    alerts = after_data["alerts"]
    for alert_id in sorted(set(a["best"]) | set(b["best"])):
        info = alerts.get(alert_id)
        lines.append(
            f"| {alert_id} | {info['code'] if info else '?'} | {info['shortfall_type'] if info else '?'} | "
            f"{show(b['best'].get(alert_id, {}).get('llm'))} | {show(a['best'].get(alert_id, {}).get('llm'))} | "
            f"{show(a['best'].get(alert_id, {}).get('baseline'))} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", action="append", required=True, help='"Label=before.db=after.db[=first run id to count]"')
    parser.add_argument("--write", help="markdown file to update (the before/after section is replaced)")
    args = parser.parse_args()
    parts = ["", SECTION, "",
             "Generated by `python -m scripts.planner_eval`. Hallucination, duplicate and valid-plan rates use the raw LLM output only; "
             "pruned and rule-based plans are counted separately. Before = planner v1 (prompt p5-v1); after = planner v2 (prompt p6-v2).", ""]
    for spec in args.pair:
        label, before_path, after_path, *rest = spec.split("=")
        after = load(after_path, int(rest[0]) if rest else 0)  # optional 4th part: only runs from this id on
        parts.append(render(label, load(before_path), after, after))
    text = "\n".join(parts)
    if args.write:
        path = Path(args.write)
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if SECTION in current:
            current = current[: current.index(SECTION)].rstrip() + "\n"
        path.write_text(current.rstrip() + "\n" + text, encoding="utf-8")
        print(f"Updated {path}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
