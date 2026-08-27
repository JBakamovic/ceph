#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

BENCHMARK_BIN = Path(__file__).parent.parent / "build-ceph/release/bin/bench_rgw_scheduler"
if not BENCHMARK_BIN.exists():
    BENCHMARK_BIN = Path("/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler")
SUITE_DIR = Path(__file__).parent.resolve()
SCENARIOS_DIR = SUITE_DIR / "scenarios"
RESULTS_DIR = SUITE_DIR / "results"

SCENARIOS = {
    "1_noisy_neighbor": {
        "description": "Aggressive Bulk Uploader (16MB PUTs) vs Interactive CRUD (4KB) vs K8s Health Probe",
        "config": {
            "backend": {
                "cluster_base_latency_ms": 5.0,
                "cluster_capacity": 64,
                "congestion_factor": 3.0,
                "jitter_pct": 0.10,
                "spike_amplitude_ms": 0.0,
                "spike_duration_s": 1.0,
                "spike_interval_s": 5.0
            },
            "benchmark": {
                "allow_retries": False,
                "max_concurrent_requests": 128,
                "max_retries": 3,
                "runtime_seconds": 6,
                "scheduler": "throttler",
                "thread_count": 8
            },
            "dmclock": {
                "admin":    {"res": 15.0, "wgt": 100.0, "lim": 50.0},
                "auth":     {"res":  5.0, "wgt":  50.0, "lim": 25.0},
                "data":     {"res": 20.0, "wgt": 100.0, "lim": 100.0},
                "metadata": {"res": 10.0, "wgt":  50.0, "lim": 50.0}
            },
            "tenants": [
                {
                    "name": "Tenant A (Bully Bulk Ingest)",
                    "role": "Bulk Aggressor",
                    "client_id": "data",
                    "workers": 50,
                    "pacing_ms": 0.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"PutLarge": 80, "GetLarge": 20}
                },
                {
                    "name": "Tenant B (Interactive Web CRUD)",
                    "role": "Latency Sensitive",
                    "client_id": "data",
                    "workers": 15,
                    "pacing_ms": 2.0,
                    "pacing_distribution": "exponential",
                    "op_weights": {"GetSmall": 60, "PutSmall": 40}
                },
                {
                    "name": "Tenant C (K8s Liveness Probe)",
                    "role": "Control Plane Health",
                    "client_id": "admin",
                    "workers": 1,
                    "pacing_ms": 200.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"ProbeHealth": 100}
                }
            ]
        },
        "schedulers": ["throttler", "dmclock", "none"]
    },
    "2_metadata_crawler_storm": {
        "description": "Catalog Index Crawler (ListBucket lock storm) vs Media Streaming vs Mobile Apps",
        "config": {
            "backend": {
                "cluster_base_latency_ms": 5.0,
                "cluster_capacity": 64,
                "congestion_factor": 3.0,
                "jitter_pct": 0.10,
                "spike_amplitude_ms": 0.0,
                "spike_duration_s": 1.0,
                "spike_interval_s": 5.0
            },
            "benchmark": {
                "allow_retries": False,
                "max_concurrent_requests": 128,
                "max_retries": 3,
                "runtime_seconds": 6,
                "scheduler": "throttler",
                "thread_count": 8
            },
            "dmclock": {
                "admin":    {"res": 10.0, "wgt": 100.0, "lim": 50.0},
                "auth":     {"res":  5.0, "wgt":  50.0, "lim": 25.0},
                "data":     {"res": 30.0, "wgt": 100.0, "lim": 100.0},
                "metadata": {"res": 10.0, "wgt":  50.0, "lim": 40.0}
            },
            "tenants": [
                {
                    "name": "Tenant 1 (Search Crawler)",
                    "role": "Metadata Bucket Indexer",
                    "client_id": "metadata",
                    "workers": 30,
                    "pacing_ms": 0.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"ListBucket": 100}
                },
                {
                    "name": "Tenant 2 (Media Streamer)",
                    "role": "High Bandwidth Reads",
                    "client_id": "data",
                    "workers": 25,
                    "pacing_ms": 2.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"GetLarge": 100}
                },
                {
                    "name": "Tenant 3 (Mobile Client)",
                    "role": "Interactive Small Reads",
                    "client_id": "data",
                    "workers": 15,
                    "pacing_ms": 3.0,
                    "pacing_distribution": "exponential",
                    "op_weights": {"GetSmall": 100}
                }
            ]
        },
        "schedulers": ["throttler", "dmclock", "none"]
    },
    "3_backend_congestion_and_spikes": {
        "description": "Severe Backend Bottleneck (Capacity Knee = 32) with Periodic 30ms OSD Scrub Spikes",
        "config": {
            "backend": {
                "cluster_base_latency_ms": 6.0,
                "cluster_capacity": 32,
                "congestion_factor": 3.5,
                "jitter_pct": 0.10,
                "spike_amplitude_ms": 30.0,
                "spike_duration_s": 1.0,
                "spike_interval_s": 3.0
            },
            "benchmark": {
                "allow_retries": False,
                "max_concurrent_requests": 128,
                "max_retries": 3,
                "runtime_seconds": 6,
                "scheduler": "throttler",
                "thread_count": 8
            },
            "dmclock": {
                "admin":    {"res": 15.0, "wgt": 100.0, "lim": 50.0},
                "auth":     {"res":  5.0, "wgt":  50.0, "lim": 25.0},
                "data":     {"res": 20.0, "wgt":  80.0, "lim": 80.0},
                "metadata": {"res": 10.0, "wgt":  50.0, "lim": 50.0}
            },
            "tenants": [
                {
                    "name": "Tenant A (Batch Ingest)",
                    "role": "Bulk Ingest",
                    "client_id": "data",
                    "workers": 35,
                    "pacing_ms": 0.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"PutSmall": 50, "PutLarge": 50}
                },
                {
                    "name": "Tenant B (Online Queries)",
                    "role": "Online Read Traffic",
                    "client_id": "data",
                    "workers": 20,
                    "pacing_ms": 2.0,
                    "pacing_distribution": "exponential",
                    "op_weights": {"GetSmall": 80, "GetLarge": 20}
                },
                {
                    "name": "Tenant C (Liveness Probe)",
                    "role": "Health Probe",
                    "client_id": "admin",
                    "workers": 1,
                    "pacing_ms": 150.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"ProbeHealth": 100}
                }
            ]
        },
        "schedulers": ["throttler", "dmclock", "none"]
    },
    "4_tiered_sla_isolation": {
        "description": "Multi-Tenant SLA Isolation: Gold (admin) vs Silver (metadata) vs Bronze (data)",
        "config": {
            "backend": {
                "cluster_base_latency_ms": 5.0,
                "cluster_capacity": 48,
                "congestion_factor": 2.5,
                "jitter_pct": 0.10,
                "spike_amplitude_ms": 0.0,
                "spike_duration_s": 1.0,
                "spike_interval_s": 5.0
            },
            "benchmark": {
                "allow_retries": False,
                "max_concurrent_requests": 128,
                "max_retries": 3,
                "runtime_seconds": 6,
                "scheduler": "throttler",
                "thread_count": 8
            },
            "dmclock": {
                "admin":    {"res": 25.0, "wgt": 150.0, "lim": 100.0},
                "auth":     {"res": 10.0, "wgt":  60.0, "lim":  50.0},
                "metadata": {"res": 15.0, "wgt":  80.0, "lim":  60.0},
                "data":     {"res":  5.0, "wgt":  20.0, "lim":  40.0}
            },
            "tenants": [
                {
                    "name": "Tier 1 (Gold SLA - Premium)",
                    "role": "Mission Critical",
                    "client_id": "admin",
                    "workers": 15,
                    "pacing_ms": 2.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"GetSmall": 50, "PutSmall": 50}
                },
                {
                    "name": "Tier 2 (Silver SLA - Standard)",
                    "role": "Standard Business",
                    "client_id": "metadata",
                    "workers": 25,
                    "pacing_ms": 1.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"GetSmall": 50, "ListBucket": 50}
                },
                {
                    "name": "Tier 3 (Bronze SLA - Batch)",
                    "role": "Best Effort Bulk",
                    "client_id": "data",
                    "workers": 50,
                    "pacing_ms": 0.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"PutLarge": 70, "GetLarge": 30}
                }
            ]
        },
        "schedulers": ["throttler", "dmclock", "none"]
    },
    "5_retry_storm_dynamics": {
        "description": "503 Overload Under Client Exponential Backoff Retries vs Fast Drop",
        "config": {
            "backend": {
                "cluster_base_latency_ms": 5.0,
                "cluster_capacity": 48,
                "congestion_factor": 3.0,
                "jitter_pct": 0.10,
                "spike_amplitude_ms": 0.0,
                "spike_duration_s": 1.0,
                "spike_interval_s": 5.0
            },
            "benchmark": {
                "allow_retries": True,
                "max_concurrent_requests": 64,
                "max_retries": 4,
                "runtime_seconds": 6,
                "scheduler": "throttler",
                "thread_count": 8
            },
            "dmclock": {
                "admin":    {"res": 15.0, "wgt": 100.0, "lim": 50.0},
                "auth":     {"res":  5.0, "wgt":  50.0, "lim": 25.0},
                "data":     {"res": 20.0, "wgt": 100.0, "lim": 100.0},
                "metadata": {"res": 10.0, "wgt":  50.0, "lim": 50.0}
            },
            "tenants": [
                {
                    "name": "Batch Clients (With Retries)",
                    "role": "Heavy Load with Retries",
                    "client_id": "data",
                    "workers": 40,
                    "pacing_ms": 0.0,
                    "pacing_distribution": "constant",
                    "op_weights": {"PutLarge": 50, "PutSmall": 50}
                },
                {
                    "name": "API Clients (With Retries)",
                    "role": "Interactive with Retries",
                    "client_id": "data",
                    "workers": 25,
                    "pacing_ms": 2.0,
                    "pacing_distribution": "exponential",
                    "op_weights": {"GetSmall": 80, "PutSmall": 20}
                }
            ]
        },
        "schedulers": ["throttler", "dmclock"]
    }
}

