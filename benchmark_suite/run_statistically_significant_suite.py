#!/usr/bin/env python3
"""
Master Statistically Significant Live Ceph Cluster Evaluation Suite.
Runs multi-trial A/B benchmarks comparing 'throttler' vs 'dmclock' with
warmup, 95% Confidence Intervals, variance tracking (CV%), and Welch's t-tests.
"""

import sys
import os
import time
import subprocess
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from statistical_live_runner import run_statistically_rigorous_benchmark, welch_ttest
from run_live_cluster_ab_suite import (
    restart_rgw,
    get_workload_1_noisy_neighbor,
    get_workload_2_metadata_crawler,
    get_workload_3_tiered_sla_qos,
    get_workload_4_dynamic_churn,
    get_workload_6_osd_scrub_congestion,
    get_workload_7_line_rate_saturation,
    CEPH_CONF,
    CEPH_BIN
)

def format_statistical_ab_table(title, throttler_stats, dmclock_stats):
    print("\n" + "="*125)
    print(f" STATISTICALLY SIGNIFICANT A/B COMPARISON: {title} (5 Trials x 4s, 95% CI, Welch's t-test)")
    print("="*125)
    
    header = (
        f"| {'Tenant Persona':<28} "
        f"| {'Throttler Mean TPS (95% CI)':<26} "
        f"| {'dmClock Mean TPS (95% CI)':<26} "
        f"| {'Throttler p50':<14} "
        f"| {'dmClock p50':<14} "
        f"| {'p-val / Sig':<12} |"
    )
    sep = "+" + "-"*30 + "+" + "-"*28 + "+" + "-"*28 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*14 + "+"
    print(sep)
    print(header)
    print(sep)

    for name in throttler_stats:
        t = throttler_stats[name]
        d = dmclock_stats.get(name, {})
        
        t_tps = t["tps"]
        d_tps = d.get("tps", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
        t_p50 = t["p50_ms"]
        d_p50 = d.get("p50_ms", {"mean": 0.0})
        
        t_samples = t["raw_samples"]["tps"]
        d_samples = d.get("raw_samples", {}).get("tps", [])
        
        _, p_val, sig = welch_ttest(t_samples, d_samples)
        
        t_tps_str = f"{t_tps['mean']:.1f} ± {(t_tps['ci_upper'] - t_tps['mean']):.1f}"
        d_tps_str = f"{d_tps['mean']:.1f} ± {(d_tps['ci_upper'] - d_tps['mean']):.1f}"
        t_p50_str = f"{t_p50['mean']:.1f}ms"
        d_p50_str = f"{d_p50['mean']:.1f}ms"
        p_str = f"{p_val:.4f} {sig}"
        
        row = f"| {name:<28} | {t_tps_str:<26} | {d_tps_str:<26} | {t_p50_str:<14} | {d_p50_str:<14} | {p_str:<12} |"
        print(row)
    print(sep)
    print("  Significance legend: *** (p < 0.001), ** (p < 0.01), * (p < 0.05), ns (not significant)")

def main():
    print("#"*125)
    print(" STARTING STATISTICALLY SIGNIFICANT LIVE CEPH CLUSTER A/B BENCHMARK")
    print(" Methodology: 2s Warmup + 5 Independent Trials x 4s per Workload + Welch's t-test")
    print("#"*125)

    workloads = [
        ("Noisy Neighbor Overload", get_workload_1_noisy_neighbor()),
        ("Dynamic Tenant Churn", get_workload_4_dynamic_churn()),
        ("5-Tier SLA QoS", get_workload_3_tiered_sla_qos()),
        ("Metadata Crawler vs Streamer", get_workload_2_metadata_crawler()),
        ("Live OSD Scrub Congestion Spike", get_workload_6_osd_scrub_congestion()),
        ("Line-Rate Disk Saturation", get_workload_7_line_rate_saturation())
    ]

    all_results = {}

    for sched in ["throttler", "dmclock"]:
        per_tenant = "true" if sched == "dmclock" else "false"
        adaptive = "true" if sched == "dmclock" else "false"
        restart_rgw(scheduler_type=sched, per_tenant=per_tenant, adaptive=adaptive)
        all_results[sched] = {}
        
        for name, config in workloads:
            if "Scrub" in name:
                print(f"\n[*] Injecting live OSD deep scrub on osd.0 for {name}...")
                subprocess.run([CEPH_BIN, "-c", CEPH_CONF, "tell", "osd.0", "scrub"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            stats = run_statistically_rigorous_benchmark(
                scenario_name=f"{name} [{sched}]",
                tenants_config=config,
                trials=5,
                trial_duration_s=4.0,
                warmup_duration_s=2.0,
                cooldown_s=1.0
            )
            all_results[sched][name] = stats

    # Print Final Statistically Significant Tables
    print("\n\n" + "#"*125)
    print(" FINAL STATISTICALLY SIGNIFICANT LIVE CEPH CLUSTER A/B RESULTS")
    print("#"*125)

    for name, _ in workloads:
        format_statistical_ab_table(name, all_results["throttler"][name], all_results["dmclock"][name])

    # Save to Markdown Report
    out_file = "/home/jbakamovic/development/ceph/benchmark_suite/results/STATISTICAL_SIGNIFICANCE_REPORT.md"
    with open(out_file, "w") as f:
        f.write("# Statistically Significant Live Ceph Cluster Benchmark Report\n\n")
        f.write("Evaluation across 5 independent trials (4s each) with 2s warmup and 1s cooldown drainage.\n")
        f.write("Statistical Metrics: Sample Mean, 95% Confidence Interval (CI), Coefficient of Variation (CV%), and Welch's two-sample t-test p-values.\n\n")
        
        for name, _ in workloads:
            f.write(f"### {name}\n\n")
            f.write("| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n")
            
            t_stats = all_results["throttler"][name]
            d_stats = all_results["dmclock"][name]
            
            for t_name in t_stats:
                t = t_stats[t_name]
                d = d_stats.get(t_name, {})
                
                t_tps = t["tps"]
                d_tps = d.get("tps", {"mean": 0.0, "ci_lower": 0.0, "ci_upper": 0.0})
                t_p50 = t["p50_ms"]
                d_p50 = d.get("p50_ms", {"mean": 0.0})
                
                t_samples = t["raw_samples"]["tps"]
                d_samples = d.get("raw_samples", {}).get("tps", [])
                
                t_stat, p_val, sig = welch_ttest(t_samples, d_samples)
                
                t_tps_str = f"{t_tps['mean']:.1f} ± {(t_tps['ci_upper'] - t_tps['mean']):.1f}"
                d_tps_str = f"{d_tps['mean']:.1f} ± {(d_tps['ci_upper'] - d_tps['mean']):.1f}"
                
                f.write(f"| {t_name} | {t_tps_str} | {d_tps_str} | {t_p50['mean']:.1f}ms | {d_p50['mean']:.1f}ms | {t_stat:.2f} | {p_val:.4e} | {sig} |\n")
            f.write("\n")

    print(f"\n[+] Statistically verified report saved to {out_file}")

if __name__ == "__main__":
    main()
