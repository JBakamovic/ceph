#!/usr/bin/env python3
"""
test_objecter_pool_throttle_contention.py - Automated End-to-End Orchestrator for
Degraded PG Contention and Objecter Per-Pool Throttling Verification.

This script automates the complete test lifecycle:
1. Cluster pre-flight health checks (OSD status, PG state, RGW admin socket bridge).
2. Dynamic RGW configuration toggling via admin daemon:
   - objecter_pool_throttle_enable (true/false)
   - objecter_inflight_ops (e.g. 50 ops)
   - objecter_pool_inflight_ops_ratio (e.g. 0.5)
3. Controlled degraded fault injection:
   - Sets min_size=3 on 'degraded_pool' (CRUSH rule 0, 3 replicas)
   - Stops 'ceph-osd -i 2'
   - Polls until PGs transition to undersized+degraded+peered (writes to degraded_pool freeze)
   - Verifies 'good_pool' remains active+clean on osd.0 & osd.1
4. S3 multi-worker load generation:
   - Degraded workers saturate the degraded pool with permanent in-flight writes
   - Good workers submit concurrent PUT requests to the healthy good_pool
5. Post-test server-side Ceph log telemetry:
   - Tracks log offsets on remote RGW gateway (Jarvis @ 10.0.0.2)
   - Parses radosgw.8000.log to extract server-side latencies, completion counts, and in-flight operations
6. Guaranteed cluster restoration:
   - Restores min_size=2 on 'degraded_pool'
   - Restarts 'ceph-osd -i 2'
   - Polls until all 3 OSDs are UP and PGs are active+clean (guaranteed in finally block)
7. Multi-mode execution:
   - --mode compare: Runs baseline (throttling disabled) vs fixed (throttling enabled)
     and outputs a side-by-side terminal comparison table.
   - --mode baseline: Legacy shared throttle behavior (objecter_pool_throttle_enable=false)
   - --mode fixed: Per-pool partitioned throttle behavior (objecter_pool_throttle_enable=true)
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError, EndpointConnectionError, ReadTimeoutError
except ImportError:
    print("Error: boto3 is required. Please install it with: pip install boto3", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pool_throttle_test")

CEPH_BIN = "/home/ultron/development/build-ceph/release/bin/ceph"
CEPH_CONF = "/home/ultron/development/build-ceph/release/ceph.conf"
CEPH_OSD_BIN = "/home/ultron/development/build-ceph/release/bin/ceph-osd"
BUILD_DIR = "/home/ultron/development/build-ceph/release"
ASOK_PATH = "/tmp/radosgw.8000.asok"
JARVIS_HOST = "10.0.0.2"
JARVIS_LOG_PATH = "/home/jarvis/development/build-ceph/release/out/radosgw.8000.log"

ACCESS_KEY = "0555b35654ad1656d804"
SECRET_KEY = "h7GhxuBLTrlhVUyxSPUKUV8r/2EI4ngqJxD7iBdBYLhwluN30JaT3Q=="
PAYLOAD_1K = b"X" * 1024


# ==============================================================================
# 1. Cluster Management & Fault Injection
# ==============================================================================
class ClusterManager:
    """Manages Ceph cluster state, fault injection, and dynamic RGW configs."""

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
        """Returns parsed cluster status."""
        status_text = self.run_ceph("status")
        osds_up = 0
        pgs_clean = 0
        total_pgs = 0

        m_osd = re.search(r"(\d+)\s+osds:\s+(\d+)\s+up", status_text)
        if m_osd:
            osds_up = int(m_osd.group(2))

        m_pgs = re.search(r"(\d+)\s+pgs:\s+(\d+)\s+active\+clean", status_text)
        if m_pgs:
            total_pgs = int(m_pgs.group(1))
            pgs_clean = int(m_pgs.group(2))
        else:
            m_total = re.search(r"(\d+)\s+pgs", status_text)
            if m_total:
                total_pgs = int(m_total.group(1))

        return {
            "osds_up": osds_up,
            "total_pgs": total_pgs,
            "pgs_clean": pgs_clean,
            "raw": status_text,
        }

    def verify_asok_reachable(self):
        try:
            val = self.run_asok("version")
            return bool(val)
        except Exception as e:
            logger.error(f"RGW admin socket at {self.asok_path} is unreachable: {e}")
            return False

    def set_rgw_config(self, key, value):
        logger.info(f"Setting RGW config via admin socket: {key} = {value}")
        self.run_asok("config", "set", key, str(value))
        # Verify
        raw = self.run_asok("config", "get", key)
        try:
            parsed = json.loads(raw)
            actual = parsed.get(key)
            # Compare float or string
            try:
                if abs(float(actual) - float(value)) > 1e-4:
                    logger.warning(f"Config verification mismatch for {key}: expected {value}, got {actual}")
            except (ValueError, TypeError):
                if str(actual).lower() != str(value).lower():
                    logger.warning(f"Config verification mismatch for {key}: expected {value}, got {actual}")
        except Exception:
            pass

    def get_pool_map(self):
        """Returns (name_to_id, id_to_name) mapping from ceph osd pool ls detail."""
        try:
            raw = self.run_ceph("osd", "pool", "ls", "detail", "-f", "json")
            idx = raw.find("[")
            if idx >= 0:
                raw = raw[idx:]
            pools = json.loads(raw)
            name_to_id = {p["pool_name"]: p["pool_id"] for p in pools}
            id_to_name = {p["pool_id"]: p["pool_name"] for p in pools}
            return name_to_id, id_to_name
        except Exception as e:
            logger.warning(f"Failed to fetch pool map: {e}")
            return {}, {}

    def get_rgw_perf(self):
        try:
            raw = self.run_asok("perf", "dump")
            idx = raw.find("{")
            if idx >= 0:
                raw = raw[idx:]
            data = json.loads(raw)
            obj = data.get("objecter", {})

            # Aggregate all global throttle-objecter_ops instances
            global_wait_count = 0
            global_wait_sum = 0.0
            global_get = 0
            for k, v in data.items():
                if re.match(r"^throttle-objecter_ops(-0x[0-9a-fA-F]+)?$", k):
                    global_get += v.get("get", 0)
                    w = v.get("wait", {})
                    global_wait_count += w.get("avgcount", 0)
                    global_wait_sum += w.get("sum", 0.0)

            # Per-pool throttle stats
            _, id_to_name = self.get_pool_map()
            per_pool_stats = {}
            for k, v in data.items():
                m = re.match(r"^throttle-objecter_pool_(\d+)_ops(-0x[0-9a-fA-F]+)?$", k)
                if m:
                    pid = int(m.group(1))
                    pname = id_to_name.get(pid, f"pool_{pid}")
                    w = v.get("wait", {})
                    if pname not in per_pool_stats:
                        per_pool_stats[pname] = {
                            "pool_id": pid,
                            "get": 0,
                            "wait_count": 0,
                            "wait_sum": 0.0,
                        }
                    per_pool_stats[pname]["get"] += v.get("get", 0)
                    per_pool_stats[pname]["wait_count"] += w.get("avgcount", 0)
                    per_pool_stats[pname]["wait_sum"] += w.get("sum", 0.0)

            return {
                "objecter_op_active": obj.get("op_active", 0),
                "objecter_op_inflight": obj.get("op_inflight", 0),
                "objecter_op_send": obj.get("op_send", 0),
                "objecter_op_reply": obj.get("op_reply", 0),
                "global_throttle": {
                    "get": global_get,
                    "wait_count": global_wait_count,
                    "wait_sum": global_wait_sum,
                },
                "per_pool": per_pool_stats,
                # Backwards-compatibility fields
                "throttle_ops_wait_count": global_wait_count,
                "throttle_ops_wait_sum": global_wait_sum,
            }
        except Exception as e:
            logger.warning(f"Failed to fetch perf dump from admin socket: {e}")
            return {}

    def inject_degraded_fault(self, degraded_pool="degraded_pool", osd_id=2, timeout=20):
        """Sets min_size=3 and stops osd.2 so degraded_pool writes freeze permanently."""
        logger.info(f"Injecting degraded fault: setting {degraded_pool} min_size=3...")
        self.run_ceph("osd", "pool", "set", degraded_pool, "min_size", "3")

        logger.info(f"Stopping ceph-osd -i {osd_id}...")
        subprocess.run(["pkill", "-f", f"ceph-osd -i {osd_id}"], check=False)

        logger.info(f"Waiting for {degraded_pool} PGs to report undersized/degraded/peered...")
        t0 = time.time()
        while time.time() - t0 < timeout:
            status = self.run_ceph("status")
            if "undersized" in status or "degraded" in status or "peered" in status:
                logger.info("Fault injected successfully: PGs degraded/peered as expected.")
                return True
            time.sleep(0.5)

        logger.warning("Timeout waiting for degraded PG status, proceeding anyway.")
        return False

    def restore_cluster(self, degraded_pool="degraded_pool", osd_id=2, timeout=45):
        """Restores min_size=2 and starts osd.2, waiting for cluster recovery."""
        logger.info(f"Restoring cluster: setting {degraded_pool} min_size=2...")
        try:
            self.run_ceph("osd", "pool", "set", degraded_pool, "min_size", "2")
        except Exception as e:
            logger.warning(f"Error restoring pool min_size: {e}")

        # Check if OSD is already running
        res = subprocess.run(["pgrep", "-f", f"ceph-osd -i {osd_id}"], stdout=subprocess.PIPE, text=True)
        if not res.stdout.strip():
            logger.info(f"Starting ceph-osd -i {osd_id}...")
            stdout_path = os.path.join(BUILD_DIR, "out", f"osd.{osd_id}.stdout")
            cmd = f"nohup {CEPH_OSD_BIN} -i {osd_id} -c {CEPH_CONF} > {stdout_path} 2>&1 < /dev/null &"
            subprocess.run(cmd, shell=True, check=False)

        logger.info("Waiting for cluster recovery (3 OSDs UP and active+clean PGs)...")
        t0 = time.time()
        while time.time() - t0 < timeout:
            status = self.check_cluster_health()
            if status["osds_up"] >= 3 and ("active+clean" in status["raw"] or status["pgs_clean"] == status["total_pgs"]):
                logger.info(f"Cluster fully restored: {status['osds_up']} OSDs UP, active+clean.")
                return True
            time.sleep(1.0)

        logger.warning("Cluster restore completed with partial peering; continuing.")
        return False


# ==============================================================================
# 2. Remote RGW Log Telemetry (Jarvis)
# ==============================================================================
class RemoteLogInspector:
    """Tracks and parses radosgw.8000.log on Jarvis over SSH."""

    REQ_DONE_PATTERN = re.compile(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+[\+\-]\d{4})\s+\S+\s+\d+\s+====== req done req=(\S+)\s+op=(\S+)\s+bucket=(\S+)\s+status=(-?\d+)\s+http_status=(\d+)\s+latency=([\d\.]+)s\s+request_id=(\S+)"
    )
    REQ_START_PATTERN = re.compile(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+[\+\-]\d{4})\s+\S+\s+\d+\s+====== starting new request req=(\S+)\s+request_id=(\S+)"
    )

    def __init__(self, host=JARVIS_HOST, log_path=JARVIS_LOG_PATH):
        self.host = host
        self.log_path = log_path

    def get_log_byte_offset(self):
        cmd = ["ssh", self.host, f"stat -c %s {self.log_path}"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return int(res.stdout.strip())
        except Exception as e:
            logger.warning(f"Failed to query remote log byte offset on {self.host}: {e}")
            return 0

    def fetch_new_log_content(self, start_offset):
        cmd = ["ssh", self.host, f"tail -c +{start_offset + 1} {self.log_path}"]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
            return res.stdout
        except Exception as e:
            logger.warning(f"Failed to fetch new log content from {self.host}: {e}")
            return ""

    @classmethod
    def parse_log_records(cls, log_text, client_timeout=10.0):
        good_put_records = []
        good_list_records = []
        good_meta_records = []
        other_good_records = []
        deg_records = []
        start_count = 0

        for line in log_text.splitlines():
            if "====== starting new request" in line:
                start_count += 1
                continue

            m = cls.REQ_DONE_PATTERN.search(line)
            if m:
                ts, req_ptr, op, bucket, status, http_status, lat, req_id = m.groups()
                rec = {
                    "timestamp": ts,
                    "op": op,
                    "bucket": bucket,
                    "status": int(status),
                    "http_status": int(http_status),
                    "latency": float(lat),
                    "req_id": req_id,
                }
                if bucket == "bucket-good":
                    if op == "put_obj":
                        good_put_records.append(rec)
                    elif op in ("list_bucket", "get_bucket_location"):
                        good_list_records.append(rec)
                    elif op in ("stat_bucket", "get_bucket_logging"):
                        good_meta_records.append(rec)
                    else:
                        other_good_records.append(rec)
                elif bucket == "bucket-degraded":
                    deg_records.append(rec)

        good_put_200 = [r for r in good_put_records if r["http_status"] == 200]
        good_put_ontime = [r for r in good_put_200 if r["latency"] <= client_timeout]
        good_put_delayed = [r for r in good_put_200 if r["latency"] > client_timeout]

        good_list_200 = [r for r in good_list_records if r["http_status"] == 200]
        good_list_ontime = [r for r in good_list_200 if r["latency"] <= client_timeout]
        good_list_delayed = [r for r in good_list_200 if r["latency"] > client_timeout]

        good_meta_200 = [r for r in good_meta_records if r["http_status"] == 200]
        deg_200 = [r for r in deg_records if r["http_status"] == 200]

        all_good_200 = good_put_200 + good_list_200 + good_meta_200 + [r for r in other_good_records if r["http_status"] == 200]

        return {
            "new_requests_started": start_count,
            "good_put_completed_200": len(good_put_200),
            "good_put_ontime_completed": len(good_put_ontime),
            "good_put_delayed_completed": len(good_put_delayed),
            "good_put_latencies": [r["latency"] for r in good_put_200],
            "good_list_completed_200": len(good_list_200),
            "good_list_ontime_completed": len(good_list_ontime),
            "good_list_delayed_completed": len(good_list_delayed),
            "good_list_latencies": [r["latency"] for r in good_list_200],
            "good_meta_completed_200": len(good_meta_200),
            "good_meta_latencies": [r["latency"] for r in good_meta_200],
            "degraded_completed_200": len(deg_200),
            "degraded_latencies": [r["latency"] for r in deg_200],
            # Backwards compatibility
            "good_completed_200": len(all_good_200),
            "good_latencies": [r["latency"] for r in all_good_200],
        }


# ==============================================================================
# 3. Multi-Worker S3 Workload Generator
# ==============================================================================
def get_s3_client(endpoint, timeout=10.0, max_pool=150):
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


def good_worker_task(worker_id, endpoint, bucket, duration, rate, client_to, stop_evt, results):
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


def degraded_worker_task(worker_id, endpoint, bucket, duration, client_to, stop_evt, results):
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

        if status != 200:
            time.sleep(0.5)


def calculate_latencies(lat_list):
    if not lat_list:
        return {"p50": 0.0, "p90": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0, "avg": 0.0}
    s = sorted(lat_list)
    n = len(s)
    return {
        "p50": round(s[int(n * 0.50)], 4),
        "p90": round(s[min(int(n * 0.90), n - 1)], 4),
        "p95": round(s[min(int(n * 0.95), n - 1)], 4),
        "p99": round(s[min(int(n * 0.99), n - 1)], 4),
        "max": round(s[-1], 4),
        "avg": round(sum(s) / n, 4),
    }


def list_probe_task(endpoint, bucket, duration, rate, client_to, stop_evt, results):
    """Probes the bucket index (default.rgw.buckets.index) and metadata via list_objects_v2."""
    s3 = get_s3_client(endpoint, timeout=client_to)
    interval = 1.0 / max(rate, 0.1)
    seq = 0
    start_time = time.time()

    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        t0 = time.time()
        status = 0
        err_msg = ""
        try:
            res = s3.list_objects_v2(Bucket=bucket, MaxKeys=50)
            status = res.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
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
            "probe": "list_objects",
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


def meta_probe_task(endpoint, bucket, duration, rate, client_to, stop_evt, results):
    """Probes the RGW metadata plane (default.rgw.meta) via head_bucket."""
    s3 = get_s3_client(endpoint, timeout=client_to)
    interval = 1.0 / max(rate, 0.1)
    seq = 0
    start_time = time.time()

    while time.time() - start_time < duration and not stop_evt.is_set():
        seq += 1
        t0 = time.time()
        status = 0
        err_msg = ""
        try:
            res = s3.head_bucket(Bucket=bucket)
            status = res.get("ResponseMetadata", {}).get("HTTPStatusCode", 200)
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
            "probe": "head_bucket",
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


def compute_perf_delta(perf_pre, perf_post):
    """Calculates deltas for global and per-pool Objecter throttle performance counters."""
    if not perf_pre or not perf_post:
        return {
            "objecter_op_send_delta": 0,
            "objecter_op_reply_delta": 0,
            "throttle_wait_count_delta": 0,
            "throttle_wait_sum_sec": 0.0,
            "global_throttle": {"get_delta": 0, "wait_count_delta": 0, "wait_sum_sec": 0.0},
            "per_pool": {},
        }

    g_pre = perf_pre.get("global_throttle", {})
    g_post = perf_post.get("global_throttle", {})

    pools_delta = {}
    p_pre = perf_pre.get("per_pool", {})
    p_post = perf_post.get("per_pool", {})
    all_pools = set(p_pre.keys()) | set(p_post.keys())

    for pname in sorted(all_pools):
        before = p_pre.get(pname, {})
        after = p_post.get(pname, {})
        pid = after.get("pool_id", before.get("pool_id", -1))
        pools_delta[pname] = {
            "pool_id": pid,
            "get_delta": after.get("get", 0) - before.get("get", 0),
            "wait_count_delta": after.get("wait_count", 0) - before.get("wait_count", 0),
            "wait_sum_sec": round(after.get("wait_sum", 0.0) - before.get("wait_sum", 0.0), 3),
        }

    g_wait_cnt = g_post.get("wait_count", 0) - g_pre.get("wait_count", 0)
    g_wait_sum = round(g_post.get("wait_sum", 0.0) - g_pre.get("wait_sum", 0.0), 3)

    return {
        "objecter_op_send_delta": perf_post.get("objecter_op_send", 0) - perf_pre.get("objecter_op_send", 0),
        "objecter_op_reply_delta": perf_post.get("objecter_op_reply", 0) - perf_pre.get("objecter_op_reply", 0),
        "throttle_wait_count_delta": g_wait_cnt,
        "throttle_wait_sum_sec": g_wait_sum,
        "global_throttle": {
            "get_delta": g_post.get("get", 0) - g_pre.get("get", 0),
            "wait_count_delta": g_wait_cnt,
            "wait_sum_sec": g_wait_sum,
        },
        "per_pool": pools_delta,
    }


# ==============================================================================
# 4. Experiment Runner
# ==============================================================================
class ExperimentRunner:
    """Executes single runs or comparative experiments with full lifecycle orchestration."""

    def __init__(self, args):
        self.args = args
        self.cluster = ClusterManager()
        self.inspector = RemoteLogInspector()

    def run_experiment(
        self,
        run_name,
        pool_throttle_enable,
        inflight_ops=None,
        pool_ratio=None,
        pool_throttle_async=None,
        queue_ratio=None,
        max_queue_ops=None,
        degraded_workers=None,
        good_workers=None,
        duration=None,
        skip_fault=False,
    ):
        inflight_ops = inflight_ops if inflight_ops is not None else self.args.objecter_inflight_ops
        pool_ratio = pool_ratio if pool_ratio is not None else self.args.pool_ratio
        pool_throttle_async = pool_throttle_async if pool_throttle_async is not None else getattr(self.args, "pool_throttle_async", True)
        queue_ratio = queue_ratio if queue_ratio is not None else getattr(self.args, "queue_ratio", 4.0)
        max_queue_ops = max_queue_ops if max_queue_ops is not None else getattr(self.args, "max_queue_ops", 0)
        degraded_workers = degraded_workers if degraded_workers is not None else self.args.degraded_workers
        good_workers = good_workers if good_workers is not None else self.args.good_workers
        duration = duration if duration is not None else self.args.duration

        logger.info(f"\n========================================================")
        logger.info(f"STARTING EXPERIMENT RUN: {run_name}")
        logger.info(f"objecter_pool_throttle_enable = {pool_throttle_enable}")
        logger.info(f"objecter_inflight_ops        = {inflight_ops}")
        logger.info(f"objecter_pool_inflight_ratio = {pool_ratio}")
        logger.info(f"objecter_pool_throttle_async = {pool_throttle_async}")
        logger.info(f"objecter_pool_throttle_queue_ratio = {queue_ratio}")
        logger.info(f"objecter_pool_throttle_max_queue_ops = {max_queue_ops}")
        logger.info(f"good_workers                 = {good_workers}")
        logger.info(f"degraded_workers             = {degraded_workers}")
        logger.info(f"duration                     = {duration}")
        logger.info(f"skip_fault                   = {skip_fault}")
        logger.info(f"========================================================")

        # 1. Pre-flight checks
        if not self.cluster.verify_asok_reachable():
            raise RuntimeError(f"Admin socket at {ASOK_PATH} is not accessible!")

        # 2. Dynamic config update
        self.cluster.set_rgw_config("objecter_inflight_ops", inflight_ops)
        self.cluster.set_rgw_config("objecter_pool_throttle_enable", "true" if pool_throttle_enable else "false")
        self.cluster.set_rgw_config("objecter_pool_inflight_ops_ratio", str(pool_ratio))
        self.cluster.set_rgw_config("objecter_pool_throttle_async", "true" if pool_throttle_async else "false")
        self.cluster.set_rgw_config("objecter_pool_throttle_queue_ratio", str(queue_ratio))
        self.cluster.set_rgw_config("objecter_pool_throttle_max_queue_ops", str(max_queue_ops))

        # 3. Capture baseline state
        perf_pre = self.cluster.get_rgw_perf()
        log_offset_pre = self.inspector.get_log_byte_offset()
        logger.info(f"Remote RGW log byte offset before run: {log_offset_pre}")

        stop_evt = threading.Event()
        good_results = []
        degraded_results = []
        list_probe_results = []
        meta_probe_results = []
        t_start = 0.0
        t_end = 0.0

        try:
            # 4. Inject fault (unless skipped)
            if not skip_fault:
                self.cluster.inject_degraded_fault(
                    degraded_pool=self.args.degraded_pool,
                    osd_id=self.args.fault_osd,
                )
            else:
                logger.info("Skipping fault injection as requested (--skip-fault).")

            # 5. Execute concurrent S3 workload
            enable_probes = getattr(self.args, "enable_probes", True)
            probe_workers = 2 if enable_probes else 0
            logger.info(
                f"Launching workload: {good_workers} good workers (@ {self.args.good_rate}/s), "
                f"{degraded_workers} degraded workers, probes={'enabled' if enable_probes else 'disabled'}, "
                f"duration={duration}s"
            )

            t_start = time.time()
            with ThreadPoolExecutor(max_workers=good_workers + degraded_workers + probe_workers) as executor:
                futures = []
                for i in range(good_workers):
                    f = executor.submit(
                        good_worker_task,
                        i,
                        self.args.endpoint,
                        self.args.good_bucket,
                        duration,
                        self.args.good_rate,
                        self.args.good_timeout,
                        stop_evt,
                        good_results,
                    )
                    futures.append(f)

                for i in range(degraded_workers):
                    f = executor.submit(
                        degraded_worker_task,
                        i,
                        self.args.endpoint,
                        self.args.degraded_bucket,
                        duration,
                        self.args.degraded_timeout,
                        stop_evt,
                        degraded_results,
                    )
                    futures.append(f)

                if enable_probes:
                    f_list = executor.submit(
                        list_probe_task,
                        self.args.endpoint,
                        self.args.good_bucket,
                        duration,
                        getattr(self.args, "probe_rate", 2.0),
                        self.args.good_timeout,
                        stop_evt,
                        list_probe_results,
                    )
                    futures.append(f_list)

                    f_meta = executor.submit(
                        meta_probe_task,
                        self.args.endpoint,
                        self.args.good_bucket,
                        duration,
                        getattr(self.args, "probe_rate", 2.0),
                        self.args.good_timeout,
                        stop_evt,
                        meta_probe_results,
                    )
                    futures.append(f_meta)

                for f in futures:
                    f.result()
            t_end = time.time()

        finally:
            # 6. GUARANTEED CLUSTER RESTORATION
            logger.info("Restoring cluster health in finally block...")
            self.cluster.restore_cluster(
                degraded_pool=self.args.degraded_pool,
                osd_id=self.args.fault_osd,
            )

        # 7. Post-test telemetry
        perf_post = self.cluster.get_rgw_perf()
        new_log_content = self.inspector.fetch_new_log_content(log_offset_pre)
        log_metrics = RemoteLogInspector.parse_log_records(new_log_content, client_timeout=self.args.good_timeout)
        server_good_put_lat = calculate_latencies(log_metrics["good_put_latencies"])
        server_good_list_lat = calculate_latencies(log_metrics["good_list_latencies"])
        server_good_meta_lat = calculate_latencies(log_metrics["good_meta_latencies"])

        # 8. Compute client stats
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

        actual_duration = t_end - t_start if t_end > t_start else duration

        client_metrics = {
            "good_pool": {
                "total_attempts": len(good_results),
                "completed_200": len(good_completed),
                "timeouts_408": len(good_timeouts),
                "other_errors": len(good_errors),
                "success_rate_pct": round(len(good_completed) / max(len(good_results), 1) * 100.0, 1),
                "throughput_ops_sec": round(len(good_completed) / max(actual_duration, 0.1), 2),
                "latency": good_lat_stats,
            },
            "degraded_pool": {
                "total_attempts": len(degraded_results),
                "completed_200": len(deg_completed),
                "timeouts_408": len(deg_timeouts),
                "other_errors": len(deg_errors),
                "latency": deg_lat_stats,
            },
        }

        if enable_probes:
            list_completed = [r for r in list_probe_results if r["status"] == 200]
            list_timeouts = [r for r in list_probe_results if r["status"] == 408]
            list_errors = [r for r in list_probe_results if r["status"] not in (200, 408)]
            list_lat_stats = calculate_latencies([r["latency"] for r in list_completed])

            meta_completed = [r for r in meta_probe_results if r["status"] == 200]
            meta_timeouts = [r for r in meta_probe_results if r["status"] == 408]
            meta_errors = [r for r in meta_probe_results if r["status"] not in (200, 408)]
            meta_lat_stats = calculate_latencies([r["latency"] for r in meta_completed])

            client_metrics["probe_list"] = {
                "total_attempts": len(list_probe_results),
                "completed_200": len(list_completed),
                "timeouts_408": len(list_timeouts),
                "other_errors": len(list_errors),
                "success_rate_pct": round(len(list_completed) / max(len(list_probe_results), 1) * 100.0, 1),
                "latency": list_lat_stats,
            }
            client_metrics["probe_meta"] = {
                "total_attempts": len(meta_probe_results),
                "completed_200": len(meta_completed),
                "timeouts_408": len(meta_timeouts),
                "other_errors": len(meta_errors),
                "success_rate_pct": round(len(meta_completed) / max(len(meta_probe_results), 1) * 100.0, 1),
                "latency": meta_lat_stats,
            }

        perf_delta = compute_perf_delta(perf_pre, perf_post)

        summary = {
            "run_name": run_name,
            "timestamp": datetime.now().isoformat(),
            "config": {
                "pool_throttle_enable": pool_throttle_enable,
                "inflight_ops": inflight_ops,
                "pool_ratio": pool_ratio,
                "pool_throttle_async": pool_throttle_async,
                "queue_ratio": queue_ratio,
                "max_queue_ops": max_queue_ops,
                "good_workers": good_workers,
                "good_rate": self.args.good_rate,
                "good_timeout": self.args.good_timeout,
                "degraded_workers": degraded_workers,
                "degraded_timeout": self.args.degraded_timeout,
                "duration": duration,
                "skip_fault": skip_fault,
                "enable_probes": enable_probes,
            },
            "duration_actual": round(actual_duration, 2),
            "client_metrics": client_metrics,
            "server_ceph_metrics": {
                "good_put_completed_200": log_metrics["good_put_completed_200"],
                "good_put_ontime_completed": log_metrics["good_put_ontime_completed"],
                "good_put_delayed_completed": log_metrics["good_put_delayed_completed"],
                "good_put_latency": server_good_put_lat,
                "good_list_completed_200": log_metrics["good_list_completed_200"],
                "good_list_ontime_completed": log_metrics["good_list_ontime_completed"],
                "good_list_delayed_completed": log_metrics["good_list_delayed_completed"],
                "good_list_latency": server_good_list_lat,
                "good_meta_completed_200": log_metrics["good_meta_completed_200"],
                "good_meta_latency": server_good_meta_lat,
                "good_completed_200": log_metrics["good_completed_200"],
                "good_latency": server_good_put_lat,
                "degraded_completed_200": log_metrics["degraded_completed_200"],
                "new_requests_started": log_metrics["new_requests_started"],
            },
            "perf_counters": perf_delta,
        }

        # 9. Save JSON artifact
        os.makedirs(self.args.output_dir, exist_ok=True)
        ts_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_json_path = os.path.join(self.args.output_dir, f"{run_name}_{ts_suffix}.json")
        with open(out_json_path, "w") as f:
            json.dump(summary, f, indent=2)
        logger.info(f"Results successfully archived to {out_json_path}")

        return summary


# ==============================================================================
# 5. Output Formatting & Comparison Table
# ==============================================================================
def format_summary_table(baseline, fixed=None):
    """Prints a clear ASCII comparison table to the terminal."""
    col_w = [34, 26, 26] if fixed else [34, 30]

    def line(sep="-"):
        total = sum(col_w) + len(col_w) + 1
        return sep * total

    def row(c1, c2, c3=None):
        if c3 is not None:
            return f"| {c1:<{col_w[0]}} | {c2:<{col_w[1]}} | {c3:<{col_w[2]}} |"
        return f"| {c1:<{col_w[0]}} | {c2:<{col_w[1]}} |"

    out = []
    out.append("\n" + line("="))
    if fixed:
        b_name = "SYNC (Per-Pool Throttle)" if baseline.get("config", {}).get("pool_throttle_enable") and not baseline.get("config", {}).get("pool_throttle_async") else "BASELINE (Legacy Global)"
        f_name = "ASYNC (Option A Non-Blocking)" if fixed.get("config", {}).get("pool_throttle_async") else "FIXED (Per-Pool Throttle)"
        out.append(row("METRIC", b_name, f_name))
    else:
        name = baseline["run_name"]
        out.append(row("METRIC", f"RESULT ({name})"))
    out.append(line("="))

    b_good = baseline["client_metrics"]["good_pool"]
    b_ceph = baseline["server_ceph_metrics"]
    b_perf = baseline["perf_counters"]
    b_client = baseline["client_metrics"]

    if fixed:
        f_good = fixed["client_metrics"]["good_pool"]
        f_ceph = fixed["server_ceph_metrics"]
        f_perf = fixed["perf_counters"]
        f_client = fixed["client_metrics"]

        b_cfg = baseline.get("config", {})
        f_cfg = fixed.get("config", {})
        b_inf = b_cfg.get("inflight_ops", "?")
        f_inf = f_cfg.get("inflight_ops", "?")
        b_cap = f"{b_inf} ops (Shared)"
        f_ratio = f_cfg.get("pool_ratio", 0.5)
        f_cap = f"{max(1, int(f_inf * f_ratio))} ops ({int(f_ratio*100)}%)" if isinstance(f_inf, (int, float)) else "?"
        out.append(row("Global In-Flight Ops Limit", str(b_inf), str(f_inf)))
        out.append(row("Per-Pool Op Cap", b_cap, f_cap))
        b_async = "True" if b_cfg.get("pool_throttle_async", True) else "False (Futex Sleep)"
        f_async = "True (0 Threads Sleep)" if f_cfg.get("pool_throttle_async", True) else "False"
        out.append(row("Async Throttling Queue", b_async, f_async))
        out.append(line("-"))

        out.append(row("Good Pool Total Attempts", str(b_good["total_attempts"]), str(f_good["total_attempts"])))
        out.append(row("Good Pool Completed (200 OK)", f"{b_good['completed_200']} ({b_good['success_rate_pct']}%)", f"{f_good['completed_200']} ({f_good['success_rate_pct']}%)"))
        out.append(row("Good Pool Timeouts (408)", str(b_good["timeouts_408"]), str(f_good["timeouts_408"])))
        out.append(row("Good Pool Throughput", f"{b_good['throughput_ops_sec']} ops/s", f"{f_good['throughput_ops_sec']} ops/s"))
        out.append(row("Good Pool P50 Latency (Client)", f"{b_good['latency']['p50'] * 1000:.1f} ms", f"{f_good['latency']['p50'] * 1000:.1f} ms"))
        out.append(row("Good Pool P95 Latency (Client)", f"{b_good['latency']['p95'] * 1000:.1f} ms", f"{f_good['latency']['p95'] * 1000:.1f} ms"))
        out.append(row("Good Pool Max Latency (Client)", f"{b_good['latency']['max'] * 1000:.1f} ms", f"{f_good['latency']['max'] * 1000:.1f} ms"))
        
        # Non-Data Probes Section
        if "probe_list" in b_client and "probe_list" in f_client:
            b_list = b_client["probe_list"]
            f_list = f_client["probe_list"]
            b_meta = b_client.get("probe_meta", {})
            f_meta = f_client.get("probe_meta", {})

            out.append(line("-"))
            out.append(row("NON-DATA PROBES (INDEX & META)", "", ""))
            out.append(line("-"))
            out.append(row("Bucket List (Index) Attempts", str(b_list.get("total_attempts", 0)), str(f_list.get("total_attempts", 0))))
            out.append(row("Bucket List Completed (200 OK)", f"{b_list.get('completed_200', 0)} ({b_list.get('success_rate_pct', 0)}%)", f"{f_list.get('completed_200', 0)} ({f_list.get('success_rate_pct', 0)}%)"))
            out.append(row("Bucket List Timeouts (408)", str(b_list.get("timeouts_408", 0)), str(f_list.get("timeouts_408", 0))))
            out.append(row("Bucket List P50 Latency", f"{b_list.get('latency', {}).get('p50', 0) * 1000:.1f} ms", f"{f_list.get('latency', {}).get('p50', 0) * 1000:.1f} ms"))
            
            if b_meta and f_meta:
                out.append(row("Bucket Head (Meta) Completed", f"{b_meta.get('completed_200', 0)} ({b_meta.get('success_rate_pct', 0)}%)", f"{f_meta.get('completed_200', 0)} ({f_meta.get('success_rate_pct', 0)}%)"))
                out.append(row("Bucket Head Timeouts (408)", str(b_meta.get("timeouts_408", 0)), str(f_meta.get("timeouts_408", 0))))
                out.append(row("Bucket Head P50 Latency", f"{b_meta.get('latency', {}).get('p50', 0) * 1000:.1f} ms", f"{f_meta.get('latency', {}).get('p50', 0) * 1000:.1f} ms"))

        out.append(line("-"))
        out.append(row("CEPH SERVER LOG AUDIT", "", ""))
        out.append(line("-"))
        b_ontime = b_ceph.get("good_put_ontime_completed", b_ceph.get("good_completed_200", 0))
        f_ontime = f_ceph.get("good_put_ontime_completed", f_ceph.get("good_completed_200", 0))
        b_delayed = b_ceph.get("good_put_delayed_completed", 0)
        f_delayed = f_ceph.get("good_put_delayed_completed", 0)
        b_put_str = f"{b_ontime} on-time ({b_delayed} delayed)" if b_delayed > 0 else f"{b_ontime} (100% on-time)"
        f_put_str = f"{f_ontime} on-time ({f_delayed} delayed)" if f_delayed > 0 else f"{f_ontime} (100% on-time)"
        out.append(row("Server Good PUTs (200 OK)", b_put_str, f_put_str))

        b_put_lat = b_ceph.get("good_put_latency", b_ceph.get("good_latency", {}))
        f_put_lat = f_ceph.get("good_put_latency", f_ceph.get("good_latency", {}))
        out.append(row("Server Good PUT P50 Latency", f"{b_put_lat.get('p50', 0) * 1000:.1f} ms", f"{f_put_lat.get('p50', 0) * 1000:.1f} ms"))
        out.append(row("Server Good PUT Max Latency", f"{b_put_lat.get('max', 0) * 1000:.1f} ms", f"{f_put_lat.get('max', 0) * 1000:.1f} ms"))

        if "good_list_completed_200" in b_ceph and "good_list_completed_200" in f_ceph:
            b_list_cnt = b_ceph["good_list_completed_200"]
            f_list_cnt = f_ceph["good_list_completed_200"]
            b_list_del = b_ceph.get("good_list_delayed_completed", 0)
            f_list_del = f_ceph.get("good_list_delayed_completed", 0)
            b_l_str = f"{b_list_cnt} ({b_list_del} delayed)" if b_list_del > 0 else f"{b_list_cnt}"
            f_l_str = f"{f_list_cnt} ({f_list_del} delayed)" if f_list_del > 0 else f"{f_list_cnt}"
            out.append(row("Server Bucket Lists (200 OK)", b_l_str, f_l_str))

        if "good_meta_completed_200" in b_ceph and "good_meta_completed_200" in f_ceph:
            out.append(row("Server Bucket Heads (200 OK)", str(b_ceph["good_meta_completed_200"]), str(f_ceph["good_meta_completed_200"])))

        out.append(line("-"))
        out.append(row("OBJECTER THROTTLE (ADMIN SOCKET)", "", ""))
        out.append(line("-"))
        g_b = b_perf.get("global_throttle", {})
        g_f = f_perf.get("global_throttle", {})
        out.append(row("Global Throttle Wait Count Delta", str(g_b.get("wait_count_delta", b_perf.get("throttle_wait_count_delta", 0))), str(g_f.get("wait_count_delta", f_perf.get("throttle_wait_count_delta", 0)))))
        out.append(row("Global Throttle Wait Time Sum", f"{g_b.get('wait_sum_sec', b_perf.get('throttle_wait_sum_sec', 0.0))} s", f"{g_f.get('wait_sum_sec', f_perf.get('throttle_wait_sum_sec', 0.0))} s"))

        # Per-pool details
        b_pools = b_perf.get("per_pool", {})
        f_pools = f_perf.get("per_pool", {})
        for pname in ["default.rgw.buckets.index", "default.rgw.meta", "default.rgw.log", "good_pool", "degraded_pool"]:
            bp = b_pools.get(pname, {})
            fp = f_pools.get(pname, {})
            if bp or fp:
                b_str = f"{bp.get('wait_count_delta', 0)} waits ({bp.get('wait_sum_sec', 0.0)} s)"
                f_str = f"{fp.get('wait_count_delta', 0)} waits ({fp.get('wait_sum_sec', 0.0)} s)"
                out.append(row(f"Throttle Wait: {pname}", b_str, f_str))
    else:
        out.append(row("Good Pool Total Attempts", str(b_good["total_attempts"])))
        out.append(row("Good Pool Completed (200 OK)", f"{b_good['completed_200']} ({b_good['success_rate_pct']}%)"))
        out.append(row("Good Pool Timeouts (408)", str(b_good["timeouts_408"])))
        out.append(row("Good Pool Throughput", f"{b_good['throughput_ops_sec']} ops/s"))
        out.append(row("Good Pool P50 Latency (Client)", f"{b_good['latency']['p50'] * 1000:.1f} ms"))
        out.append(row("Good Pool P95 Latency (Client)", f"{b_good['latency']['p95'] * 1000:.1f} ms"))
        out.append(row("Good Pool Max Latency (Client)", f"{b_good['latency']['max'] * 1000:.1f} ms"))
        if "probe_list" in b_client:
            b_list = b_client["probe_list"]
            out.append(line("-"))
            out.append(row("NON-DATA PROBES (INDEX & META)", ""))
            out.append(line("-"))
            out.append(row("Bucket List Completed (200 OK)", f"{b_list.get('completed_200', 0)} ({b_list.get('success_rate_pct', 0)}%)"))
            out.append(row("Bucket List Timeouts (408)", str(b_list.get("timeouts_408", 0))))
            out.append(row("Bucket List P50 Latency", f"{b_list.get('latency', {}).get('p50', 0) * 1000:.1f} ms"))
        out.append(line("-"))
        out.append(row("CEPH SERVER LOG AUDIT", ""))
        out.append(line("-"))
        b_ontime = b_ceph.get("good_put_ontime_completed", b_ceph.get("good_completed_200", 0))
        b_delayed = b_ceph.get("good_put_delayed_completed", 0)
        b_put_str = f"{b_ontime} on-time ({b_delayed} delayed)" if b_delayed > 0 else f"{b_ontime} (100% on-time)"
        out.append(row("Server Good PUTs (200 OK)", b_put_str))
        b_put_lat = b_ceph.get("good_put_latency", b_ceph.get("good_latency", {}))
        out.append(row("Server Good PUT P50 Latency", f"{b_put_lat.get('p50', 0) * 1000:.1f} ms"))
        out.append(row("Server Good PUT Max Latency", f"{b_put_lat.get('max', 0) * 1000:.1f} ms"))
        if "good_list_completed_200" in b_ceph:
            b_list_cnt = b_ceph["good_list_completed_200"]
            b_list_del = b_ceph.get("good_list_delayed_completed", 0)
            b_l_str = f"{b_list_cnt} ({b_list_del} delayed)" if b_list_del > 0 else f"{b_list_cnt}"
            out.append(row("Server Bucket Lists (200 OK)", b_l_str))
        if "good_meta_completed_200" in b_ceph:
            out.append(row("Server Bucket Heads (200 OK)", str(b_ceph["good_meta_completed_200"])))
        out.append(line("-"))
        out.append(row("Throttle Wait Count Delta", str(b_perf.get("throttle_wait_count_delta", 0))))
        out.append(row("Throttle Wait Time Sum", f"{b_perf.get('throttle_wait_sum_sec', 0.0)} s"))

    out.append(line("="))
    return "\n".join(out)


def format_sweep_table(sweep_results):
    """Formats a multi-column ASCII table comparing multiple sweep iterations."""
    col_w = 22
    label_w = 32
    labels = [r[0] for r in sweep_results]
    summaries = [r[1] for r in sweep_results]

    header = f"| {'METRIC':<{label_w}} | " + " | ".join([f"{lbl:<{col_w}}" for lbl in labels]) + " |"
    total_len = len(header)
    line_eq = "=" * total_len
    line_dash = "-" * total_len

    out = ["\n" + line_eq, header, line_eq]

    # Global Inflight Limit
    inflight_limits = [str(s["config"]["inflight_ops"]) for s in summaries]
    out.append(f"| {'Global In-Flight Ops Limit':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in inflight_limits]) + " |")

    # Degraded Workers
    deg_workers = [str(s["config"]["degraded_workers"]) for s in summaries]
    out.append(f"| {'Degraded Workers':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in deg_workers]) + " |")

    # Degraded Op Cap
    caps = []
    for s in summaries:
        cfg = s["config"]
        if not cfg["pool_throttle_enable"]:
            caps.append(f"{cfg['inflight_ops']} ops (Shared)")
        else:
            r = cfg["pool_ratio"]
            inflight = cfg["inflight_ops"]
            cap = max(1, int(r * inflight))
            caps.append(f"{cap} ops ({int(r*100)}%)")
    out.append(f"| {'Degraded Pool Op Cap':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in caps]) + " |")
    out.append(line_dash)

    # Good Pool Completed
    completions = [f"{s['client_metrics']['good_pool']['completed_200']} ({s['client_metrics']['good_pool']['success_rate_pct']}%)" for s in summaries]
    out.append(f"| {'Good Pool Completed (200 OK)':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in completions]) + " |")

    # Timeouts
    timeouts = [str(s['client_metrics']['good_pool']['timeouts_408']) for s in summaries]
    out.append(f"| {'Good Pool Timeouts (408)':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in timeouts]) + " |")

    # Throughput
    tputs = [f"{s['client_metrics']['good_pool']['throughput_ops_sec']} ops/s" for s in summaries]
    out.append(f"| {'Good Pool Throughput':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in tputs]) + " |")

    # Latencies
    p50_client = [f"{s['client_metrics']['good_pool']['latency']['p50'] * 1000:.1f} ms" for s in summaries]
    out.append(f"| {'Good Pool P50 Latency (Client)':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in p50_client]) + " |")

    p95_client = [f"{s['client_metrics']['good_pool']['latency']['p95'] * 1000:.1f} ms" for s in summaries]
    out.append(f"| {'Good Pool P95 Latency (Client)':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in p95_client]) + " |")

    out.append(line_dash)

    # Ceph Server Latencies
    p50_ceph = [f"{s['server_ceph_metrics']['good_latency']['p50'] * 1000:.1f} ms" for s in summaries]
    out.append(f"| {'Ceph Server P50 Latency':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in p50_ceph]) + " |")

    max_ceph = [f"{s['server_ceph_metrics']['good_latency']['max'] * 1000:.1f} ms" for s in summaries]
    out.append(f"| {'Ceph Server Max Latency':<{label_w}} | " + " | ".join([f"{c:<{col_w}}" for c in max_ceph]) + " |")

    out.append(line_eq)
    return "\n".join(out)


# ==============================================================================
# 6. Main Entrypoint
# ==============================================================================
def parse_arguments():
    parser = argparse.ArgumentParser(
        description="Automated degraded PG contention and Objecter per-pool throttling orchestrator."
    )
    parser.add_argument(
        "--mode",
        choices=["compare", "compare-async", "baseline", "fixed", "sweep"],
        default="compare",
        help="Test mode: 'compare' runs baseline vs fixed; 'compare-async' runs sync vs async queue; 'sweep' runs parameter sweep; "
             "'baseline' runs legacy global throttle; 'fixed' runs per-pool throttle.",
    )
    parser.add_argument(
        "--sweep-param",
        choices=["ratio", "degraded_workers", "inflight_ops"],
        default="ratio",
        help="Parameter to sweep over when --mode sweep is selected.",
    )
    parser.add_argument(
        "--sweep-values",
        default=None,
        help="Comma-separated values for parameter sweep (e.g. '0.2,0.5,0.8,0.95' or '20,60,120,200').",
    )
    parser.add_argument("--endpoint", default="http://10.0.0.2:8000", help="S3 endpoint URL")
    parser.add_argument("--good-bucket", default="bucket-good", help="Bucket backed by healthy pool")
    parser.add_argument("--degraded-bucket", default="bucket-degraded", help="Bucket backed by degraded pool")
    parser.add_argument("--degraded-pool", default="degraded_pool", help="RADOS pool name to degrade")
    parser.add_argument("--fault-osd", type=int, default=2, help="OSD ID to stop for fault injection")
    parser.add_argument("--good-workers", type=int, default=5, help="Concurrent workers for good pool")
    parser.add_argument("--good-rate", type=float, default=5.0, help="Target request rate per good worker (/s)")
    parser.add_argument("--degraded-workers", type=int, default=100, help="Concurrent workers for degraded pool")
    parser.add_argument("--duration", type=float, default=15.0, help="Duration of load test in seconds")
    parser.add_argument("--good-timeout", type=float, default=10.0, help="S3 client timeout for good requests (s)")
    parser.add_argument("--degraded-timeout", type=float, default=15.0, help="S3 client timeout for degraded requests (s)")
    parser.add_argument("--objecter-inflight-ops", type=int, default=50, help="Objecter inflight ops limit")
    parser.add_argument("--pool-ratio", type=float, default=0.5, help="objecter_pool_inflight_ops_ratio")
    parser.add_argument("--pool-throttle-async", dest="pool_throttle_async", action="store_true", default=True, help="Enable async non-blocking queueing in Objecter")
    parser.add_argument("--no-pool-throttle-async", dest="pool_throttle_async", action="store_false", help="Disable async non-blocking queueing in Objecter")
    parser.add_argument("--queue-ratio", type=float, default=4.0, help="objecter_pool_throttle_queue_ratio")
    parser.add_argument("--max-queue-ops", type=int, default=0, help="objecter_pool_throttle_max_queue_ops")
    parser.add_argument("--sweep-ratios", default="0.2,0.5,0.8,0.95", help="Comma-separated ratios for --mode sweep (alias for --sweep-values with --sweep-param ratio)")
    parser.add_argument("--include-baseline", action="store_true", help="Include legacy baseline as first column in sweep")
    parser.add_argument("--output-dir", default="/home/ultron/development/49", help="Results output directory")
    parser.add_argument("--run-name", default=None, help="Optional custom run name prefix")
    parser.add_argument("--skip-fault", action="store_true", help="Skip degraded fault injection (clean run)")
    parser.add_argument("--disable-probes", action="store_true", help="Disable non-data probes (list_objects, head_bucket)")
    parser.add_argument("--probe-rate", type=float, default=2.0, help="Request rate for non-data probes (/s)")
    parsed = parser.parse_args()
    parsed.enable_probes = not parsed.disable_probes
    return parsed


def main():
    args = parse_arguments()
    runner = ExperimentRunner(args)

    # Clean signal handling
    def sig_handler(signum, frame):
        logger.warning(f"Caught signal {signum}, initiating emergency cluster recovery...")
        runner.cluster.restore_cluster(args.degraded_pool, args.fault_osd)
        sys.exit(1)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    base_name = args.run_name or "objecter_contention_exp"

    if args.mode == "compare":
        logger.info("Executing Comparative Benchmark: Run 1 (Baseline) vs Run 2 (Fixed)")

        # Run 1: Baseline (legacy global throttle)
        res_baseline = runner.run_experiment(
            run_name=f"{base_name}_baseline",
            pool_throttle_enable=False,
            inflight_ops=args.objecter_inflight_ops,
            pool_ratio=args.pool_ratio,
            skip_fault=args.skip_fault,
        )

        logger.info("Cooling down cluster for 5 seconds before Run 2...")
        time.sleep(5)

        # Run 2: Fixed (per-pool partitioned throttle)
        res_fixed = runner.run_experiment(
            run_name=f"{base_name}_fixed",
            pool_throttle_enable=True,
            inflight_ops=args.objecter_inflight_ops,
            pool_ratio=args.pool_ratio,
            skip_fault=args.skip_fault,
        )

        # Print comparison table
        table = format_summary_table(res_baseline, res_fixed)
        print(table)

    elif args.mode == "compare-async":
        logger.info("Executing Comparative Benchmark: Run 1 (Synchronous Futex Blocking) vs Run 2 (Option A Asynchronous Queue)")

        # Run 1: Synchronous Blocking (per-pool throttling enabled, but async queue disabled)
        res_sync = runner.run_experiment(
            run_name=f"{base_name}_sync_blocking",
            pool_throttle_enable=True,
            pool_throttle_async=False,
            inflight_ops=args.objecter_inflight_ops,
            pool_ratio=args.pool_ratio,
            skip_fault=args.skip_fault,
        )

        logger.info("Cooling down cluster for 5 seconds before Run 2...")
        time.sleep(5)

        # Run 2: Asynchronous Non-Blocking Queue (Option A)
        res_async = runner.run_experiment(
            run_name=f"{base_name}_option_a_async",
            pool_throttle_enable=True,
            pool_throttle_async=True,
            queue_ratio=args.queue_ratio,
            max_queue_ops=args.max_queue_ops,
            inflight_ops=args.objecter_inflight_ops,
            pool_ratio=args.pool_ratio,
            skip_fault=args.skip_fault,
        )

        # Print comparison table
        table = format_summary_table(res_sync, res_async)
        print(table)

    elif args.mode == "sweep":
        sweep_results = []

        if args.sweep_param == "degraded_workers":
            val_str = args.sweep_values or "20,60,120,200"
            workers_list = [int(x.strip()) for x in val_str.split(",") if x.strip()]
            logger.info(f"Executing Degraded Workers Sweep: {workers_list} (include_baseline={args.include_baseline})")

            if args.include_baseline:
                max_w = max(workers_list)
                logger.info(f"Running Baseline iteration with {max_w} degraded workers...")
                res_base = runner.run_experiment(
                    run_name=f"{base_name}_baseline_{max_w}w",
                    pool_throttle_enable=False,
                    inflight_ops=args.objecter_inflight_ops,
                    pool_ratio=args.pool_ratio,
                    degraded_workers=max_w,
                    skip_fault=args.skip_fault,
                )
                sweep_results.append((f"Baseline ({max_w}w)", res_base))
                logger.info("Cooling down cluster for 5 seconds before next iteration...")
                time.sleep(5)

            for w in workers_list:
                logger.info(f"\n--- Running Sweep Iteration: degraded_workers = {w} ---")
                res = runner.run_experiment(
                    run_name=f"{base_name}_deg_{w}w",
                    pool_throttle_enable=True,
                    inflight_ops=args.objecter_inflight_ops,
                    pool_ratio=args.pool_ratio,
                    degraded_workers=w,
                    skip_fault=args.skip_fault,
                )
                sweep_results.append((f"{w} Workers", res))
                logger.info("Cooling down cluster for 5 seconds before next iteration...")
                time.sleep(5)

        elif args.sweep_param == "inflight_ops":
            val_str = args.sweep_values or "20,50,100,200"
            ops_list = [int(x.strip()) for x in val_str.split(",") if x.strip()]
            logger.info(f"Executing Inflight Ops Sweep: {ops_list} (include_baseline={args.include_baseline})")

            if args.include_baseline:
                logger.info("Running Baseline iteration with 50 inflight ops...")
                res_base = runner.run_experiment(
                    run_name=f"{base_name}_baseline_50ops",
                    pool_throttle_enable=False,
                    inflight_ops=50,
                    pool_ratio=args.pool_ratio,
                    skip_fault=args.skip_fault,
                )
                sweep_results.append(("Baseline (50 ops)", res_base))
                logger.info("Cooling down cluster for 5 seconds before next iteration...")
                time.sleep(5)

            for ops in ops_list:
                logger.info(f"\n--- Running Sweep Iteration: inflight_ops = {ops} ---")
                res = runner.run_experiment(
                    run_name=f"{base_name}_ops_{ops}",
                    pool_throttle_enable=True,
                    inflight_ops=ops,
                    pool_ratio=args.pool_ratio,
                    skip_fault=args.skip_fault,
                )
                sweep_results.append((f"{ops} Inflight Ops", res))
                logger.info("Cooling down cluster for 5 seconds before next iteration...")
                time.sleep(5)

        else:  # default: ratio
            val_str = args.sweep_values or args.sweep_ratios
            ratios = [float(r.strip()) for r in val_str.split(",") if r.strip()]
            logger.info(f"Executing Ratio Sensitivity Sweep: {ratios} (include_baseline={args.include_baseline})")

            if args.include_baseline:
                logger.info("Running Baseline iteration for sweep...")
                res_base = runner.run_experiment(
                    run_name=f"{base_name}_baseline",
                    pool_throttle_enable=False,
                    inflight_ops=args.objecter_inflight_ops,
                    pool_ratio=args.pool_ratio,
                    skip_fault=args.skip_fault,
                )
                sweep_results.append(("Baseline (Global)", res_base))
                logger.info("Cooling down cluster for 5 seconds before next iteration...")
                time.sleep(5)

            for r in ratios:
                logger.info(f"\n--- Running Sweep Iteration: pool_ratio = {r} ---")
                pct = int(r * 100)
                res = runner.run_experiment(
                    run_name=f"{base_name}_ratio_{pct}",
                    pool_throttle_enable=True,
                    inflight_ops=args.objecter_inflight_ops,
                    pool_ratio=r,
                    skip_fault=args.skip_fault,
                )
                sweep_results.append((f"Ratio {r} ({pct}%)", res))
                logger.info("Cooling down cluster for 5 seconds before next iteration...")
                time.sleep(5)

        table = format_sweep_table(sweep_results)
        print(table)

        # Save sweep summary JSON
        ts_suffix = datetime.now().strftime("%Y%m%d_%H%M%S")
        sweep_out = os.path.join(args.output_dir, f"{base_name}_sweep_{args.sweep_param}_{ts_suffix}.json")
        with open(sweep_out, "w") as f:
            json.dump([{"label": lbl, "data": d} for lbl, d in sweep_results], f, indent=2)
        logger.info(f"Sweep results successfully archived to {sweep_out}")

    elif args.mode == "baseline":
        res = runner.run_experiment(
            run_name=f"{base_name}_baseline",
            pool_throttle_enable=False,
            inflight_ops=args.objecter_inflight_ops,
            pool_ratio=args.pool_ratio,
            skip_fault=args.skip_fault,
        )
        table = format_summary_table(res)
        print(table)

    elif args.mode == "fixed":
        res = runner.run_experiment(
            run_name=f"{base_name}_fixed",
            pool_throttle_enable=True,
            inflight_ops=args.objecter_inflight_ops,
            pool_ratio=args.pool_ratio,
            skip_fault=args.skip_fault,
        )
        table = format_summary_table(res)
        print(table)


if __name__ == "__main__":
    main()
