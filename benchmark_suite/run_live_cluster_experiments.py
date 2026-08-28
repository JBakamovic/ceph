#!/usr/bin/env python3
"""
Executes a comprehensive catalog of live end-to-end multi-tenant S3 benchmark
experiments against the local vstart Ceph cluster.
"""

import time
import subprocess
from live_s3_runner import run_live_scenario

CEPH_CONF = "/home/jbakamovic/development/build-ceph/release/ceph.conf"
CEPH_BIN = "/home/jbakamovic/development/build-ceph/release/bin/ceph"

def run_ceph_cmd(args):
    cmd = [CEPH_BIN, "-c", CEPH_CONF] + args
    return subprocess.run(cmd, capture_output=True, text=True)

def experiment_1_noisy_neighbor(runtime=8):
    print("\n" + "#"*80)
    print(" LIVE TEST 1: NOISY NEIGHBOR ISOLATION")
    print("#"*80)
    tenants = [
        {
            "name": "VIP Latency-Sensitive App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.05,
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
    run_live_scenario("Scenario 1: Noisy Neighbor under Live BlueStore I/O", tenants, runtime=runtime)

def experiment_2_metadata_crawler_storm(runtime=8):
    print("\n" + "#"*80)
    print(" LIVE TEST 2: METADATA CRAWLER STORM VS MEDIA STREAMER VS MOBILE")
    print("#"*80)
    tenants = [
        {
            "name": "Metadata Crawler (Head/List)",
            "access_key": "CRAWLERKEY",
            "secret_key": "CRAWLERSECRET123456789",
            "bucket": "crawler-bucket",
            "concurrency": 24,
            "pacing_s": 0.0,
            "payload_bytes": 0
        },
        {
            "name": "Media Streamer (1MB Writes)",
            "access_key": "STREAMERKEY",
            "secret_key": "STREAMERSECRET123456789",
            "bucket": "media-bucket",
            "concurrency": 8,
            "pacing_s": 0.05,
            "payload_bytes": 131072
        },
        {
            "name": "Mobile Client (Interactive)",
            "access_key": "MOBILEKEY",
            "secret_key": "MOBILESECRET123456789",
            "bucket": "mobile-bucket",
            "concurrency": 4,
            "pacing_s": 0.02,
            "payload_bytes": 4096
        }
    ]
    run_live_scenario("Scenario 2: Metadata Lock Storm Isolation", tenants, runtime=runtime)

def experiment_3_tiered_sla_isolation(runtime=8):
    print("\n" + "#"*80)
    print(" LIVE TEST 3: 5-TIER MULTI-TENANT SLA QOS")
    print("#"*80)
    tenants = [
        {
            "name": "Tier 1: Platinum VIP",
            "access_key": "PLATINUMKEY",
            "secret_key": "PLATINUMSECRET123456789",
            "bucket": "platinum-bucket",
            "concurrency": 16,
            "pacing_s": 0.01,
            "payload_bytes": 2048
        },
        {
            "name": "Tier 2: Gold Standard",
            "access_key": "GOLDKEY",
            "secret_key": "GOLDSECRET123456789",
            "bucket": "gold-bucket",
            "concurrency": 12,
            "pacing_s": 0.02,
            "payload_bytes": 4096
        },
        {
            "name": "Tier 3: Silver Standard",
            "access_key": "SILVERKEY",
            "secret_key": "SILVERSECRET123456789",
            "bucket": "silver-bucket",
            "concurrency": 8,
            "pacing_s": 0.05,
            "payload_bytes": 8192
        },
        {
            "name": "Tier 4: Bronze Standard",
            "access_key": "BRONZEKEY",
            "secret_key": "BRONZESECRET123456789",
            "bucket": "bronze-bucket",
            "concurrency": 8,
            "pacing_s": 0.10,
            "payload_bytes": 16384
        },
        {
            "name": "Tier 5: Free Bulk",
            "access_key": "FREEKEY",
            "secret_key": "FREESECRET123456789",
            "bucket": "free-bucket",
            "concurrency": 16,
            "pacing_s": 0.0,
            "payload_bytes": 32768
        }
    ]
    run_live_scenario("Scenario 4 & 7: Tiered Multi-Tenant SLA Allocation", tenants, runtime=runtime)

def experiment_4_dynamic_churn(runtime=8):
    print("\n" + "#"*80)
    print(" LIVE TEST 4: DYNAMIC TENANT SURPLUS SHARING & CHURN")
    print("#"*80)
    tenants = [
        {
            "name": "VIP Enterprise App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.05,
            "payload_bytes": 1024
        },
        {
            "name": "System Health Monitor",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-bucket",
            "concurrency": 1,
            "pacing_s": 0.25,
            "payload_bytes": 128
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
    run_live_scenario("Scenario 14: Dynamic Tenant Churn (8 Dynamic Tenants + VIP)", tenants, runtime=runtime)

def experiment_5_intra_class_starvation(runtime=8):
    print("\n" + "#"*80)
    print(" LIVE TEST 5: INTRA-CLASS S3 DATA TENANT FAIRNESS")
    print("#"*80)
    tenants = [
        {
            "name": "Tenant A (Bully Data Flood)",
            "access_key": "BULLYACCESSKEY",
            "secret_key": "BULLYSECRETKEY123456789",
            "bucket": "bully-bucket",
            "concurrency": 32,
            "pacing_s": 0.0,
            "is_bully": True,
            "payload_bytes": 32768
        },
        {
            "name": "Tenant B (Interactive Data VIP)",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.03,
            "payload_bytes": 1024
        },
        {
            "name": "Tenant C (Health Probe)",
            "access_key": "HEALTHACCESSKEY",
            "secret_key": "HEALTHSECRETKEY123456789",
            "bucket": "health-bucket",
            "concurrency": 1,
            "pacing_s": 0.25,
            "payload_bytes": 128
        }
    ]
    run_live_scenario("Scenario 6: Intra-Class S3 Tenant Starvation Isolation", tenants, runtime=runtime)

def experiment_6_osd_scrub_adaptive(runtime=10):
    print("\n" + "#"*80)
    print(" LIVE TEST 6: OSD SCRUB CONGESTION & ADAPTIVE AIMD FEEDBACK")
    print("#"*80)
    print("[*] Injecting live OSD scrub on osd.0...")
    run_ceph_cmd(["tell", "osd.0", "scrub"])
    
    tenants = [
        {
            "name": "VIP Latency-Sensitive App",
            "access_key": "VIPACCESSKEY",
            "secret_key": "VIPSECRETKEY123456789",
            "bucket": "vip-bucket",
            "concurrency": 8,
            "pacing_s": 0.05,
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
    run_live_scenario("Scenario 15: OSD Scrub Distress & Closed-Loop AIMD", tenants, runtime=runtime)

def main():
    print("Starting Comprehensive Live Ceph Cluster E2E Benchmark Catalog...")
    experiment_1_noisy_neighbor(runtime=6)
    experiment_2_metadata_crawler_storm(runtime=6)
    experiment_3_tiered_sla_isolation(runtime=6)
    experiment_4_dynamic_churn(runtime=6)
    experiment_5_intra_class_starvation(runtime=6)
    experiment_6_osd_scrub_adaptive(runtime=8)
    print("\n[+] Full 6-Scenario Live Cluster E2E S3 Catalog Completed Successfully!")

if __name__ == "__main__":
    main()
