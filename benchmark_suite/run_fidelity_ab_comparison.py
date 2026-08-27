#!/usr/bin/env python3
import subprocess
import json
import sys
from pathlib import Path

BINARY = "/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler"
SCENARIOS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/scenarios")
RESULTS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

def run_bench(config_file, scheduler, extra_args=None, runtime=6):
    cfg_path = SCENARIOS_DIR / config_file
    out_json = RESULTS_DIR / f"temp_{config_file}_{scheduler}.json"
    
    cmd = [
        BINARY,
        "--config", str(cfg_path),
        "--scheduler", scheduler,
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
    accepted = tenant_data.get("accepted", 0)
    drop_pct = tenant_data.get("drop_rate_pct", 0.0)
    p50 = tenant_data.get("p50_ms", 0.0)
    return f"{accepted} ({drop_pct:.1f}%, {p50:.1f}ms)"

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
    print(" FIDELITY KNOB A/B EMPIRICAL COMPARISON")
    print("====================================================================================================")

    # ------------------------------------------------------------------------
    # Experiment 1: at_limit (RejectThreshold{1.0} vs. AtLimit::Reject)
    # ------------------------------------------------------------------------
    print("\n[+] Running Experiment 1: at_limit A/B comparison on Scenario 6 (Intra-Class Starvation, 10s)...")
    res_reject_coarse = run_bench("6_intra_class_tenant_starvation.json", "dmclock_coarse", ["--at_limit", "reject"], 10)
    res_thresh_coarse = run_bench("6_intra_class_tenant_starvation.json", "dmclock_coarse", ["--at_limit", "reject_threshold", "--at_limit_threshold_s", "1.0"], 10)
    res_reject_fine   = run_bench("6_intra_class_tenant_starvation.json", "dmclock_fine_upstream", ["--at_limit", "reject"], 10)
    res_thresh_fine   = run_bench("6_intra_class_tenant_starvation.json", "dmclock_fine_upstream", ["--at_limit", "reject_threshold", "--at_limit_threshold_s", "1.0"], 10)

    rows_1 = []
    tenants = ["Tenant A (Bully Data)", "Tenant B (Interactive Data)", "Tenant C (Health Probe)"]
    for t_idx, t_name in enumerate(tenants):
        rows_1.append([
            t_name,
            format_cell(res_reject_coarse["tenants"][t_idx] if res_reject_coarse else None),
            format_cell(res_thresh_coarse["tenants"][t_idx] if res_thresh_coarse else None),
            format_cell(res_reject_fine["tenants"][t_idx] if res_reject_fine else None),
            format_cell(res_thresh_fine["tenants"][t_idx] if res_thresh_fine else None),
        ])
    print_table(
        "Experiment 1: at_limit Mode (Scenario 6 - 10s Run)",
        ["Tenant Persona", "Coarse (Reject)", "Coarse (RejectThresh 1s)", "Fine Upstream (Reject)", "Fine Upstream (RejectThresh 1s)"],
        rows_1
    )

    # ------------------------------------------------------------------------
    # Experiment 2: uniform_cost (Cost=1 vs. Differentiated Op Costs)
    # ------------------------------------------------------------------------
    print("\n[+] Running Experiment 2: uniform_cost A/B comparison on Scenario 4 (Tiered SLA Isolation, 6s)...")
    res_diff_cost = run_bench("4_tiered_sla_isolation.json", "dmclock_fine_upstream", ["--uniform_cost", "0"], 6)
    res_unif_cost = run_bench("4_tiered_sla_isolation.json", "dmclock_fine_upstream", ["--uniform_cost", "1"], 6)

    rows_2 = []
    t_names_4 = ["Tier 1 (Gold SLA - Premium)", "Tier 2 (Silver SLA - Standard)", "Tier 3 (Bronze SLA - Batch)"]
    for t_idx, t_name in enumerate(t_names_4):
        rows_2.append([
            t_name,
            format_cell(res_diff_cost["tenants"][t_idx] if res_diff_cost else None),
            format_cell(res_unif_cost["tenants"][t_idx] if res_unif_cost else None),
        ])
    print_table(
        "Experiment 2: Op Cost Weighting Model (Scenario 4 - 6s Run)",
        ["Tenant Persona", "Differentiated Cost (Get=1, Put=2..12)", "Uniform Cost (All Ops Cost=1)"],
        rows_2
    )

    # ------------------------------------------------------------------------
    # Experiment 3: capacity_relative (Scaling R,W,L with Cluster Knee)
    # ------------------------------------------------------------------------
    print("\n[+] Running Experiment 3: capacity_relative scaling on Scenario 9 (High-Throughput Scale, 6s)...")
    res_abs_64  = run_bench("9_high_throughput_scale.json", "dmclock_fine_upstream", ["--cluster_capacity", "64", "--capacity_relative", "0"], 6)
    res_rel_256 = run_bench("9_high_throughput_scale.json", "dmclock_fine_upstream", ["--cluster_capacity", "256", "--capacity_relative", "1", "--capacity_estimate_ops_s", "256"], 6)

    rows_3 = []
    t_names_9 = [
        "Tenant 1 [Control] Health Monitor",
        "Tenant 2 [Tier 1] High-Scale Ingestion VIP",
        "Tenant 3 [Tier 2] Real-Time Web & Search",
        "Tenant 4 [Tier 3] Internal Metrics & Logs",
        "Tenant 5 [Tier 4] Free-Tier Bulk Flood"
    ]
    for t_idx, t_name in enumerate(t_names_9):
        rows_3.append([
            t_name,
            format_cell(res_abs_64["tenants"][t_idx] if res_abs_64 else None),
            format_cell(res_rel_256["tenants"][t_idx] if res_rel_256 else None),
        ])
    print_table(
        "Experiment 3: Capacity Scaling Model (Scenario 9 - 6s Run)",
        ["Tenant Persona", "Absolute Rates (Cap=64)", "Capacity-Relative (Cap=256)"],
        rows_3
    )

if __name__ == "__main__":
    main()
