#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
BENCHMARK_BIN = REPO_ROOT / "build-ceph/release/bin/bench_rgw_scheduler"
if not BENCHMARK_BIN.exists():
    BENCHMARK_BIN = Path("/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler")

SCENARIOS_DIR = REPO_ROOT / "benchmark_suite/scenarios"
OLD_RESULTS_DIR = REPO_ROOT / "benchmark_suite/results"
NEW_RESULTS_DIR = REPO_ROOT / "benchmark_suite/results_new"

NEW_RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# List of experiment runs to execute
EXPERIMENTS = [
    # 1. Noisy Neighbor (Runtime 6s, 8 threads)
    {
        "id": "1_noisy_neighbor_throttler",
        "scenario": "1_noisy_neighbor.json",
        "scheduler": "throttler",
        "extra_args": []
    },
    {
        "id": "1_noisy_neighbor_dmclock",
        "scenario": "1_noisy_neighbor.json",
        "scheduler": "dmclock",
        "extra_args": []
    },
    {
        "id": "1_noisy_neighbor_none",
        "scenario": "1_noisy_neighbor.json",
        "scheduler": "none",
        "extra_args": []
    },

    # 2. Metadata Crawler Storm (Runtime 6s, 8 threads)
    {
        "id": "2_metadata_crawler_storm_throttler",
        "scenario": "2_metadata_crawler_storm.json",
        "scheduler": "throttler",
        "extra_args": []
    },
    {
        "id": "2_metadata_crawler_storm_dmclock",
        "scenario": "2_metadata_crawler_storm.json",
        "scheduler": "dmclock",
        "extra_args": []
    },
    {
        "id": "2_metadata_crawler_storm_none",
        "scenario": "2_metadata_crawler_storm.json",
        "scheduler": "none",
        "extra_args": []
    },

    # 3. Backend Congestion and Spikes (Runtime 6s, 8 threads)
    {
        "id": "3_backend_congestion_and_spikes_throttler",
        "scenario": "3_backend_congestion_and_spikes.json",
        "scheduler": "throttler",
        "extra_args": []
    },
    {
        "id": "3_backend_congestion_and_spikes_dmclock",
        "scenario": "3_backend_congestion_and_spikes.json",
        "scheduler": "dmclock",
        "extra_args": []
    },
    {
        "id": "3_backend_congestion_and_spikes_none",
        "scenario": "3_backend_congestion_and_spikes.json",
        "scheduler": "none",
        "extra_args": []
    },

    # 4. Tiered SLA Isolation (Runtime 6s, 8 threads)
    {
        "id": "4_tiered_sla_isolation_throttler",
        "scenario": "4_tiered_sla_isolation.json",
        "scheduler": "throttler",
        "extra_args": []
    },
    {
        "id": "4_tiered_sla_isolation_dmclock",
        "scenario": "4_tiered_sla_isolation.json",
        "scheduler": "dmclock",
        "extra_args": []
    },
    {
        "id": "4_tiered_sla_isolation_none",
        "scenario": "4_tiered_sla_isolation.json",
        "scheduler": "none",
        "extra_args": []
    },

    # 5. Retry Storm Dynamics (Runtime 6s, 8 threads)
    {
        "id": "5_retry_storm_dynamics_throttler",
        "scenario": "5_retry_storm_dynamics.json",
        "scheduler": "throttler",
        "extra_args": ["--retries"]
    },
    {
        "id": "5_retry_storm_dynamics_dmclock",
        "scenario": "5_retry_storm_dynamics.json",
        "scheduler": "dmclock",
        "extra_args": ["--retries"]
    },

    # 6. Scenario 6: Intra-Class Starvation across Thread Counts (Runtime 10s)
    # 8 Threads
    {
        "id": "6_starvation_throttler",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "throttler",
        "extra_args": ["--threads", "8", "--runtime", "10"]
    },
    {
        "id": "6_starvation_dmclock_coarse",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "dmclock_coarse",
        "extra_args": ["--threads", "8", "--runtime", "10"]
    },
    {
        "id": "6_starvation_dmclock_fine",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "dmclock_fine",
        "extra_args": ["--threads", "8", "--runtime", "10"]
    },
    # 16 Threads
    {
        "id": "6_starvation_throttler_t16",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "throttler",
        "extra_args": ["--threads", "16", "--runtime", "10"]
    },
    {
        "id": "6_starvation_dmclock_coarse_t16",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "dmclock_coarse",
        "extra_args": ["--threads", "16", "--runtime", "10"]
    },
    {
        "id": "6_starvation_dmclock_fine_t16",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "dmclock_fine",
        "extra_args": ["--threads", "16", "--runtime", "10"]
    },
    # 32 Threads
    {
        "id": "6_starvation_throttler_t32",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "throttler",
        "extra_args": ["--threads", "32", "--runtime", "10"]
    },
    {
        "id": "6_starvation_dmclock_coarse_t32",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "dmclock_coarse",
        "extra_args": ["--threads", "32", "--runtime", "10"]
    },
    {
        "id": "6_starvation_dmclock_fine_t32",
        "scenario": "6_intra_class_tenant_starvation.json",
        "scheduler": "dmclock_fine",
        "extra_args": ["--threads", "32", "--runtime", "10"]
    },

    # 7. Scenario 7: 6-Tier QoS Hierarchy (Runtime 10s, 8 threads)
    {
        "id": "7_multi_tier_throttler",
        "scenario": "7_multi_tier_tenant_qos.json",
        "scheduler": "throttler",
        "extra_args": ["--runtime", "10"]
    },
    {
        "id": "7_multi_tier_dmclock_coarse",
        "scenario": "7_multi_tier_tenant_qos.json",
        "scheduler": "dmclock_coarse",
        "extra_args": ["--runtime", "10"]
    },
    {
        "id": "7_multi_tier_dmclock_fine",
        "scenario": "7_multi_tier_tenant_qos.json",
        "scheduler": "dmclock_fine",
        "extra_args": ["--runtime", "10"]
    },

    # 8. Scenario 8: Adaptive Capacity Tuning (Runtime 12s, 8 threads)
    {
        "id": "8_static_dmclock_coarse",
        "scenario": "8_adaptive_capacity_tuning.json",
        "scheduler": "dmclock_coarse",
        "extra_args": ["--runtime", "12"]
    },
    {
        "id": "8_adaptive_dmclock_coarse",
        "scenario": "8_adaptive_capacity_tuning.json",
        "scheduler": "dmclock_coarse",
        "extra_args": ["--adaptive", "--runtime", "12"]
    },
    {
        "id": "8_static_dmclock_fine",
        "scenario": "8_adaptive_capacity_tuning.json",
        "scheduler": "dmclock_fine",
        "extra_args": ["--runtime", "12"]
    },
    {
        "id": "8_adaptive_dmclock_fine",
        "scenario": "8_adaptive_capacity_tuning.json",
        "scheduler": "dmclock_fine",
        "extra_args": ["--adaptive", "--runtime", "12"]
    }
]

