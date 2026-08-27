#!/usr/bin/env python3
import subprocess
import json
import os
import sys
from pathlib import Path

BINARY = "/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler"
SCENARIOS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/scenarios")
RESULTS_DIR = Path("/home/jbakamovic/development/ceph/benchmark_suite/results")
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

SCENARIOS = [
    ("1_noisy_neighbor.json", "Scenario 1: Classic Noisy Neighbor", 6),
    ("2_metadata_crawler_storm.json", "Scenario 2: Metadata Index Lock Storm", 6),
    ("3_backend_congestion_and_spikes.json", "Scenario 3: Severe Backend Congestion & Spikes", 6),
    ("4_tiered_sla_isolation.json", "Scenario 4: Multi-Tenant Tiered SLA Isolation", 6),
    ("5_retry_storm_dynamics.json", "Scenario 5: Retry Storm Dynamics", 6),
    ("6_intra_class_tenant_starvation.json", "Scenario 6: Intra-Class S3 Tenant Starvation", 10),
    ("7_multi_tier_tenant_qos.json", "Scenario 7: 6-Tier Multi-Tenant Production QoS", 10),
    ("8_adaptive_capacity_tuning.json", "Scenario 8: Closed-Loop Adaptive Capacity Tuning", 12),
    ("9_high_throughput_scale.json", "Scenario 9: High-Throughput Multi-Tenant Scale", 10),
    ("10_proportional_tier_iop_scaling.json", "Scenario 10: Proportional Tier IOP Scaling", 10),
]

SCHEDULERS = ["throttler", "dmclock_coarse", "dmclock_fine", "dmclock_fine_upstream"]

def run_experiment(scenario_file, scheduler, runtime):
    cfg_path = SCENARIOS_DIR / scenario_file
    stem = scenario_file.replace(".json", "")
    out_json = RESULTS_DIR / f"{stem}_{scheduler}.json"
    out_csv = RESULTS_DIR / f"{stem}_{scheduler}.csv"
    
    cmd = [
        BINARY,
        "--config", str(cfg_path),
        "--scheduler", scheduler,
        "--runtime", str(runtime),
        "--export_json", str(out_json),
        "--export_csv", str(out_csv)
    ]
    
    # For scenario 8 adaptive:
    if "8_adaptive" in scenario_file and "adaptive" in scheduler:
        cmd.extend(["--adaptive", "--target_latency_ms", "3.0", "--sample_interval_ms", "50"])
        
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if res.returncode != 0:
        print(f"[-] ERROR running {scenario_file} with {scheduler}: {res.stderr}", file=sys.stderr)
        return None
    
    with open(out_json) as f:
        return json.load(f)

def format_table(header, rows):
    col_widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(cell)))
    
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    
    lines = [sep]
    header_line = "| " + " | ".join(f"{h:<{col_widths[i]}}" for i, h in enumerate(header)) + " |"
    lines.append(header_line)
    lines.append(sep)
    
    for row in rows:
        row_line = "| " + " | ".join(f"{str(cell):<{col_widths[i]}}" for i, cell in enumerate(row)) + " |"
        lines.append(row_line)
    lines.append(sep)
    return "\n".join(lines)

def main():
    print(f"[*] Starting benchmark matrix across {len(SCENARIOS)} scenarios and 4 schedulers...")
    
    full_report = []
    
    for scenario_file, title, runtime in SCENARIOS:
        print(f"\n[+] Running {title} ({runtime}s)...")
        results = {}
        for sched in SCHEDULERS:
            # For scenario 5, retry dynamics is throttler, coarse, fine, fine_upstream
            # For scenario 8, we can test coarse, fine, fine_upstream
            data = run_experiment(scenario_file, sched, runtime)
            results[sched] = data
        
        # Build ASCII Table for this scenario
        header = ["Tenant Persona", "throttler", "dmclock_coarse", "dmclock_fine", "dmclock_fine_upstream"]
        rows = []
        
        # Get tenant names from first available result
        sample_res = next((r for r in results.values() if r is not None), None)
        if not sample_res:
            continue
            
        for t_idx, t_info in enumerate(sample_res["tenants"]):
            name = t_info["name"]
            row = [name]
            for sched in SCHEDULERS:
                sched_res = results.get(sched)
                if sched_res and t_idx < len(sched_res["tenants"]):
                    t_data = sched_res["tenants"][t_idx]
                    acc = t_data["accepted"]
                    dr = t_data["drop_rate_pct"]
                    p50 = t_data["p50_ms"]
                    rps = t_data.get("throughput_tps", 0.0)
                    if "9_high" in scenario_file or "10_prop" in scenario_file:
                        row.append(f"{acc:,} ({rps:,.1f} r/s, {p50:.1f}ms)")
                    else:
                        row.append(f"{acc} ({dr:.1f}%, {p50:.1f}ms)")
                else:
                    row.append("N/A")
            rows.append(row)
            
        table_str = format_table(header, rows)
        report_block = f"--- {title} ({runtime}s Run) ---\n{table_str}\n"
        print(report_block)
        full_report.append(report_block)
        
    with open("/home/jbakamovic/development/ceph/benchmark_suite/results/full_upstream_comparison_report.txt", "w") as f:
        f.write("\n".join(full_report))
    print("[+] Full report saved to benchmark_suite/results/full_upstream_comparison_report.txt")

if __name__ == "__main__":
    main()
