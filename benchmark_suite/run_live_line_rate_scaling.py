#!/usr/bin/env python3
"""
Executes live end-to-end line-rate scaling and multi-tier proportional sharing
experiments against the live running Ceph cluster (BlueStore OSD + Beast RGW).
"""

import time
import subprocess
from live_s3_runner import run_live_scenario

CEPH_CONF = "/home/jbakamovic/development/build-ceph/release/ceph.conf"
ASOK = "/home/jbakamovic/development/build-ceph/release/out/radosgw.8000.asok"
CEPH_BIN = "/home/jbakamovic/development/build-ceph/release/bin/ceph"

def get_perf_stats():
    res = subprocess.run([CEPH_BIN, "-c", CEPH_CONF, "--admin-daemon", ASOK, "perf", "dump", "rgw_op"],
                         capture_output=True, text=True)
    return res.stdout

def run_live_tier_scaling(runtime=10):
    print("\n" + "="*95)
    print(" LIVE CLUSTER EXPERIMENT: 4-TIER PROPORTIONAL SHARING UNDER LIVE BLUESTORE I/O")
    print("="*95)
    
    tenants = [
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
    
    return run_live_scenario("Live Multi-Tier Proportional Sharing", tenants, runtime=runtime)

def run_live_saturation(runtime=10):
    print("\n" + "="*95)
    print(" LIVE CLUSTER EXPERIMENT: MAX LINE-RATE DISK SATURATION (64 Concurrent S3 Workers)")
    print("="*95)
    
    tenants = [
        {
            "name": "Tenant A (Concurrent Flood)",
            "access_key": "PLATINUMKEY",
            "secret_key": "PLATINUMSECRET123456789",
            "bucket": "sat-a-bucket",
            "concurrency": 32,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Tenant B (Concurrent Flood)",
            "access_key": "GOLDKEY",
            "secret_key": "GOLDSECRET123456789",
            "bucket": "sat-b-bucket",
            "concurrency": 32,
            "pacing_s": 0.0,
            "payload_bytes": 4096
        },
        {
            "name": "Control Health Probe",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "sat-health-bucket",
            "concurrency": 1,
            "pacing_s": 0.10,
            "payload_bytes": 128
        }
    ]
    
    return run_live_scenario("Live Max Line-Rate Saturation", tenants, runtime=runtime)

def main():
    print("#"*95)
    print(" STARTING LIVE CEPH CLUSTER LINE-RATE & MULTI-TIER PROPORTIONAL EXPERIMENTS")
    print("#"*95)
    
    run_live_tier_scaling(runtime=10)
    run_live_saturation(runtime=10)
    
    print("\n[+] All Live Cluster Line-Rate Scaling Experiments Completed Successfully!")

if __name__ == "__main__":
    main()