def run_experiment(exp):
    exp_id = exp["id"]
    scenario_path = SCENARIOS_DIR / exp["scenario"]
    json_out = NEW_RESULTS_DIR / f"{exp_id}.json"
    csv_out = NEW_RESULTS_DIR / f"{exp_id}.csv"

    cmd = [
        str(BENCHMARK_BIN),
        "--config", str(scenario_path),
        "--scheduler", exp["scheduler"],
        "--export_json", str(json_out),
        "--export_csv", str(csv_out)
    ] + exp["extra_args"]

    print(f"\n[*] Running: {exp_id}")
    print(f"    Command: {' '.join(cmd)}")
    sys.stdout.flush()

    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[-] FAILED with code {res.returncode}:\n{res.stderr}")
        return None

    print(res.stdout)
    if json_out.exists():
        with open(json_out) as f:
            return json.load(f)
    return None

def compare_results(old_data, new_data):
    comparison = {
        "old_throughput": old_data.get("overall_throughput_req_per_sec", 0.0),
        "new_throughput": new_data.get("overall_throughput_req_per_sec", 0.0),
        "old_jain_index": old_data.get("jains_fairness_index", 0.0),
        "new_jain_index": new_data.get("jains_fairness_index", 0.0),
        "tenants": []
    }
    old_tenants = {t["name"]: t for t in old_data.get("tenants", [])}
    new_tenants = {t["name"]: t for t in new_data.get("tenants", [])}

    for name, n_t in new_tenants.items():
        o_t = old_tenants.get(name, {})
        comparison["tenants"].append({
            "name": name,
            "old_accepted": o_t.get("accepted", 0),
            "new_accepted": n_t.get("accepted", 0),
            "old_drop_rate": o_t.get("drop_rate_pct", 0.0),
            "new_drop_rate": n_t.get("drop_rate_pct", 0.0),
            "old_p50": o_t.get("latencies_ms", {}).get("p50", 0.0),
            "new_p50": n_t.get("latencies_ms", {}).get("p50", 0.0),
            "old_p95": o_t.get("latencies_ms", {}).get("p95", 0.0),
            "new_p95": n_t.get("latencies_ms", {}).get("p95", 0.0),
        })
    return comparison

def main():
    print("================================================================================")
    print(" RE-RUNNING ALL RGW SCHEDULING BENCHMARK EXPERIMENTS & COMPARISON")
    print("================================================================================")

    all_comparisons = {}

    for exp in EXPERIMENTS:
        exp_id = exp["id"]
        new_res = run_experiment(exp)
        old_json_path = OLD_RESULTS_DIR / f"{exp_id}.json"

        if old_json_path.exists() and new_res is not None:
            with open(old_json_path) as f:
                old_res = json.load(f)
            all_comparisons[exp_id] = compare_results(old_res, new_res)
        elif new_res is not None:
            print(f"[!] No old result found for {exp_id} to compare against.")

    summary_file = NEW_RESULTS_DIR / "comparison_summary.json"
    with open(summary_file, "w") as f:
        json.dump(all_comparisons, f, indent=4)

    print("\n================================================================================")
    print(f"[+] All benchmark experiments completed successfully!")
    print(f"    Comparison summary saved to: {summary_file}")
    print("================================================================================")

if __name__ == "__main__":
    main()
