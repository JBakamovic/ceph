#!/usr/bin/env python3
import subprocess
import json
import sys
from pathlib import Path

BINARY = "/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler"
SCENARIOS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/scenarios")
RESULTS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def run_bench(config_file, scheduler, load_model, extra_args=None, runtime=6):
    cfg_path = SCENARIOS_DIR / config_file
    out_json = RESULTS_DIR / f"temp_{config_file}_{scheduler}_{load_model}.json"
    
    cmd = [
        BINARY,
        "--config", str(cfg_path),
        "--scheduler", scheduler,
        "--load_model", load_model,
        "--runtime", str(runtime),
        "--export_json", str(out_json),
    ]
    if extra_args:
        cmd.extend(extra_args)
        
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"[-] Error: {res.stderr}", file=sys.stderr)
        return None
    with open(out_json) as f:
        data = json.load(f)
    out_json.unlink(missing_ok=True)
    return data

def format_cell(tenant_data):
    if not tenant_data:
        return "N/A"
    attempted = tenant_data.get("attempted", 0)
    accepted = tenant_data.get("accepted", 0)
    drop_pct = tenant_data.get("drop_rate_pct", 0.0)
    p50 = tenant_data.get("p50_ms", 0.0)
    p99 = tenant_data.get("p99_ms", 0.0)
    return f"{attempted} att | {accepted} acc ({drop_pct:.1f}%, p50={p50:.1f}ms, p99={p99:.1f}ms)"

def print_table(title, headers, rows):
    print(f"\n--- {title} ---")
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

def main():
    print("====================================================================================================")
    print(" CLOSED-LOOP VS. OPEN-LOOP POISSON LOAD GENERATION COMPARISON")
    print("====================================================================================================")

    # ------------------------------------------------------------------------
    # Comparison on Scenario 1: Classic Noisy Neighbor (6s)
    # ------------------------------------------------------------------------
    print("\n[+] Running Scenario 1 (Noisy Neighbor, 6s) under Closed-Loop vs. Open-Loop...")
    
    # Closed-Loop runs
    cl_throttler = run_bench("1_noisy_neighbor.json", "throttler", "closed_loop", runtime=6)
    cl_coarse    = run_bench("1_noisy_neighbor.json", "dmclock_coarse", "closed_loop", runtime=6)
    cl_fine      = run_bench("1_noisy_neighbor.json", "dmclock_fine_upstream", "closed_loop", runtime=6)

    # Open-Loop runs
    ol_throttler = run_bench("1_noisy_neighbor.json", "throttler", "open_loop", runtime=6)
    ol_coarse    = run_bench("1_noisy_neighbor.json", "dmclock_coarse", "open_loop", runtime=6)
    ol_fine      = run_bench("1_noisy_neighbor.json", "dmclock_fine_upstream", "open_loop", runtime=6)

    rows_closed = []
    rows_open = []
    t_names_1 = ["Tenant 1 (Bully Bulk Uploader)", "Tenant 2 (Interactive Small Ops)", "Tenant 3 (Health Monitor)"]
    
    for t_idx, t_name in enumerate(t_names_1):
        rows_closed.append([
            t_name,
            format_cell(cl_throttler["tenants"][t_idx] if cl_throttler else None),
            format_cell(cl_coarse["tenants"][t_idx] if cl_coarse else None),
            format_cell(cl_fine["tenants"][t_idx] if cl_fine else None),
        ])
        rows_open.append([
            t_name,
            format_cell(ol_throttler["tenants"][t_idx] if ol_throttler else None),
            format_cell(ol_coarse["tenants"][t_idx] if ol_coarse else None),
            format_cell(ol_fine["tenants"][t_idx] if ol_fine else None),
        ])

    print_table(
        "Scenario 1: CLOSED-LOOP Load (Offered load bends with scheduler response time)",
        ["Tenant Persona", "Throttler", "Coarse dmClock", "Fine dmClock Upstream"],
        rows_closed
    )

    print_table(
        "Scenario 1: OPEN-LOOP Poisson Load (Offered load strictly fixed & identical)",
        ["Tenant Persona", "Throttler", "Coarse dmClock", "Fine dmClock Upstream"],
        rows_open
    )

if __name__ == "__main__":
    main()
