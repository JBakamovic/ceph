#!/usr/bin/env python3
"""Run one scenario across schedulers, with repeats, and record provenance.

The existing run_suite.py builds its scenarios from dicts inlined in the script
and writes them over benchmark_suite/scenarios/*.json. That predates the
at_limit / uniform_cost / load_model / capacity_relative knobs, so running it now
would silently overwrite the pinned scenarios and reproduce none of the current
results. This runner takes a scenario file as given and never edits it.

Every result carries a provenance block -- binary mtime, git HEAD, whether the
tree was dirty, the exact argv -- because a benchmark number without the code
that produced it is not reproducible, and this project has already been bitten by
results whose origin could not be reconstructed.

Usage:
  run_experiment.py SCENARIO.json [-s throttler -s dmclock_fine_upstream]
                                  [-n 3] [--runtime 10] [-o results/dir]
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SUITE = Path(__file__).resolve().parent
REPO = SUITE.parent
DEFAULT_BIN = Path.home() / "development" / "build-ceph" / "release" / "bin" / "bench_rgw_scheduler"


def git(*args):
    try:
        return subprocess.check_output(["git", "-C", str(REPO), *args],
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        return "unknown"


def provenance(binary):
    return {
        "binary": str(binary),
        "binary_mtime": os.path.getmtime(binary) if os.path.exists(binary) else None,
        "git_head": git("rev-parse", "HEAD"),
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(git("status", "--porcelain", "--", "src/", "benchmark_suite/")),
        "argv": sys.argv,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", type=Path)
    ap.add_argument("-s", "--scheduler", action="append", default=None,
                    help="repeatable; default: throttler, dmclock_coarse, dmclock_fine_upstream")
    ap.add_argument("-n", "--repeats", type=int, default=3)
    ap.add_argument("--runtime", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=120,
                    help="a run that exceeds this is recorded as incomplete, not silently dropped")
    ap.add_argument("-b", "--binary", type=Path, default=DEFAULT_BIN)
    ap.add_argument("-o", "--out", type=Path, default=SUITE / "results" / "experiments")
    args = ap.parse_args()

    scheds = args.scheduler or ["throttler", "dmclock_coarse", "dmclock_fine_upstream"]
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.scenario.stem
    summary = {"scenario": str(args.scenario), "provenance": provenance(args.binary),
               "runtime_s": args.runtime, "repeats": args.repeats, "runs": {}}

    for sched in scheds:
        summary["runs"][sched] = []
        for i in range(1, args.repeats + 1):
            out = args.out / f"{stem}_{sched}_{i}.json"
            cmd = [str(args.binary), "--config", str(args.scenario),
                   "--scheduler", sched, "--runtime", str(args.runtime),
                   "--export_json", str(out)]
            print(f"  {sched} run {i}/{args.repeats} ... ", end="", flush=True)
            try:
                subprocess.run(cmd, timeout=args.timeout, check=True,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                print("DID NOT COMPLETE")
                summary["runs"][sched].append({"run": i, "status": "timeout"})
                continue
            except subprocess.CalledProcessError as e:
                print(f"FAILED rc={e.returncode}")
                summary["runs"][sched].append({"run": i, "status": f"rc={e.returncode}"})
                continue
            d = json.load(open(out))
            elapsed = d["elapsed_time_s"]
            total = sum(t["accepted"] for t in d["tenants"]) / elapsed
            print(f"{total:.0f} tps")
            summary["runs"][sched].append({
                "run": i, "status": "ok", "result_file": str(out),
                "total_tps": total,
                "per_tenant": {t["name"]: {"accepted_per_s": t["accepted"] / elapsed,
                                           "drop_pct": t["drop_rate_pct"],
                                           "p50_ms": t["p50_ms"]} for t in d["tenants"]},
                "dmclock_counters": d.get("dmclock_counters"),
                "fidelity": d.get("fidelity"),
            })

    path = args.out / f"{stem}_summary.json"
    json.dump(summary, open(path, "w"), indent=2)
    print(f"\nsummary -> {path}")
    if summary["provenance"]["git_dirty"]:
        print("note: tree was dirty; these numbers are not reproducible from git alone")


if __name__ == "__main__":
    main()
