#!/usr/bin/env python3
import json
import glob
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent.resolve()
OLD_DIR = REPO_ROOT / "benchmark_suite/results"
NEW_DIR = REPO_ROOT / "benchmark_suite/results_new"

SCENARIO_GROUPS = [
    {
        "name": "Scenario 1: Classic Noisy Neighbor",
        "keys": ["1_noisy_neighbor_throttler", "1_noisy_neighbor_dmclock", "1_noisy_neighbor_none"]
    },
    {
        "name": "Scenario 2: Metadata Index Lock Storm",
        "keys": ["2_metadata_crawler_storm_throttler", "2_metadata_crawler_storm_dmclock", "2_metadata_crawler_storm_none"]
    },
    {
        "name": "Scenario 3: Severe Backend Congestion & Spikes",
        "keys": ["3_backend_congestion_and_spikes_throttler", "3_backend_congestion_and_spikes_dmclock", "3_backend_congestion_and_spikes_none"]
    },
    {
        "name": "Scenario 4: Multi-Tenant Tiered SLA Isolation",
        "keys": ["4_tiered_sla_isolation_throttler", "4_tiered_sla_isolation_dmclock", "4_tiered_sla_isolation_none"]
    },
    {
        "name": "Scenario 5: 503 Overload Under Client Exponential Backoff Retries",
        "keys": ["5_retry_storm_dynamics_throttler", "5_retry_storm_dynamics_dmclock"]
    },
    {
        "name": "Scenario 6: Intra-Class Tenant Starvation (8 Threads)",
        "keys": ["6_starvation_throttler", "6_starvation_dmclock_coarse", "6_starvation_dmclock_fine"]
    },
    {
        "name": "Scenario 6: Intra-Class Tenant Starvation (16 Threads)",
        "keys": ["6_starvation_throttler_t16", "6_starvation_dmclock_coarse_t16", "6_starvation_dmclock_fine_t16"]
    },
    {
        "name": "Scenario 6: Intra-Class Tenant Starvation (32 Threads)",
        "keys": ["6_starvation_throttler_t32", "6_starvation_dmclock_coarse_t32", "6_starvation_dmclock_fine_t32"]
    },
    {
        "name": "Scenario 7: 6-Tier Multi-Tenant Production QoS Hierarchy",
        "keys": ["7_multi_tier_throttler", "7_multi_tier_dmclock_coarse", "7_multi_tier_dmclock_fine"]
    },
    {
        "name": "Scenario 8: Closed-Loop Adaptive Capacity Tuning (OSD Scrub Spikes)",
        "keys": ["8_static_dmclock_coarse", "8_adaptive_dmclock_coarse", "8_static_dmclock_fine", "8_adaptive_dmclock_fine"]
    }
]

def load_json(p):
    if p.exists():
        with open(p) as f:
            return json.load(f)
    return None

def format_row(name, o_acc, n_acc, o_dr, n_dr, o_p50, n_p50, o_p95, n_p95):
    return f"| {name} | {o_acc} -> **{n_acc}** | {o_dr:.1f}% -> **{n_dr:.1f}%** | {o_p50:.1f}ms -> **{n_p50:.1f}ms** | {o_p95:.1f}ms -> **{n_p95:.1f}ms** |"

for group in SCENARIO_GROUPS:
    print(f"\n### {group['name']}\n")
    for key in group["keys"]:
        old_f = OLD_DIR / f"{key}.json"
        new_f = NEW_DIR / f"{key}.json"
        old_d = load_json(old_f)
        new_d = load_json(new_f)

        if not new_d:
            continue

        sched = new_d.get("scheduler", key)
        threads = new_d.get("server_threads", 8)
        j_old = old_d.get("jains_fairness_index", 0.0) if old_d else 0.0
        j_new = new_d.get("jains_fairness_index", 0.0)

        print(f"#### Scheduler: `{sched}` ({threads} Threads) | Jain Index: {j_old:.3f} -> **{j_new:.3f}**")
        print("| Tenant Persona | Accepted (Old -> New) | Drop Rate % (Old -> New) | p50 Latency (Old -> New) | p95 Latency (Old -> New) |")
        print("| :--- | :---: | :---: | :---: | :---: |")

        old_tenants = {t["name"]: t for t in old_d.get("tenants", [])} if old_d else {}
        for nt in new_d.get("tenants", []):
            name = nt["name"]
            ot = old_tenants.get(name, {})
            o_acc = ot.get("accepted", 0)
            n_acc = nt.get("accepted", 0)
            o_dr = ot.get("drop_rate_pct", 0.0)
            n_dr = nt.get("drop_rate_pct", 0.0)
            o_p50 = ot.get("p50_ms", 0.0)
            n_p50 = nt.get("p50_ms", 0.0)
            o_p95 = ot.get("p95_ms", 0.0)
            n_p95 = nt.get("p95_ms", 0.0)
            print(format_row(name, o_acc, n_acc, o_dr, n_dr, o_p50, n_p50, o_p95, n_p95))
        print()
