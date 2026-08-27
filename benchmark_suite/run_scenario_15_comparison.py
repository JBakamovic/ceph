#!/usr/bin/env python3
import subprocess
import json
import sys
from pathlib import Path

BINARY = "/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler"
SCENARIO = Path("/home/jbakamovic/development/ceph/benchmark_suite/scenarios/15_osd_backend_congestion_collapse.json")
RESULTS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/results/experiments")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def run_arm(name, scheduler, adaptive=False, runtime=10):
    out_json = RESULTS_DIR / f"15_temp_{name}.json"
    cmd = [
        BINARY,
        "--config", str(SCENARIO),
        "--scheduler", scheduler,
        "--runtime", str(runtime),
        "--export_json", str(out_json),
    ]
    if adaptive:
        cmd.extend(["--adaptive", "--target_latency_ms", "6.0", "--sample_interval_ms", "50"])
        
    print(f"  Running {name} ({runtime}s)... ", end="", flush=True)
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"FAILED: {res.stderr}")
        return None
    with open(out_json) as f:
        data = json.load(f)
    out_json.unlink(missing_ok=True)
    elapsed = data["elapsed_time_s"]
    total = sum(t["accepted"] for t in data["tenants"]) / elapsed
    print(f"{total:.1f} TPS")
    return data

def format_cell(t_data):
    if not t_data:
        return "N/A"
    acc = t_data.get("accepted", 0)
    drop = t_data.get("drop_rate_pct", 0.0)
    p50 = t_data.get("p50_ms", 0.0)
    p99 = t_data.get("p99_ms", 0.0)
    return f"{acc} acc ({drop:.1f}% drops, p50={p50:.1f}ms, p99={p99:.1f}ms)"

def main():
    print("====================================================================================================")
    print(" SCENARIO 15: OSD BACKEND CONGESTION COLLAPSE — STATIC VS. ADAPTIVE AIMD COMPARISON")
    print("====================================================================================================")

    res_throttler = run_arm("throttler_static", "throttler", adaptive=False)
    res_coarse    = run_arm("dmclock_coarse_static", "dmclock_coarse", adaptive=False)
    res_fine_stat = run_arm("dmclock_fine_static", "dmclock_fine_upstream", adaptive=False)
    res_fine_adpt = run_arm("dmclock_fine_adaptive", "dmclock_fine_upstream", adaptive=True)

    headers = [
        "Tenant Persona",
        "Throttler (Static)",
        "Coarse dmClock (Static)",
        "Fine Upstream (Static)",
        "Fine Upstream (Adaptive AIMD)"
    ]

    t_names = ["VIP Latency-Sensitive App", "Bulk Ingestion Bully", "System Health Monitor"]
    rows = []

    for idx, t_name in enumerate(t_names):
        rows.append([
            t_name,
            format_cell(res_throttler["tenants"][idx] if res_throttler else None),
            format_cell(res_coarse["tenants"][idx] if res_coarse else None),
            format_cell(res_fine_stat["tenants"][idx] if res_fine_stat else None),
            format_cell(res_fine_adpt["tenants"][idx] if res_fine_adpt else None),
        ])

    print("\n--- Summary Table ---")
    widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(str(val)))
            
    header_str = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    print(header_str)
    print("| " + " | ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers)) + " |")
    print(header_str)
    for row in rows:
        print("| " + " | ".join(f"{str(val):<{widths[i]}}" for i, val in enumerate(row)) + " |")
    print(header_str)

if __name__ == "__main__":
    main()
