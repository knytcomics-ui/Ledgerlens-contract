#!/usr/bin/env python3
"""Record CI job durations, publish a trend summary and enforce the
wall-clock budget (issue #1224).

Subcommands:
  collect  Fetch recent completed runs of a workflow via the GitHub REST API
           and append one JSON record per run to a JSON-lines dataset
           (runs already present are skipped, so the dataset is append-only).
  quality  Data-quality self-check report (#1405).
  trend    Summarise the dataset: median wall-clock of required checks and of
           each job over a recent window versus the previous window, plus
           budget warnings for the most recent run.

Budgets live in .github/ci-budget.json. Requires GITHUB_TOKEN and
GITHUB_REPOSITORY for `collect`.
"""

import argparse
import json
import os
import statistics
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://api.github.com"


def _get(path):
    req = urllib.request.Request(API + path, headers={
        "Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _ts(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")


def summarise_run(run, jobs):
    durations = {}
    starts, ends = [], []
    for job in jobs:
        if not job.get("started_at") or not job.get("completed_at") or job.get("conclusion") == "skipped":
            continue
        start, end = _ts(job["started_at"]), _ts(job["completed_at"])
        durations[job["name"]] = int((end - start).total_seconds())
        starts.append(start)
        ends.append(end)
    wall = int((max(ends) - min(starts)).total_seconds()) if starts else 0
    return {
        "run_id": run["id"],
        "head_sha": run.get("head_sha"),
        "created_at": run.get("created_at"),
        "wall_clock_seconds": wall,
        "jobs": dict(sorted(durations.items())),
    }


def budget_warnings(record, budget):
    warnings = []
    limit = budget.get("required_checks_seconds")
    if limit and record["wall_clock_seconds"] > limit:
        warnings.append(f"Required checks took {record['wall_clock_seconds']}s, budget is {limit}s")
    for name, job_limit in budget.get("jobs", {}).items():
        took = record["jobs"].get(name)
        if took is not None and took > job_limit:
            warnings.append(f"Job '{name}' took {took}s, budget is {job_limit}s")
    return warnings


def validate_record(record, seen_ids=()):
    """Return a list of data-quality problems (empty when the record is sane)."""
    problems = []
    run_id = record.get("run_id")
    if run_id is None:
        problems.append("missing run_id")
    elif run_id in seen_ids:
        problems.append(f"duplicate run_id {run_id}")
    wall = record.get("wall_clock_seconds")
    if not isinstance(wall, int) or wall < 0:
        problems.append(f"implausible wall-clock {wall!r}")
    for name, took in (record.get("jobs") or {}).items():
        if not isinstance(took, int) or took < 0:
            problems.append(f"job '{name}' has implausible duration {took!r}")
        elif isinstance(wall, int) and wall >= 0 and took > wall:
            problems.append(f"job '{name}' ({took}s) longer than run wall-clock ({wall}s)")
    return problems


def summarise_jobs_problems(jobs):
    """Flag raw job rows whose timestamps are inconsistent (clock skew, negative)."""
    problems = []
    for job in jobs:
        if not job.get("started_at") or not job.get("completed_at"):
            # Cancelled/in-progress jobs are skipped by summarise_run; report them.
            if job.get("conclusion") not in (None, "skipped"):
                problems.append(f"job '{job.get('name')}' ({job.get('conclusion')}) has no start/end timestamp")
            continue
        if _ts(job["completed_at"]) < _ts(job["started_at"]):
            problems.append(f"job '{job.get('name')}' completed before it started (clock skew)")
    return problems


def load_dataset(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def cmd_collect(args):
    repo = os.environ["GITHUB_REPOSITORY"]
    records = load_dataset(args.dataset)
    seen = {r["run_id"] for r in records}
    runs = _get(f"/repos/{repo}/actions/workflows/{args.workflow}/runs"
                f"?branch={args.branch}&status=completed&per_page={args.limit}")["workflow_runs"]
    added = 0
    rejected = []
    for run in sorted(runs, key=lambda r: r["id"]):
        if run["id"] in seen:
            continue
        jobs = _get(f"/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
        # Retried runs reuse run_id with a higher run_attempt; only the latest counts.
        problems = summarise_jobs_problems(jobs)
        record = summarise_run(run, jobs)
        problems += validate_record(record, seen)
        if problems:
            rejected.append({"run_id": run["id"], "problems": problems})
            print(f"::warning title=CI metrics data quality::run {run['id']} rejected: {'; '.join(problems)}")
            continue
        records.append(record)
        seen.add(record["run_id"])
        added += 1
    Path(args.dataset).parent.mkdir(parents=True, exist_ok=True)
    Path(args.dataset).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records))
    if rejected:
        quarantine = Path(args.dataset).with_name("rejected.jsonl")
        with quarantine.open("a") as fh:
            for r in rejected:
                fh.write(json.dumps({**r, "seen_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n")
    print(f"Recorded {added} new run(s), rejected {len(rejected)}; dataset has {len(records)}.")
    return 0


def cmd_quality(args):
    """Data-quality self-check: re-validate the dataset and summarise rejections."""
    records = load_dataset(args.dataset)
    seen, bad = set(), []
    for r in records:
        for p in validate_record(r, seen):
            bad.append(f"run {r.get('run_id')}: {p}")
        seen.add(r.get("run_id"))
    rej_path = Path(args.dataset).with_name("rejected.jsonl")
    rejected = load_dataset(rej_path)
    cutoff = None
    if args.days:
        from datetime import timedelta
        cutoff = datetime.utcnow() - timedelta(days=args.days)
        rejected = [r for r in rejected if not r.get("seen_at") or _ts(r["seen_at"]) >= cutoff]
    lines = ["## CI metrics data quality", "",
             f"{len(records)} record(s) in dataset; {len(bad)} invalid; "
             f"{len(rejected)} rejected at ingestion in the last {args.days} day(s).", ""]
    lines += [f"- {b}" for b in bad] + [f"- run {r['run_id']} rejected: {'; '.join(r['problems'])}" for r in rejected]
    md = "\n".join(lines) + "\n"
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(md)
    return 1 if bad else 0


def selftest():
    import unittest

    good = {"run_id": 1, "wall_clock_seconds": 100, "jobs": {"a": 90, "b": 100}}

    class T(unittest.TestCase):
        def test_valid_passes(self):
            self.assertEqual(validate_record(good), [])

        def test_duplicate_run_id(self):
            self.assertTrue(validate_record(good, {1}))

        def test_negative_durations(self):
            self.assertTrue(validate_record({**good, "wall_clock_seconds": -5}))
            self.assertTrue(validate_record({**good, "jobs": {"a": -1}}))

        def test_job_longer_than_run(self):
            self.assertTrue(validate_record({**good, "jobs": {"a": 500}}))

        def test_missing_run_id(self):
            self.assertTrue(validate_record({"wall_clock_seconds": 1, "jobs": {}}))

        def test_clock_skew_and_cancelled_jobs(self):
            skew = {"name": "x", "started_at": "2026-01-01T00:10:00Z", "completed_at": "2026-01-01T00:00:00Z"}
            self.assertTrue(summarise_jobs_problems([skew]))
            cancelled = {"name": "y", "conclusion": "cancelled", "started_at": None, "completed_at": None}
            self.assertTrue(summarise_jobs_problems([cancelled]))
            ok = {"name": "z", "conclusion": "success", "started_at": "2026-01-01T00:00:00Z", "completed_at": "2026-01-01T00:01:00Z"}
            self.assertEqual(summarise_jobs_problems([ok]), [])
            self.assertEqual(validate_record(summarise_run({"id": 9}, [ok])), [])

    result = unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromTestCase(T))
    return 0 if result.wasSuccessful() else 1


def _median(values):
    return statistics.median(values) if values else None


def cmd_trend(args):
    records = sorted(load_dataset(args.dataset), key=lambda r: r["run_id"])
    budget = json.loads(Path(args.budget).read_text())
    recent, previous = records[-args.window:], records[-2 * args.window:-args.window]
    lines = ["## CI duration trend", "",
             f"Window: last {len(recent)} run(s) vs previous {len(previous)} run(s) on main.", "",
             "| Scope | Median now | Median before | Budget |", "|---|---|---|---|"]

    def row(label, now, before, limit):
        f = lambda v: "-" if v is None else f"{v / 60:.1f} min"
        lines.append(f"| {label} | {f(now)} | {f(before)} | {f(limit)} |")

    row("**Required checks (wall clock)**",
        _median([r["wall_clock_seconds"] for r in recent]),
        _median([r["wall_clock_seconds"] for r in previous]),
        budget.get("required_checks_seconds"))
    for name in sorted({j for r in recent for j in r["jobs"]}):
        row(name, _median([r["jobs"][name] for r in recent if name in r["jobs"]]),
            _median([r["jobs"][name] for r in previous if name in r["jobs"]]),
            budget.get("jobs", {}).get(name))

    warnings = budget_warnings(recent[-1], budget) if recent else []
    if warnings:
        lines += ["", "### Budget exceeded (latest run)", ""] + [f"- {w}" for w in warnings]
    markdown = "\n".join(lines) + "\n"
    print(markdown)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(markdown)
    for w in warnings:
        print(f"::warning title=CI wall-clock budget::{w}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--dataset", required=True)
    c.add_argument("--workflow", default="ci.yml")
    c.add_argument("--branch", default="main")
    c.add_argument("--limit", type=int, default=30)
    t = sub.add_parser("trend")
    t.add_argument("--dataset", required=True)
    t.add_argument("--budget", default=".github/ci-budget.json")
    t.add_argument("--window", type=int, default=10)
    q = sub.add_parser("quality")
    q.add_argument("--dataset", required=True)
    q.add_argument("--days", type=int, default=30)
    sub.add_parser("selftest")
    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return selftest()
    return {"collect": cmd_collect, "trend": cmd_trend, "quality": cmd_quality}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
