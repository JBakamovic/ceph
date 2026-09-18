#!/usr/bin/env python3
"""
test_degraded_pool_contention.py - Test cross-pool degraded PG contention in Ceph RGW.

Tests the hypothesis:
"Degraded PGs cause many RADOS ops in flight, and ops to the good pool are slowed down by that contention.
 Hypothesis is that RGW clogs up because synchronous librados requests wait for completion and can't process new requests."
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("degraded_test")

ACCESS_KEY = "0555b35654ad1656d804"
SECRET_KEY = "h7GhxuBLTrlhVUyxSPUKUV8r/2EI4ngqJxD7iBdBYLhwluN30JaT3Q=="
PAYLOAD_1K = b"X" * 1024


def get_admin_socket_perf(asok_path="/tmp/radosgw.8000.asok"):
    cmd = ["/home/ultron/development/build-ceph/release/bin/ceph", "-c",
           "/home/ultron/development/build-ceph/release/ceph.conf",
           "--admin-daemon", asok_path, "perf", "dump"]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        data = json.loads(res.stdout)
        obj = data.get("objecter", {})
        throt = data.get("throttle-objecter_ops", {})
        return {
            "objecter_op_active": obj.get("op_active", 0),
            "objecter_op_inflight": obj.get("op_inflight", 0),
            "objecter_op_send": obj.get("op_send", 0),
            "objecter_op_reply": obj.get("op_reply", 0),
            "throttle_ops_val": throt.get("val", 0),
            "throttle_ops_max": throt.get("max", 0),
            "throttle_ops_wait_count": throt.get("wait", {}).get("avgcount", 0),
            "throttle_ops_wait_sum": throt.get("wait", {}).get("sum", 0.0),
        }
    except Exception as e:
        logger.warning(f"Failed to fetch perf dump from {asok_path}: {e}")
        return {}


def get_s3_client(endpoint, timeout=10.0, max_pool=100):
    cfg = Config(
        connect_timeout=3.0,
        read_timeout=timeout,
        retries={"max_attempts": 0},
        max_pool_connections=max_pool,
    )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        config=cfg,
    )


def run_good_worker(worker_id, endpoint, bucket, duration, rate, client_to, stop_evt, results):
    s3 = get_s3_client(endpoint, timeout=client_to)
    interval = 1.0 / max(rate, 0.1)
    seq = 0
    start_time = time.time()
    
    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        key = f"good_w{worker_id}_{seq}.dat"
        t0 = time.time()
        status = 0
        err_msg = ""
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=PAYLOAD_1K)
            status = 200
        except ClientError as ce:
            status = ce.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)
            err_msg = str(ce)
        except (ReadTimeoutError, EndpointConnectionError) as te:
            status = 408
            err_msg = str(te)
        except Exception as e:
            status = 599
            err_msg = str(e)
        t1 = time.time()
        
        results.append({
            "pool": "good",
            "worker": worker_id,
            "seq": seq,
            "start": t0,
            "end": t1,
            "latency": t1 - t0,
            "status": status,
            "error": err_msg,
        })
        
        elapsed = t1 - t0
        if elapsed < interval:
            time.sleep(interval - elapsed)


def run_degraded_worker(worker_id, endpoint, bucket, duration, client_to, stop_evt, results):
    s3 = get_s3_client(endpoint, timeout=client_to)
    seq = 0
    start_time = time.time()
    
    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        key = f"deg_w{worker_id}_{seq}.dat"
        t0 = time.time()
        status = 0
        err_msg = ""
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=PAYLOAD_1K)
            status = 200
        except ClientError as ce:
            status = ce.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 500)
            err_msg = str(ce)
        except (ReadTimeoutError, EndpointConnectionError) as te:
            status = 408
            err_msg = str(te)
        except Exception as e:
            status = 599
            err_msg = str(e)
        t1 = time.time()
        
        results.append({
            "pool": "degraded",
            "worker": worker_id,
            "seq": seq,
            "start": t0,
            "end": t1,
            "latency": t1 - t0,
            "status": status,
            "error": err_msg,
        })


def calculate_latencies(lat_list):
    if not lat_list:
        return {"p50": 0, "p90": 0, "p95": 0, "p99": 0, "max": 0, "avg": 0}
    s = sorted(lat_list)
    n = len(s)
    return {
        "p50": s[int(n * 0.50)],
        "p90": s[min(int(n * 0.90), n - 1)],
        "p95": s[min(int(n * 0.95), n - 1)],
        "p99": s[min(int(n * 0.99), n - 1)],
        "max": s[-1],
        "avg": sum(s) / n,
    }


def main():
    parser = argparse.ArgumentParser(description="Reproduce degraded PG contention")
    parser.add_argument("--endpoint", default="http://10.0.0.2:8000")
    parser.add_argument("--good-bucket", default="bucket-good")
    parser.add_argument("--degraded-bucket", default="bucket-degraded")
    parser.add_argument("--good-workers", type=int, default=5)
    parser.add_argument("--good-rate", type=float, default=5.0)
    parser.add_argument("--degraded-workers", type=int, default=50)
    parser.add_argument("--duration", type=float, default=20.0)
    parser.add_argument("--good-timeout", type=float, default=10.0)
    parser.add_argument("--degraded-timeout", type=float, default=30.0)
    parser.add_argument("--output-dir", default="/home/ultron/development/49")
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--description", default="")
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    out_json = os.path.join(args.output_dir, f"{args.run_name}.json")
    
    logger.info(f"=== Starting Run: {args.run_name} ===")
    logger.info(f"Good workers: {args.good_workers} (rate {args.good_rate}/s, TO {args.good_timeout}s)")
    logger.info(f"Degraded workers: {args.degraded_workers} (TO {args.degraded_timeout}s)")
    logger.info(f"Duration: {args.duration}s")

    perf_pre = get_admin_socket_perf()
    logger.info(f"Pre-test perf: {perf_pre}")

    import threading
    stop_evt = threading.Event()
    good_results = []
    degraded_results = []

    t_start = time.time()
    with ThreadPoolExecutor(max_workers=args.good_workers + args.degraded_workers) as executor:
        futures = []
        # Launch good workers
        for i in range(args.good_workers):
            f = executor.submit(run_good_worker, i, args.endpoint, args.good_bucket,
                                args.duration, args.good_rate, args.good_timeout, stop_evt, good_results)
            futures.append(f)
            
        # Launch degraded workers if any
        for i in range(args.degraded_workers):
            f = executor.submit(run_degraded_worker, i, args.endpoint, args.degraded_bucket,
                                args.duration, args.degraded_timeout, stop_evt, degraded_results)
            futures.append(f)

        for f in futures:
            f.result()
            
    t_end = time.time()
    perf_post = get_admin_socket_perf()
    logger.info(f"Post-test perf: {perf_post}")

    # Process metrics
    good_completed = [r for r in good_results if r["status"] == 200]
    good_timeouts = [r for r in good_results if r["status"] == 408]
    good_errors = [r for r in good_results if r["status"] not in (200, 408)]
    good_lats = [r["latency"] for r in good_completed]
    good_lat_stats = calculate_latencies(good_lats)

    deg_completed = [r for r in degraded_results if r["status"] == 200]
    deg_timeouts = [r for r in degraded_results if r["status"] == 408]
    deg_errors = [r for r in degraded_results if r["status"] not in (200, 408)]
    deg_lats = [r["latency"] for r in deg_completed]
    deg_lat_stats = calculate_latencies(deg_lats)

    summary = {
        "run_name": args.run_name,
        "description": args.description,
        "timestamp": datetime.now().isoformat(),
        "duration_actual": t_end - t_start,
        "good_pool": {
            "total_attempts": len(good_results),
            "completed_200": len(good_completed),
            "timeouts_408": len(good_timeouts),
            "other_errors": len(good_errors),
            "throughput_ops_per_sec": len(good_completed) / max(t_end - t_start, 0.1),
            "latency": good_lat_stats,
        },
        "degraded_pool": {
            "total_attempts": len(degraded_results),
            "completed_200": len(deg_completed),
            "timeouts_408": len(deg_timeouts),
            "other_errors": len(deg_errors),
            "latency": deg_lat_stats,
        },
        "perf_delta": {
            "objecter_op_send": perf_post.get("objecter_op_send", 0) - perf_pre.get("objecter_op_send", 0),
            "objecter_op_reply": perf_post.get("objecter_op_reply", 0) - perf_pre.get("objecter_op_reply", 0),
            "throttle_wait_count_delta": perf_post.get("throttle_ops_wait_count", 0) - perf_pre.get("throttle_ops_wait_count", 0),
            "throttle_wait_sum_sec": perf_post.get("throttle_ops_wait_sum", 0.0) - perf_pre.get("throttle_ops_wait_sum", 0.0),
            "post_op_active": perf_post.get("objecter_op_active", 0),
            "post_throttle_val": perf_post.get("throttle_ops_val", 0),
        },
    }

    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    logger.info(f"Saved run summary to {out_json}")
    logger.info(f"Good Pool Summary: {summary['good_pool']}")
    logger.info(f"Degraded Pool Summary: {summary['degraded_pool']}")
    logger.info(f"Perf Delta: {summary['perf_delta']}")


if __name__ == "__main__":
    main()
