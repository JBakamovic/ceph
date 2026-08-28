#!/usr/bin/env python3
"""
Master Live Cluster A/B Evaluation Suite.
Directly compares 'throttler' vs 'dmclock' (fine-grained per-tenant)
across a comprehensive 6-workload catalog on the actual live running Ceph cluster.
"""

import sys
import os
import time
import subprocess
import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from live_s3_runner import run_live_scenario

CEPH_CONF = "/home/jbakamovic/development/build-ceph/release/ceph.conf"
ASOK = "/home/jbakamovic/development/build-ceph/release/out/radosgw.8000.asok"
PID_FILE = "/home/jbakamovic/development/build-ceph/release/out/radosgw.8000.pid"
LOG_FILE = "/home/jbakamovic/development/build-ceph/release/out/radosgw.8000.log"
LUAROCKS = "/home/jbakamovic/development/build-ceph/release/out/radosgw.8000.luarocks"
RGW_BIN = "/home/jbakamovic/development/build-ceph/release/bin/radosgw"
CEPH_BIN = "/home/jbakamovic/development/build-ceph/release/bin/ceph"

def restart_rgw(scheduler_type="dmclock", per_tenant="true", adaptive="false"):
    print(f"\n[*] Restarting live Ceph RGW: scheduler={scheduler_type}, per_tenant={per_tenant}, adaptive={adaptive}...")
    
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                pid = int(f.read().strip())
            os.kill(pid, 9)
        except Exception:
            pass
    subprocess.run(["pkill", "-9", "-f", "bin/radosgw"], stderr=subprocess.DEVNULL)
    time.sleep(1.5)

    cmd = [
        RGW_BIN,
        "-c", CEPH_CONF,
        f"--log-file={LOG_FILE}",
        f"--admin-socket={ASOK}",
        f"--pid-file={PID_FILE}",
        f"--rgw_luarocks_location={LUAROCKS}",
        "--debug-rgw=1",
        "-n", "client.rgw.8000",
        "--rgw_frontends=beast port=8000",
        f"--rgw_scheduler_type={scheduler_type}",
        f"--rgw_dmclock_per_tenant_enabled={per_tenant}",
        f"--rgw_dmclock_adaptive_tuning={adaptive}",
        "--rgw_dmclock_data_res=5000",
        "--rgw_dmclock_data_wgt=5000",
        "--rgw_dmclock_data_lim=0",
        "--rgw_dmclock_metadata_res=5000",
        "--rgw_dmclock_metadata_wgt=5000",
        "--rgw_dmclock_admin_res=5000",
        "--rgw_dmclock_admin_wgt=5000"
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    for attempt in range(25):
        time.sleep(0.5)
        try:
            r = requests.get("http://localhost:8000/", timeout=1.0)
            if r.status_code == 200:
                print(f"[+] RGW active and responding with scheduler={scheduler_type} (attempt {attempt+1})")
                return True
        except Exception:
            pass
    raise RuntimeError(f"Failed to bring up radosgw with scheduler={scheduler_type}")

# Workload Definitions
def get_workload_1_noisy_neighbor():
    return [
        {
            "name": "VIP Latency-Sensitive App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-live-bucket",
            "concurrency": 4,
            "pacing_s": 0.05,
            "payload_bytes": 1024
        },
        {
            "name": "Bulk Ingestion Bully",
            "access_key": "BULLYACCESSKEY",
            "secret_key": "BULLYSECRETKEY123456789",
            "bucket": "bully-live-bucket",
            "concurrency": 24,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Cluster Health Monitor",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-live-bucket",
            "concurrency": 1,
            "pacing_s": 0.20,
            "payload_bytes": 128
        }
    ]

def get_workload_2_proportional_tiers():
    return [
        {
            "name": "Tier 1: Platinum VIP",
            "access_key": "PLATINUMKEY",
            "secret_key": "PLATINUMSECRET123456789",
            "bucket": "platinum-live-bucket",
            "concurrency": 24,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Tier 2: Gold Standard",
            "access_key": "GOLDKEY",
            "secret_key": "GOLDSECRET123456789",
            "bucket": "gold-live-bucket",
            "concurrency": 16,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Tier 3: Silver Standard",
            "access_key": "SILVERKEY",
            "secret_key": "SILVERSECRET123456789",
            "bucket": "silver-live-bucket",
            "concurrency": 8,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Tier 4: Bronze Standard",
            "access_key": "BRONZEKEY",
            "secret_key": "BRONZESECRET123456789",
            "bucket": "bronze-live-bucket",
            "concurrency": 4,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "System Health Monitor",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-live-bucket",
            "concurrency": 1,
            "pacing_s": 0.10,
            "payload_bytes": 128
        }
    ]

def get_workload_3_dynamic_churn():
    tenants = [
        {
            "name": "VIP Enterprise App",
            "access_key": "PLATINUMKEY",
            "secret_key": "PLATINUMSECRET123456789",
            "bucket": "platinum-live-bucket",
            "concurrency": 4,
            "pacing_s": 0.02,
            "payload_bytes": 1024
        },
        {
            "name": "System Health Monitor",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-live-bucket",
            "concurrency": 1,
            "pacing_s": 0.20,
            "payload_bytes": 128
        }
    ]
    for i in range(1, 9):
        tenants.append({
            "name": f"Dynamic Tenant {i:02d}",
            "access_key": f"DYNAMICKEY{i}",
            "secret_key": f"DYNAMICSECRET{i}",
            "bucket": f"dynamic-live-bucket-{i}",
            "concurrency": 2,
            "pacing_s": 0.01,
            "payload_bytes": 1024
        })
    return tenants

def get_workload_4_metadata_crawler():
    return [
        {
            "name": "Metadata Crawler (Small Ops)",
            "access_key": "CRAWLERKEY",
            "secret_key": "CRAWLERSECRET123456789",
            "bucket": "crawler-live-bucket",
            "concurrency": 16,
            "pacing_s": 0.0,
            "payload_bytes": 256
        },
        {
            "name": "Media Streamer (128KB Writes)",
            "access_key": "STREAMERKEY",
            "secret_key": "STREAMERSECRET123456789",
            "bucket": "streamer-live-bucket",
            "concurrency": 8,
            "pacing_s": 0.0,
            "payload_bytes": 131072
        },
        {
            "name": "Mobile Client (Interactive)",
            "access_key": "MOBILEKEY",
            "secret_key": "MOBILESECRET123456789",
            "bucket": "mobile-live-bucket",
            "concurrency": 2,
            "pacing_s": 0.05,
            "payload_bytes": 1024
        }
    ]

def get_workload_5_intra_class_starvation():
    return [
        {
            "name": "Tenant A (Bully Data Flood)",
            "access_key": "BULLYACCESSKEY",
            "secret_key": "BULLYSECRETKEY123456789",
            "bucket": "bully-live-bucket",
            "concurrency": 24,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Tenant B (Interactive Data VIP)",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-live-bucket",
            "concurrency": 4,
            "pacing_s": 0.02,
            "payload_bytes": 1024
        },
        {
            "name": "Tenant C (Health Probe)",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-live-bucket",
            "concurrency": 1,
            "pacing_s": 0.20,
            "payload_bytes": 128
        }
    ]

def format_ab_table(title, throttler_res, dmclock_res):
    print("\n" + "="*110)
    print(f" LIVE A/B COMPARISON: {title}")
    print("="*110)
    header = f"| {'Tenant Persona':<30} | {'Throttler TPS':<14} | {'Throttler p50':<14} | {'dmClock TPS':<14} | {'dmClock p50':<14} | {'TPS Ratio':<10} |"
    sep = "+" + "-"*32 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*16 + "+" + "-"*12 + "+"
    print(sep)
    print(header)
    print(sep)
    
    t_map = {r["name"]: r for r in throttler_res}
    d_map = {r["name"]: r for r in dmclock_res}
    
    for name in t_map:
        t = t_map[name]
        d = d_map.get(name, {"tps": 0.0, "p50_ms": 0.0})
        t_tps = t["tps"]
        d_tps = d["tps"]
        ratio = f"{d_tps / t_tps:.2f}x" if t_tps > 0 else "N/A"
        row = f"| {name:<30} | {t_tps:<14.1f} | {t['p50_ms']:<12.1f}ms | {d_tps:<14.1f} | {d['p50_ms']:<12.1f}ms | {ratio:<10} |"
        print(row)
    print(sep)

def main():
    print("#"*110)
    print(" STARTING EXPANDED 5-WORKLOAD LIVE CEPH CLUSTER A/B BENCHMARK (THROTTLER VS DMCLOCK)")
    print("#"*110)

    workloads = [
        ("Workload 1: Noisy Neighbor Overload", get_workload_1_noisy_neighbor(), 8),
        ("Workload 2: 4-Tier Proportional Sharing", get_workload_2_proportional_tiers(), 8),
        ("Workload 3: Dynamic Tenant Churn", get_workload_3_dynamic_churn(), 8),
        ("Workload 4: Metadata Crawler vs Media Streamer", get_workload_4_metadata_crawler(), 8),
        ("Workload 5: Intra-Class Data Starvation Isolation", get_workload_5_intra_class_starvation(), 8)
    ]

    all_results = {}

    for sched in ["throttler", "dmclock"]:
        per_tenant = "true" if sched == "dmclock" else "false"
        restart_rgw(scheduler_type=sched, per_tenant=per_tenant)
        all_results[sched] = {}
        
        for name, config, runtime in workloads:
            print(f"\n>>> Running '{name}' under live Ceph scheduler: {sched} ({runtime}s)...")
            res = run_live_scenario(f"{name} [{sched}]", config, runtime=runtime)
            all_results[sched][name] = res

    # Generate Final Side-by-Side A/B Reports
    print("\n\n" + "#"*110)
    print(" FINAL EXPANDED LIVE CEPH CLUSTER A/B COMPARATIVE RESULTS")
    print("#"*110)

    for name, _, _ in workloads:
        format_ab_table(name, all_results["throttler"][name], all_results["dmclock"][name])

    out_file = "/home/jbakamovic/development/ceph/benchmark_suite/results/LIVE_CLUSTER_AB_REPORT.md"
    with open(out_file, "w") as f:
        f.write("# Live Ceph Cluster A/B Benchmark Report: Throttler vs Fine-Grained dmClock\n\n")
        f.write("Executed directly on the live running Ceph cluster (1 MON, 1 MGR, 1 BlueStore OSD, Beast RGW).\n\n")
        for name, _, _ in workloads:
            t_res = all_results["throttler"][name]
            d_res = all_results["dmclock"][name]
            f.write(f"### {name}\n\n")
            f.write("| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |\n")
            f.write("| :--- | :--- | :--- | :--- | :--- | :--- |\n")
            t_map = {r["name"]: r for r in t_res}
            d_map = {r["name"]: r for r in d_res}
            for t_name in t_map:
                t = t_map[t_name]
                d = d_map.get(t_name, {"tps": 0.0, "p50_ms": 0.0})
                t_tps = t["tps"]
                d_tps = d["tps"]
                ratio = f"{d_tps / t_tps:.2f}x" if t_tps > 0 else "N/A"
                f.write(f"| {t_name} | {t_tps:.1f} | {t['p50_ms']:.1f}ms | {d_tps:.1f} | {d['p50_ms']:.1f}ms | {ratio} |\n")
            f.write("\n")

    print(f"\n[+] Full live report saved to {out_file}")

if __name__ == "__main__":
    main()
