#!/usr/bin/env python3
"""
Automated Live Ceph Cluster A/B Comparative Benchmark:
Compares:
  Mode A: Upstream Baseline (Coarse 3-Queue dmClock, Static Capacity, rgw_dmclock_per_tenant_enabled=false, rgw_dmclock_adaptive_tuning=false)
  Mode B: Our Production Architecture (Fine-Grained per-tenant dmClock, Dynamic Profile Resolution, Closed-Loop Adaptive AIMD Controller)
"""

import os
import sys
import time
import subprocess
from live_s3_runner import S3Client, run_live_scenario

CEPH_ROOT = "/home/jbakamovic/development/ceph"
BUILD_DIR = "/home/jbakamovic/development/build-ceph/release"
CEPH_CONF = f"{BUILD_DIR}/ceph.conf"
CEPH_BIN = f"{BUILD_DIR}/bin/ceph"
RADOSGW_BIN = f"{BUILD_DIR}/bin/radosgw"
PID_FILE = f"{BUILD_DIR}/out/radosgw.8000.pid"
LOG_FILE = f"{BUILD_DIR}/out/radosgw.8000.log"
ASOK_FILE = f"{BUILD_DIR}/out/radosgw.8000.asok"

def stop_rgw():
    subprocess.run(["killall", "-9", "radosgw"], stderr=subprocess.DEVNULL)
    time.sleep(2.0)
    # Ensure port 8000 is free
    for _ in range(20):
        res = subprocess.run(["fuser", "8000/tcp"], capture_output=True, text=True)
        if not res.stdout.strip():
            break
        subprocess.run(["fuser", "-k", "-9", "8000/tcp"], stderr=subprocess.DEVNULL)
        time.sleep(0.5)