def main():
    print(f"[*] Starting RGW Scheduler Benchmark Suite Runner...")
    os.makedirs(SCENARIOS_DIR, exist_ok=True)
    os.makedirs(RESULTS_DIR, exist_ok=True)

    suite_summary = {}

    for s_key, s_data in SCENARIOS.items():
        print(f"\n================================================================================")
        print(f" Running Scenario: {s_key}")
        print(f" Description: {s_data['description']}")
        print(f"================================================================================")

        # Write scenario config JSON
        scenario_file = SCENARIOS_DIR / f"{s_key}.json"
        with open(scenario_file, "w") as f:
            json.dump(s_data["config"], f, indent=4)

        suite_summary[s_key] = {
            "description": s_data["description"],
            "results": {}
        }

        for sched in s_data["schedulers"]:
            json_out = RESULTS_DIR / f"{s_key}_{sched}.json"
            csv_out = RESULTS_DIR / f"{s_key}_{sched}.csv"

            cmd = [
                BENCHMARK_BIN,
                "--config", str(scenario_file),
                "--scheduler", sched,
                "--export_json", str(json_out),
                "--export_csv", str(csv_out)
            ]

            # If scenario 5, check retries
            if s_key == "5_retry_storm_dynamics":
                cmd.append("--retries")

            print(f"\n[*] Executing: {' '.join(cmd)}")
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                print(f"[-] Error running benchmark:\n{res.stderr}")
                continue

            print(res.stdout)

            if json_out.exists():
                with open(json_out) as f:
                    suite_summary[s_key]["results"][sched] = json.load(f)

    # Save aggregated summary
    summary_file = RESULTS_DIR / "suite_summary.json"
    with open(summary_file, "w") as f:
        json.dump(suite_summary, f, indent=4)

    print(f"\n[+] Suite complete. Aggregated summary saved to: {summary_file}")

if __name__ == "__main__":
    main()
