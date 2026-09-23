#!/usr/bin/env python3
"""
test_fast_fail_circuit_breaker.py - Automated Multi-Concurrency Benchmark and
Forensics Harness for RGW Upstream Fast-Fail Circuit Breaker (PR 4).

Features:
1. Multi-level concurrency sweeps across degraded write workers (e.g. 10, 30, 60, 100).
2. Controlled degraded fault injection (OSD 2 stopped on degraded_pool min_size=3).
3. Concurrent execution of:
   - Degraded write workers (targeting bucket-degraded)
   - Good write workers (targeting bucket-good)
   - Control / probe workers (lightweight listing/reads on bucket-good)
4. Forensic system inspection via remote 'pstack' on Jarvis (10.0.0.2) during peak contention.
5. Server-side log telemetry parsing (radosgw.8000.log).
6. Guaranteed cluster restoration between runs.
7. Structured JSON metrics output preservation in /home/ultron/development/49/.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
except ImportError:
    print("Error: boto3 is required. Install via: pip install boto3", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("fast_fail_test")

CEPH_BIN = "/home/ultron/development/build-ceph/release/bin/ceph"
CEPH_CONF = "/home/ultron/development/build-ceph/release/ceph.conf"
CEPH_OSD_BIN = "/home/ultron/development/build-ceph/release/bin/ceph-osd"
BUILD_DIR = "/home/ultron/development/build-ceph/release"
ASOK_PATH = "/tmp/radosgw.8000.asok"
JARVIS_HOST = "10.0.0.2"
JARVIS_LOG_PATH = "/home/jarvis/development/build-ceph/release/out/radosgw.8000.log"
DEFAULT_OUTPUT_DIR = "/home/ultron/development/49"

ACCESS_KEY = "0555b35654ad1656d804"
SECRET_KEY = "h7GhxuBLTrlhVUyxSPUKUV8r/2EI4ngqJxD7iBdBYLhwluN30JaT3Q=="
PAYLOAD_1K = b"X" * 1024


# ==============================================================================
# 1. Cluster Management & Fault Injection
# ==============================================================================
class ClusterManager:
    """Manages cluster health, fault injection, and daemon controls."""

    def __init__(self, ceph_bin=CEPH_BIN, ceph_conf=CEPH_CONF, asok_path=ASOK_PATH):
        self.ceph_bin = ceph_bin
        self.ceph_conf = ceph_conf
        self.asok_path = asok_path

    def run_ceph(self, *args, check=True):
        cmd = [self.ceph_bin, "-c", self.ceph_conf] + list(args)
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and res.returncode != 0:
            raise RuntimeError(f"Ceph command failed ({' '.join(cmd)}):\n{res.stderr.strip()}")
        return res.stdout.strip()

    def run_asok(self, *args, check=True):
        cmd = [self.ceph_bin, "-c", self.ceph_conf, "--admin-daemon", self.asok_path] + list(args)
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if check and res.returncode != 0:
            raise RuntimeError(f"Admin socket command failed ({' '.join(cmd)}):\n{res.stderr.strip()}")
        return res.stdout.strip()

    def check_cluster_health(self):
        raw = self.run_ceph("status", "-f", "json")
        idx = raw.find("{")
        if idx >= 0:
            raw = raw[idx:]
        data = json.loads(raw)
        osdmap = data.get("osdmap", {})
        pgmap = data.get("pgmap", {})
        osds_up = osdmap.get("num_up_osds", 0)
        osds_in = osdmap.get("num_in_osds", 0)
        total_pgs = pgmap.get("num_pgs", 0)
        pgs_clean = 0
        for state in pgmap.get("pgs_by_state", []):
            if "active+clean" in state.get("state_name", ""):
                pgs_clean += state.get("count", 0)
        return {
            "osds_up": osds_up,
            "osds_in": osds_in,
            "total_pgs": total_pgs,
            "pgs_clean": pgs_clean,
            "health": data.get("health", {}).get("status", "UNKNOWN"),
            "raw": data,
        }

    def inject_degraded_fault(self, degraded_pool="degraded_pool", osd_id=2, timeout=30):
        logger.info(f"Injecting fault: setting {degraded_pool} min_size=3...")
        self.run_ceph("osd", "pool", "set", degraded_pool, "min_size", "3")

        logger.info(f"Stopping ceph-osd -i {osd_id} and marking down...")
        subprocess.run(["pkill", "-f", f"ceph-osd -i {osd_id}"], check=False)
        try:
            self.run_ceph("osd", "down", str(osd_id))
        except Exception:
            pass

        logger.info(f"Waiting for peering to complete on {degraded_pool}...")
        t0 = time.time()
        while time.time() - t0 < timeout:
            status = self.run_ceph("status")
            if "peering" not in status and ("undersized" in status or "degraded" in status):
                logger.info("Fault injected: peering complete, undersized PGs confirmed.")
                return True
            time.sleep(0.5)

        logger.warning("Timeout waiting for peering to finish, continuing.")
        return False

    def restore_cluster(self, degraded_pool="degraded_pool", osd_id=2, timeout=45):
        logger.info(f"Restoring cluster: setting {degraded_pool} min_size=2...")
        try:
            self.run_ceph("osd", "pool", "set", degraded_pool, "min_size", "2")
        except Exception as e:
            logger.warning(f"Error resetting min_size: {e}")

        res = subprocess.run(["pgrep", "-f", f"ceph-osd -i {osd_id}"], stdout=subprocess.PIPE, text=True)
        if not res.stdout.strip():
            logger.info(f"Starting ceph-osd -i {osd_id}...")
            stdout_path = os.path.join(BUILD_DIR, "out", f"osd.{osd_id}.stdout")
            cmd = f"nohup {CEPH_OSD_BIN} -i {osd_id} -c {CEPH_CONF} > {stdout_path} 2>&1 < /dev/null &"
            subprocess.run(cmd, shell=True, check=False)

        logger.info("Waiting for cluster recovery (3 OSDs UP, active+clean PGs)...")
        t0 = time.time()
        while time.time() - t0 < timeout:
            status = self.check_cluster_health()
            if status["osds_up"] >= 3 and status["pgs_clean"] == status["total_pgs"]:
                logger.info(f"Cluster fully recovered: {status['osds_up']} OSDs UP, active+clean.")
                return True
            time.sleep(1.0)

        logger.warning("Cluster recovery timed out; continuing.")
        return False


# ==============================================================================
# 2. Remote System Profiler (pstack & threads on Jarvis)
# ==============================================================================
class RemoteProfiler:
    """Captures and analyzes pstack traces on the remote RGW host."""

    def __init__(self, host=JARVIS_HOST):
        self.host = host

    def get_rgw_pid(self):
        cmd = ["ssh", self.host, "cat /home/jarvis/development/build-ceph/release/out/radosgw.8000.pid 2>/dev/null || pgrep -x radosgw | head -n 1"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            pid = res.stdout.strip()
            return int(pid) if pid else None
        except Exception as e:
            logger.warning(f"Failed to get radosgw PID on {self.host}: {e}")
            return None

    def capture_pstack(self, output_path):
        pid = self.get_rgw_pid()
        if not pid:
            logger.warning("No radosgw PID found on remote host.")
            return {}

        logger.info(f"Capturing remote pstack for PID {pid} on {self.host}...")
        cmd = ["ssh", self.host, f"pstack {pid}"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=45)
            trace = res.stdout
            with open(output_path, "w") as f:
                f.write(trace)

            analysis = self.analyze_trace(trace)
            logger.info(f"pstack analyzed: {analysis['total_threads']} total threads, "
                        f"{analysis['io_context_threads']} in io_context_pool, "
                        f"{analysis['futex_threads']} in futex/cond, "
                        f"{analysis['epoll_threads']} in epoll.")
            return analysis
        except Exception as e:
            logger.warning(f"Failed to capture pstack: {e}")
            return {}

    @staticmethod
    def analyze_trace(trace_text):
        threads = re.split(r"Thread \d+ ", trace_text)
        total_threads = len(threads) - 1
        futex_threads = 0
        io_context_threads = 0
        epoll_threads = 0
        other_threads = 0

        for t in threads[1:]:
            if "io_context_pool" in t:
                io_context_threads += 1
            if "futex" in t or "pthread_cond_wait" in t:
                futex_threads += 1
            elif "epoll_wait" in t or "io_uring" in t or "poll" in t:
                epoll_threads += 1
            else:
                other_threads += 1

        return {
            "total_threads": total_threads,
            "io_context_threads": io_context_threads,
            "futex_threads": futex_threads,
            "epoll_threads": epoll_threads,
            "other_threads": other_threads,
        }


# ==============================================================================
# 3. Client Workload Tasks
# ==============================================================================
def get_s3_client(endpoint, timeout=5.0):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        config=Config(
            signature_version="s3v4",
            connect_timeout=2.0,
            read_timeout=timeout,
            retries={"max_attempts": 0},
        ),
    )


def degraded_worker_task(worker_id, endpoint, bucket, duration, client_to, stop_evt, results):
    s3 = get_s3_client(endpoint, timeout=client_to)
    seq = 0
    start_time = time.time()

    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        key = f"deg_w{worker_id}_{seq}.dat"
        t0 = time.time()
        status = 0
        err_code = ""
        retry_after = None

        try:
            res = s3.put_object(Bucket=bucket, Key=key, Body=PAYLOAD_1K)
            status = res.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
        except ClientError as ce:
            resp = ce.response or {}
            meta = resp.get("ResponseMetadata", {})
            status = meta.get("HTTPStatusCode", 500)
            headers = meta.get("HTTPHeaders", {})
            retry_after = headers.get("retry-after")
            err_code = resp.get("Error", {}).get("Code", str(ce))
        except (ReadTimeoutError, EndpointConnectionError):
            status = 408
            err_code = "Timeout"
        except Exception as e:
            status = 599
            err_code = str(e)
        t1 = time.time()

        results.append({
            "worker": worker_id,
            "seq": seq,
            "latency": t1 - t0,
            "status": status,
            "err_code": err_code,
            "retry_after": retry_after,
        })

        if status != 200:
            time.sleep(0.05)


def good_worker_task(worker_id, endpoint, bucket, duration, stop_evt, results, timeout=30.0):
    s3 = get_s3_client(endpoint, timeout=timeout)
    seq = 0
    start_time = time.time()

    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        key = f"good_w{worker_id}_{seq}.dat"
        t0 = time.time()
        status = 0
        try:
            res = s3.put_object(Bucket=bucket, Key=key, Body=PAYLOAD_1K)
            status = res.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
        except Exception:
            status = 500
        t1 = time.time()

        results.append({
            "worker": worker_id,
            "seq": seq,
            "latency": t1 - t0,
            "status": status,
        })


def probe_worker_task(worker_id, endpoint, bucket, duration, stop_evt, results, timeout=30.0):
    s3 = get_s3_client(endpoint, timeout=timeout)
    seq = 0
    start_time = time.time()

    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        t0 = time.time()
        status = 0
        try:
            res = s3.list_objects_v2(Bucket=bucket, MaxKeys=5)
            status = res.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
        except Exception:
            status = 500
        t1 = time.time()

        results.append({
            "worker": worker_id,
            "seq": seq,
            "latency": t1 - t0,
            "status": status,
        })
        time.sleep(0.1)


# ==============================================================================
# 4. Latency & Metric Calculations
# ==============================================================================
def calc_stats(latencies):
    if not latencies:
        return {"count": 0, "avg": 0.0, "p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    s = sorted(latencies)
    n = len(s)
    return {
        "count": n,
        "avg": round(sum(s) / n, 4),
        "p50": round(s[int(n * 0.50)], 4),
        "p90": round(s[min(int(n * 0.90), n - 1)], 4),
        "p95": round(s[min(int(n * 0.95), n - 1)], 4),
        "p99": round(s[min(int(n * 0.99), n - 1)], 4),
        "max": round(s[-1], 4),
    }


# ==============================================================================
# 5. Experiment Runner
# ==============================================================================
def run_concurrency_iteration(mgr, profiler, endpoint, degraded_concurrency, good_concurrency,
                              duration, client_timeout, output_dir, run_id, good_timeout=30.0):
    logger.info(f"\n=======================================================")
    logger.info(f" Starting Concurrency Level: {degraded_concurrency} Degraded Workers")
    logger.info(f" Good Workers: {good_concurrency}, Duration: {duration}s, Degraded Timeout: {client_timeout}s, Good Timeout: {good_timeout}s")
    logger.info(f"=======================================================")

    # Inject degraded fault
    mgr.inject_degraded_fault(degraded_pool="degraded_pool", osd_id=2)

    deg_results = []
    good_results = []
    probe_results = []
    stop_evt = threading.Event()

    t_start = time.time()
    total_workers = degraded_concurrency + good_concurrency + 2

    with ThreadPoolExecutor(max_workers=total_workers) as executor:
        for i in range(degraded_concurrency):
            executor.submit(degraded_worker_task, i, endpoint, "bucket-degraded",
                            duration, client_timeout, stop_evt, deg_results)

        for i in range(good_concurrency):
            executor.submit(good_worker_task, i, endpoint, "bucket-good",
                            duration, stop_evt, good_results, good_timeout)

        for i in range(2):
            executor.submit(probe_worker_task, i, endpoint, "bucket-good",
                            duration, stop_evt, probe_results, good_timeout)

        # Allow workers to ramp up and saturate before sampling
        time.sleep(3.0)
        pstack_file = os.path.join(output_dir, f"pstack_{run_id}_{degraded_concurrency}w.txt")
        pstack_analysis = profiler.capture_pstack(pstack_file)

        elapsed = time.time() - t_start
        if elapsed < duration:
            time.sleep(duration - elapsed)
        stop_evt.set()

    t_end = time.time()
    total_duration = t_end - t_start

    # Restore cluster cleanly
    mgr.restore_cluster(degraded_pool="degraded_pool", osd_id=2)

    deg_lats = [r["latency"] for r in deg_results]
    good_lats = [r["latency"] for r in good_results]
    probe_lats = [r["latency"] for r in probe_results]

    deg_status = {}
    for r in deg_results:
        st = r["status"]
        deg_status[st] = deg_status.get(st, 0) + 1

    good_status = {}
    for r in good_results:
        st = r["status"]
        good_status[st] = good_status.get(st, 0) + 1

    slowdown_count = deg_status.get(503, 0)
    timeout_count = deg_status.get(408, 0)
    ok_count = deg_status.get(200, 0)

    summary = {
        "concurrency": degraded_concurrency,
        "duration": round(total_duration, 2),
        "degraded": {
            "total_requests": len(deg_results),
            "throughput_ops_sec": round(len(deg_results) / total_duration, 2),
            "status_counts": deg_status,
            "http_200": ok_count,
            "http_503_slowdown": slowdown_count,
            "http_408_timeout": timeout_count,
            "latencies": calc_stats(deg_lats),
        },
        "good": {
            "total_requests": len(good_results),
            "throughput_ops_sec": round(len(good_results) / total_duration, 2),
            "status_counts": good_status,
            "latencies": calc_stats(good_lats),
        },
        "probe": {
            "total_requests": len(probe_results),
            "latencies": calc_stats(probe_lats),
        },
        "pstack_analysis": pstack_analysis,
    }

    logger.info(f"Results for Concurrency {degraded_concurrency}w:")
    logger.info(f"  Degraded ops: {len(deg_results)} total | 503 SlowDown: {slowdown_count} | "
                f"Timeouts: {timeout_count} | Mean Lat: {summary['degraded']['latencies']['avg']}s | "
                f"P95 Lat: {summary['degraded']['latencies']['p95']}s")
    logger.info(f"  Good ops: {len(good_results)} total | Throughput: {summary['good']['throughput_ops_sec']} ops/s | "
                f"P95 Lat: {summary['good']['latencies']['p95']}s")

    return summary


def main():
    parser = argparse.ArgumentParser(description="Multi-concurrency benchmark for Fast-Fail Circuit Breaker")
    parser.add_argument("--mode", choices=["baseline", "fixed"], default="baseline",
                        help="Execution mode (baseline or fixed)")
    parser.add_argument("--concurrency-levels", default="10,30,60,100",
                        help="Comma-separated degraded worker counts (e.g. 10,30,60,100)")
    parser.add_argument("--good-workers", type=int, default=10,
                        help="Number of healthy pool workers (default 10)")
    parser.add_argument("--duration", type=int, default=15,
                        help="Duration in seconds per concurrency level (default 15)")
    parser.add_argument("--client-timeout", type=float, default=5.0,
                        help="Client read timeout in seconds (default 5.0)")
    parser.add_argument("--good-timeout", type=float, default=30.0,
                        help="Good pool client timeout in seconds (default 30.0)")
    parser.add_argument("--endpoint", default="http://10.0.0.2:8000",
                        help="S3 endpoint (default http://10.0.0.2:8000)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR,
                        help=f"Directory to write telemetry JSON (default {DEFAULT_OUTPUT_DIR})")
    args = parser.parse_args()

    levels = [int(c.strip()) for c in args.concurrency_levels.split(",") if c.strip()]
    mgr = ClusterManager()
    profiler = RemoteProfiler()

    # Pre-flight check
    health = mgr.check_cluster_health()
    logger.info(f"Pre-flight health check: OSDs {health['osds_up']}/{health['osds_in']} UP, "
                f"Clean PGs {health['pgs_clean']}/{health['total_pgs']}, Status: {health['health']}")
    if health["osds_up"] < 3:
        logger.info("Restoring cluster before starting benchmark...")
        mgr.restore_cluster()

    fast_fail_val = "false" if args.mode == "baseline" else "true"
    logger.info(f"Configuring RGW via admin socket: objecter_fast_fail_undersized_pgs = {fast_fail_val}")
    mgr.run_asok("config", "set", "objecter_fast_fail_undersized_pgs", fast_fail_val)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id = f"{args.mode}_{timestamp}"
    out_file = os.path.join(args.output_dir, f"fast_fail_{run_id}.json")

    results = {
        "metadata": {
            "mode": args.mode,
            "timestamp": timestamp,
            "concurrency_levels": levels,
            "good_workers": args.good_workers,
            "duration": args.duration,
            "client_timeout": args.client_timeout,
            "good_timeout": args.good_timeout,
            "endpoint": args.endpoint,
        },
        "iterations": [],
    }

    try:
        for lvl in levels:
            res = run_concurrency_iteration(
                mgr=mgr,
                profiler=profiler,
                endpoint=args.endpoint,
                degraded_concurrency=lvl,
                good_concurrency=args.good_workers,
                duration=args.duration,
                client_timeout=args.client_timeout,
                output_dir=args.output_dir,
                run_id=run_id,
                good_timeout=args.good_timeout,
            )
            results["iterations"].append(res)
            time.sleep(2.0)
    finally:
        logger.info("Restoring RGW fast-fail to true...")
        try:
            mgr.run_asok("config", "set", "objecter_fast_fail_undersized_pgs", "true")
        except Exception as e:
            logger.warning(f"Error resetting fast-fail config: {e}")
        logger.info("Final cleanup: ensuring cluster is fully restored...")
        mgr.restore_cluster()

    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)
    logger.info(f"\n[+] Telemetry saved to: {out_file}")

    print("\n" + "=" * 95)
    print(f" FAST-FAIL CIRCUIT BREAKER BENCHMARK SUMMARY (Mode: {args.mode.upper()})")
    print("=" * 95)
    header = f"{'Degraded W':<12} | {'503 SlowDown':<14} | {'408 Timeout':<13} | {'Deg P95 Lat':<13} | {'Good Ops/s':<12} | {'Good P95 Lat':<13}"
    print(header)
    print("-" * 95)
    for it in results["iterations"]:
        c = it["concurrency"]
        deg = it["degraded"]
        good = it["good"]
        p95_deg = f"{deg['latencies']['p95']:.3f}s"
        p95_good = f"{good['latencies']['p95']:.3f}s"
        row = f"{c:<12} | {deg['http_503_slowdown']:<14} | {deg['http_408_timeout']:<13} | {p95_deg:<13} | {good['throughput_ops_sec']:<12.1f} | {p95_good:<13}"
        print(row)
    print("=" * 95 + "\n")


if __name__ == "__main__":
    main()