def start_rgw(per_tenant=False, adaptive=False):
    stop_rgw()
    cmd = [
        RADOSGW_BIN,
        "-c", CEPH_CONF,
        f"--log-file={LOG_FILE}",
        f"--admin-socket={ASOK_FILE}",
        f"--pid-file={PID_FILE}",
        "-n", "client.rgw.8000",
        "--rgw_frontends=beast port=8000",
        f"--rgw_dmclock_per_tenant_enabled={'true' if per_tenant else 'false'}",
        f"--rgw_dmclock_adaptive_tuning={'true' if adaptive else 'false'}",
        "--debug-rgw=0",
        "--debug-ms=0"
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Wait for RGW to respond
    client = S3Client("http://localhost:8000", "VIPACCESSKEY", "VIPSECRETKEY123456789")
    for _ in range(50):
        try:
            status, _, _ = client.request("GET", "")
            if status in [200, 403, 404]:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("Failed to start RGW")

def run_ceph_cmd(args):
    cmd = [CEPH_BIN, "-c", CEPH_CONF] + args
    return subprocess.run(cmd, capture_output=True, text=True)

def run_ab_scenario_1_noisy_neighbor(mode_name, runtime=6):
    tenants = [
        {
            "name": "VIP Latency-Sensitive App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.04,
            "payload_bytes": 1024
        },
        {
            "name": "Bulk Ingestion Bully",
            "access_key": "BULLYACCESSKEY",
            "secret_key": "BULLYSECRETKEY123456789",
            "bucket": "bully-bucket",
            "concurrency": 32,
            "pacing_s": 0.0,
            "is_bully": True,
            "payload_bytes": 65536
        },
        {
            "name": "Cluster Health Monitor",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-bucket",
            "concurrency": 1,
            "pacing_s": 0.25,
            "payload_bytes": 128
        }
    ]
    return run_live_scenario(f"Noisy Neighbor ({mode_name})", tenants, runtime=runtime)

def run_ab_scenario_2_dynamic_churn(mode_name, runtime=6):
    tenants = [
        {
            "name": "VIP Enterprise App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.04,
            "payload_bytes": 1024
        }
    ]
    for i in range(1, 9):
        tenants.append({
            "name": f"Dynamic Tenant {i:02d}",
            "access_key": f"DYNAMICKEY{i}",
            "secret_key": f"DYNAMICSECRET{i}",
            "bucket": f"dynamic-bucket-{i}",
            "concurrency": 4,
            "pacing_s": 0.02,
            "payload_bytes": 4096
        })
    return run_live_scenario(f"Dynamic Churn ({mode_name})", tenants, runtime=runtime)

def run_ab_scenario_3_osd_scrub(mode_name, runtime=8):
    run_ceph_cmd(["tell", "osd.0", "scrub"])
    tenants = [
        {
            "name": "VIP Latency-Sensitive App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.04,
            "payload_bytes": 1024
        },
        {
            "name": "Bulk Ingestion Bully",
            "access_key": "BULLYACCESSKEY",
            "secret_key": "BULLYSECRETKEY123456789",
            "bucket": "bully-bucket",
            "concurrency": 32,
            "pacing_s": 0.0,
            "is_bully": True,
            "payload_bytes": 65536
        }
    ]
    return run_live_scenario(f"OSD Scrub ({mode_name})", tenants, runtime=runtime)

def compute_jains_index(results, prefix="Dynamic Tenant"):
    tps_list = [r["tps"] for r in results if r["name"].startswith(prefix)]
    if not tps_list or sum(tps_list) == 0:
        return 0.0
    n = len(tps_list)
    sum_sq = sum(tps_list) ** 2
    sq_sum = sum(x**2 for x in tps_list)
    return sum_sq / (n * sq_sum) if sq_sum > 0 else 0.0

def print_comparison_table(title, res_a, res_b):
    print("\n" + "="*95)
    print(f" LIVE A/B COMPARISON: {title}")
    print("="*95)
    header = f"| {'Tenant Persona':<27} | {'[Mode A: Upstream]':<30} | {'[Mode B: Production]':<30} |"
    sub_hdr = f"| {'':<27} | {'TPS':<7} {'p50':<7} {'p99':<8} {'Drop%':<5} | {'TPS':<7} {'p50':<7} {'p99':<8} {'Drop%':<5} |"
    print(header)
    print(sub_hdr)
    print("+" + "-"*29 + "+" + "-"*32 + "+" + "-"*32 + "+")
    
    map_b = {r["name"]: r for r in res_b}
    for a in res_a:
        name = a["name"]
        b = map_b.get(name, {"tps": 0.0, "p50_ms": 0.0, "p99_ms": 0.0, "drop_pct": 0.0})
        col_a = f"{a['tps']:<7.1f} {a['p50_ms']:<5.1f}ms {a['p99_ms']:<6.1f}ms {a['drop_pct']:<5.1f}%"
        col_b = f"{b['tps']:<7.1f} {b['p50_ms']:<5.1f}ms {b['p99_ms']:<6.1f}ms {b['drop_pct']:<5.1f}%"
        print(f"| {name:<27} | {col_a:<30} | {col_b:<30} |")
    print("+" + "-"*29 + "+" + "-"*32 + "+" + "-"*32 + "+")

def main():
    print("#"*95)
    print(" EXECUTING LIVE CEPH CLUSTER A/B COMPARATIVE BENCHMARK")
    print(" Mode A: Upstream Baseline (Coarse dmClock, Static Capacity, No Per-Tenant Queues)")
    print(" Mode B: Production Architecture (Fine-Grained per-tenant dmClock + Closed-Loop AIMD)")
    print("#"*95)

    # -------------------------------------------------------------
    # RUN MODE A (UPSTREAM BASELINE)
    # -------------------------------------------------------------
    print("\n>>> [1/2] Launching RGW in Mode A: UPSTREAM BASELINE...")
    start_rgw(per_tenant=False, adaptive=False)
    print("  [*] Running Scenario 1: Noisy Neighbor under Upstream...")
    s1_res_a = run_ab_scenario_1_noisy_neighbor("Upstream", runtime=6)
    print("  [*] Running Scenario 2: Dynamic Tenant Churn under Upstream...")
    s2_res_a = run_ab_scenario_2_dynamic_churn("Upstream", runtime=6)
    print("  [*] Running Scenario 3: OSD Scrub Spikes under Upstream...")
    s3_res_a = run_ab_scenario_3_osd_scrub("Upstream", runtime=8)

    # -------------------------------------------------------------
    # RUN MODE B (OUR PRODUCTION ARCHITECTURE)
    # -------------------------------------------------------------
    print("\n>>> [2/2] Launching RGW in Mode B: OUR PRODUCTION ARCHITECTURE...")
    start_rgw(per_tenant=True, adaptive=True)
    print("  [*] Running Scenario 1: Noisy Neighbor under Production...")
    s1_res_b = run_ab_scenario_1_noisy_neighbor("Production", runtime=6)
    print("  [*] Running Scenario 2: Dynamic Tenant Churn under Production...")
    s2_res_b = run_ab_scenario_2_dynamic_churn("Production", runtime=6)
    print("  [*] Running Scenario 3: OSD Scrub Spikes under Production...")
    s3_res_b = run_ab_scenario_3_osd_scrub("Production", runtime=8)

    # -------------------------------------------------------------
    # PRINT SIDE-BY-SIDE COMPARATIVE TABLES
    # -------------------------------------------------------------
    print_comparison_table("Scenario 1: Noisy Neighbor Isolation", s1_res_a, s1_res_b)
    print_comparison_table("Scenario 2: Dynamic Tenant Churn & Surplus Sharing", s2_res_a, s2_res_b)
    jain_a = compute_jains_index(s2_res_a)
    jain_b = compute_jains_index(s2_res_b)
    print(f"[*] Dynamic Tenant Jain's Fairness Index -> Mode A (Upstream): {jain_a:.4f} | Mode B (Production): {jain_b:.4f}")
    print_comparison_table("Scenario 3: OSD Backend Scrub Congestion", s3_res_a, s3_res_b)

    print("\n[+] Direct Live Cluster A/B Comparison Completed Successfully!")

if __name__ == "__main__":
    main()
