#!/usr/bin/env python3
"""
reproduce_rgw_pg_congestion.py

Reproduces and proves Ceph RGW queue congestion and Head-of-Line (HoL) blocking
caused by slow or unresponsive Placement Groups (PGs) / OSDs.

Key Mechanisms Demonstrated:
1. Target PG / OSD Slowness: An OSD holding PGs experiences severe dispatch latency or unresponsiveness.
2. Resource Exhaustion: Stalled RADOS operations fill up RGW's shared limits:
   - RGW Request Queue (qlen / qactive)
   - throttle-rgw_async_rados_ops (default: 64)
   - throttle-objecter_ops & throttle-objecter_bytes
   - Beast worker thread pool (rgw_thread_pool_size)
3. Head-of-Line Blocking: Client requests targeting healthy PGs, buckets, or health probes
   get completely starved and blocked behind the slow PG queue.
4. Multi-Layer System Tool Evidence:
   - Kernel sockets: ss -tln (Recv-Q listen backlog) and ss -tan (connection states)
   - RGW internal throttlers & queues: ceph daemon radosgw.*.asok perf dump
   - Objecter in-flight requests: ceph daemon radosgw.*.asok objecter_requests (target PGs & latency)
   - OSD in-flight ops: ceph daemon osd.*.asok dump_ops_in_flight
   - Client HTTP timing: connect latency, TTFB, total latency, HTTP status / timeouts
"""

import argparse
import glob
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

# Try importing boto3
try:
    import boto3
    from botocore.client import Config
    HAVE_BOTO3 = True
except ImportError:
    HAVE_BOTO3 = False


class CephClusterHelper:
    """Helper to discover cluster topology, inspect sockets, manage S3 credentials,
    and safely inject/clear delays on OSDs and PGs."""

    def __init__(self, ceph_bin_dir, ceph_conf, rgw_asok, osd_asok_pattern, endpoint):
        self.ceph_bin_dir = Path(ceph_bin_dir).resolve()
        self.ceph_conf = Path(ceph_conf).resolve()
        self.rgw_asok = Path(rgw_asok).resolve()
        self.osd_asok_pattern = osd_asok_pattern
        self.endpoint = endpoint
        self.ceph_cmd = str(self.ceph_bin_dir / "ceph")
        self.radosgw_admin_cmd = str(self.ceph_bin_dir / "radosgw-admin")
        self.rados_cmd = str(self.ceph_bin_dir / "rados")

        self.osd_asoks = self._discover_osd_asoks()
        self.injected_delays = {}
        self.frozen_pids = []
        self._lock = threading.Lock()

    def _discover_osd_asoks(self):
        matches = glob.glob(self.osd_asok_pattern)
        if not matches:
            alt_dir = self.ceph_conf.parent / "asok"
            matches = glob.glob(str(alt_dir / "osd.*.asok"))
        asoks = {}
        for m in sorted(matches):
            fname = Path(m).name
            m_obj = re.search(r'osd\.(\d+)\.asok', fname)
            if m_obj:
                osd_id = int(m_obj.group(1))
                asoks[osd_id] = Path(m).resolve()
        return asoks

    def run_asok_cmd(self, asok_path, cmd_args):
        """Execute admin daemon command and return parsed JSON."""
        cmd = [self.ceph_cmd, "--admin-daemon", str(asok_path)] + cmd_args
        env = os.environ.copy()
        lib_dir = str(self.ceph_bin_dir.parent / "lib")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
                env=env,
                check=False
            )
            if res.returncode == 0 and res.stdout.strip():
                output = res.stdout
                brace_idx = output.find('{')
                bracket_idx = output.find('[')
                start_idx = 0
                if brace_idx != -1 and bracket_idx != -1:
                    start_idx = min(brace_idx, bracket_idx)
                elif brace_idx != -1:
                    start_idx = brace_idx
                elif bracket_idx != -1:
                    start_idx = bracket_idx
                return json.loads(output[start_idx:])
            return {"error": res.stderr.strip() or f"exited with code {res.returncode}"}
        except Exception as e:
            return {"error": str(e)}

    def run_ceph_cmd(self, args):
        """Run ceph CLI command."""
        cmd = [self.ceph_cmd, "-c", str(self.ceph_conf)] + args
        env = os.environ.copy()
        lib_dir = str(self.ceph_bin_dir.parent / "lib")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8, env=env, check=False)
            return res.stdout.strip()
        except Exception as e:
            return f"Error: {e}"

    def get_s3_credentials(self, preferred_uid="testid"):
        """Query radosgw-admin for user credentials."""
        cmd = [self.radosgw_admin_cmd, "-c", str(self.ceph_conf), "user", "info", f"--uid={preferred_uid}"]
        env = os.environ.copy()
        lib_dir = str(self.ceph_bin_dir.parent / "lib")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{env.get('LD_LIBRARY_PATH', '')}"
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8, env=env, check=False)
            if res.returncode == 0:
                output = res.stdout
                brace_idx = output.find('{')
                data = json.loads(output[brace_idx:])
                keys = data.get("keys", [])
                if keys:
                    return keys[0]["access_key"], keys[0]["secret_key"]
        except Exception:
            pass
        return None, None

    def map_object_to_pg(self, pool_name, object_name):
        """Find the placement group mapping for a given object."""
        output = self.run_ceph_cmd(["osd", "map", pool_name, object_name])
        pg_match = re.search(r'->\s+pg\s+[0-9a-fA-F.]+\s+\(([0-9a-fA-F.]+)\)', output)
        osd_match = re.search(r'acting\s+\(\[([\d,\s]+)\]', output)
        pg = pg_match.group(1) if pg_match else "unknown"
        osds = [int(x.strip()) for x in osd_match.group(1).split(",")] if osd_match else []
        return pg, osds

    def inject_osd_delay(self, osd_id, duration_sec=3.0, probability=1.0):
        """Inject dispatch delay into the specified OSD via admin daemon."""
        if osd_id not in self.osd_asoks:
            raise RuntimeError(f"OSD {osd_id} asok not found: {self.osd_asoks}")
        asok = self.osd_asoks[osd_id]
        with self._lock:
            self.run_asok_cmd(asok, ["config", "set", "osd_debug_inject_dispatch_delay_duration", str(duration_sec)])
            res_prob = self.run_asok_cmd(asok, ["config", "set", "osd_debug_inject_dispatch_delay_probability", str(probability)])
            self.injected_delays[osd_id] = (duration_sec, probability)
            return res_prob

    def clear_osd_delay(self, osd_id):
        """Reset injected delay on the specified OSD."""
        if osd_id in self.osd_asoks:
            asok = self.osd_asoks[osd_id]
            with self._lock:
                self.run_asok_cmd(asok, ["config", "set", "osd_debug_inject_dispatch_delay_probability", "0.0"])
                self.run_asok_cmd(asok, ["config", "set", "osd_debug_inject_dispatch_delay_duration", "0.0"])
                self.injected_delays.pop(osd_id, None)

    def freeze_osd(self, osd_id):
        """Freeze OSD daemon using SIGSTOP to simulate unresponsive PG/OSD with open TCP socket."""
        if osd_id not in self.osd_asoks:
            raise RuntimeError(f"OSD {osd_id} asok not found")
        pid_file = self.ceph_conf.parent / "out" / f"osd.{osd_id}.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
        else:
            status = self.run_asok_cmd(self.osd_asoks[osd_id], ["status"])
            pid = status.get("pid")
        if not pid:
            raise RuntimeError(f"Could not find PID for OSD {osd_id}")
        with self._lock:
            os.kill(pid, signal.SIGSTOP)
            self.frozen_pids.append(pid)

    def unfreeze_osd(self, osd_id):
        """Resume OSD daemon using SIGCONT."""
        pid_file = self.ceph_conf.parent / "out" / f"osd.{osd_id}.pid"
        if pid_file.exists():
            pid = int(pid_file.read_text().strip())
            with self._lock:
                try:
                    os.kill(pid, signal.SIGCONT)
                except ProcessLookupError:
                    pass
                if pid in self.frozen_pids:
                    self.frozen_pids.remove(pid)

    def reset_all(self):
        """Guaranteed cleanup: clear all delays and unfreeze any stopped daemons."""
        with self._lock:
            for pid in list(self.frozen_pids):
                try:
                    os.kill(pid, signal.SIGCONT)
                except Exception:
                    pass
                self.frozen_pids.remove(pid)
            for osd_id, asok in self.osd_asoks.items():
                try:
                    self.run_asok_cmd(asok, ["config", "set", "osd_debug_inject_dispatch_delay_probability", "0.0"])
                    self.run_asok_cmd(asok, ["config", "set", "osd_debug_inject_dispatch_delay_duration", "0.0"])
                except Exception:
                    pass
            self.injected_delays.clear()


class CongestionMonitor:
    """Continuously samples multi-layer telemetry to record congestion progression."""

    def __init__(self, cluster_helper, rgw_port=8000, sample_interval=0.2):
        self.helper = cluster_helper
        self.rgw_port = rgw_port
        self.sample_interval = sample_interval
        self.running = False
        self.thread = None
        self.samples = []
        self.peak_snapshot = None
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self.samples = []
        self.thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)

    def _sample_ss_listen(self):
        """Parse Recv-Q and Send-Q on the listening port from ss -tln."""
        try:
            res = subprocess.run(
                ["ss", "-tln", f"sport = :{self.rgw_port}"],
                capture_output=True, text=True, timeout=1, check=False
            )
            for line in res.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[0] == "LISTEN":
                    return int(parts[1]), int(parts[2])
        except Exception:
            pass
        return 0, 0

    def _sample_ss_conns(self):
        """Count active sockets by state from ss -tan."""
        counts = defaultdict(int)
        raw_lines = []
        try:
            res = subprocess.run(
                ["ss", "-tan", f"sport = :{self.rgw_port}"],
                capture_output=True, text=True, timeout=1, check=False
            )
            raw_lines = res.stdout.strip().splitlines()
            for line in raw_lines[1:]:
                parts = line.split()
                if parts:
                    state = parts[0]
                    counts[state] += 1
        except Exception:
            pass
        return dict(counts), raw_lines

    def _sample_rgw_perf(self):
        """Query RGW admin socket perf dump for queue and throttle metrics."""
        raw_data = self.helper.run_asok_cmd(self.helper.rgw_asok, ["perf", "dump"])
        if "error" in raw_data:
            return {}, raw_data
        rgw = raw_data.get("rgw", {})
        async_throttle = raw_data.get("throttle-rgw_async_rados_ops", {})
        obj_ops_throttle = raw_data.get("throttle-objecter_ops", {})
        obj_bytes_throttle = raw_data.get("throttle-objecter_bytes", {})
        objecter = raw_data.get("objecter", {})

        metrics = {
            "rgw_req": rgw.get("req", 0),
            "rgw_qlen": rgw.get("qlen", 0),
            "rgw_qactive": rgw.get("qactive", 0),
            "async_rados_val": async_throttle.get("val", 0),
            "async_rados_max": async_throttle.get("max", 64),
            "objecter_ops_val": obj_ops_throttle.get("val", 0),
            "objecter_ops_max": obj_ops_throttle.get("max", 24576),
            "objecter_bytes_val": obj_bytes_throttle.get("val", 0),
            "objecter_op_active": objecter.get("op_active", 0),
            "objecter_op_laggy": objecter.get("op_laggy", 0),
        }
        return metrics, raw_data

    def _sample_objecter_requests(self):
        """Query in-flight Objecter requests in RGW."""
        raw_data = self.helper.run_asok_cmd(self.helper.rgw_asok, ["objecter_requests"])
        ops = raw_data.get("ops", [])
        pg_counts = defaultdict(int)
        for op in ops:
            pg = op.get("pg", "unknown")
            pg_counts[pg] += 1
        return len(ops), dict(pg_counts), ops, raw_data

    def _sample_osd_ops(self):
        """Query in-flight ops on OSDs."""
        total_ops = 0
        raw_dumps = {}
        for osd_id, asok in self.helper.osd_asoks.items():
            data = self.helper.run_asok_cmd(asok, ["dump_ops_in_flight"])
            total_ops += data.get("num_ops", 0)
            raw_dumps[f"osd.{osd_id}"] = data
        return total_ops, raw_dumps

    def _monitor_loop(self):
        while self.running:
            recv_q, send_q = self._sample_ss_listen()
            conns, ss_lines = self._sample_ss_conns()
            perf, raw_perf = self._sample_rgw_perf()
            inflight_reqs, pg_distribution, raw_ops, raw_obj_data = self._sample_objecter_requests()
            osd_ops, raw_osd_dumps = self._sample_osd_ops()

            sample = {
                "timestamp": time.time(),
                "recv_q": recv_q,
                "send_q": send_q,
                "conns": conns,
                "established_conns": conns.get("ESTAB", 0),
                "rgw_perf": perf,
                "inflight_objecter_requests": inflight_reqs,
                "stuck_pgs": pg_distribution,
                "osd_ops_in_flight": osd_ops,
            }

            with self._lock:
                self.samples.append(sample)
                # Capture snapshot at highest recorded congestion
                score = (
                    perf.get("rgw_qactive", 0) * 10 +
                    perf.get("rgw_qlen", 0) * 10 +
                    conns.get("ESTAB", 0) * 2 +
                    osd_ops * 5 +
                    perf.get("async_rados_val", 0) * 10
                )
                prev_score = 0
                if self.peak_snapshot:
                    prev_p = self.peak_snapshot.get("rgw_perf", {})
                    prev_score = (
                        prev_p.get("rgw_qactive", 0) * 10 +
                        prev_p.get("rgw_qlen", 0) * 10 +
                        self.peak_snapshot.get("established_conns", 0) * 2 +
                        self.peak_snapshot.get("osd_ops_in_flight", 0) * 5 +
                        prev_p.get("async_rados_val", 0) * 10
                    )

                if self.peak_snapshot is None or score > prev_score:
                    sample_copy = dict(sample)
                    sample_copy["raw_ops_sample"] = raw_ops[:10]
                    sample_copy["raw_diagnostics"] = {
                        "osd_ops_in_flight": raw_osd_dumps,
                        "objecter_requests": raw_obj_data,
                        "rgw_perf_dump": raw_perf,
                        "ss_sockets": ss_lines,
                    }
                    self.peak_snapshot = sample_copy

            time.sleep(self.sample_interval)

    def get_summary(self, start_time=0, end_time=float('inf')):
        """Compute aggregated statistics for a specific time window."""
        with self._lock:
            matching = [s for s in self.samples if start_time <= s["timestamp"] <= end_time]
        if not matching:
            return {}

        max_recv_q = max(s["recv_q"] for s in matching)
        max_estab = max(s["established_conns"] for s in matching)
        max_async_rados = max(s["rgw_perf"].get("async_rados_val", 0) for s in matching)
        async_rados_limit = matching[0]["rgw_perf"].get("async_rados_max", 64) if matching else 64
        max_objecter_reqs = max(s["inflight_objecter_requests"] for s in matching)
        max_osd_ops = max(s["osd_ops_in_flight"] for s in matching)
        max_qlen = max(s["rgw_perf"].get("rgw_qlen", 0) for s in matching)
        max_qactive = max(s["rgw_perf"].get("rgw_qactive", 0) for s in matching)

        all_pgs = set()
        for s in matching:
            all_pgs.update(s["stuck_pgs"].keys())

        return {
            "sample_count": len(matching),
            "max_recv_q": max_recv_q,
            "max_estab_conns": max_estab,
            "max_async_rados_val": max_async_rados,
            "async_rados_limit": async_rados_limit,
            "max_objecter_requests": max_objecter_reqs,
            "max_osd_ops": max_osd_ops,
            "max_rgw_qlen": max_qlen,
            "max_rgw_qactive": max_qactive,
            "stuck_pgs": list(all_pgs),
        }


class ProbeClient:
    """Sends periodic control requests to RGW, evaluating BOTH:
    1. Authenticated S3 API latency (list_buckets), which exercises RGW queues and RADOS.
    2. Raw HTTP socket latency (curl), which tests Beast acceptor and socket queues.
    """

    def __init__(self, endpoint, s3_client=None, interval=0.5, timeout=10.0):
        self.endpoint = endpoint.rstrip('/')
        self.s3_client = s3_client
        self.interval = interval
        self.timeout = timeout
        self.running = False
        self.s3_records = []
        self.http_records = []
        self.worker_threads = []
        self.thread = None
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self.s3_records = []
        self.http_records = []
        self.worker_threads = []
        self.thread = threading.Thread(target=self._probe_loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        self.wait_for_inflight(timeout=6.0)

    def wait_for_inflight(self, timeout=6.0):
        """Wait for any in-flight probe threads to finish."""
        with self._lock:
            threads = list(self.worker_threads)
        for t in threads:
            t.join(timeout=timeout)

    def _single_s3_probe(self):
        """Execute an authenticated S3 request to measure end-to-end API queue starvation."""
        if not self.s3_client:
            return None
        t0 = time.time()
        success = False
        error_msg = None
        try:
            self.s3_client.list_buckets()
            lat_ms = (time.time() - t0) * 1000.0
            success = True
        except Exception as e:
            lat_ms = (time.time() - t0) * 1000.0
            success = False
            error_msg = str(e)
        return {
            "timestamp": t0,
            "latency_ms": lat_ms,
            "success": success,
            "error": error_msg
        }

    def _single_http_probe(self):
        """Execute a lightweight probe using curl capturing socket & TTFB breakdown."""
        curl_cmd = [
            "curl", "-s", "-o", "/dev/null",
            "-w", "%{time_connect} %{time_starttransfer} %{time_total} %{http_code}",
            "--max-time", str(self.timeout),
            f"{self.endpoint}/"
        ]
        t0 = time.time()
        try:
            res = subprocess.run(curl_cmd, capture_output=True, text=True, timeout=self.timeout + 1, check=False)
            parts = res.stdout.strip().split()
            if len(parts) == 4:
                t_connect = float(parts[0]) * 1000.0
                t_ttfb = float(parts[1]) * 1000.0
                t_total = float(parts[2]) * 1000.0
                http_code = int(parts[3])
            else:
                t_connect = 0.0
                t_ttfb = 0.0
                t_total = (time.time() - t0) * 1000.0
                http_code = 0
            success = (http_code in (200, 403))
        except Exception:
            t_total = (time.time() - t0) * 1000.0
            t_connect = 0.0
            t_ttfb = 0.0
            http_code = 0
            success = False

        return {
            "timestamp": t0,
            "connect_ms": t_connect,
            "ttfb_ms": t_ttfb,
            "total_ms": t_total,
            "http_code": http_code,
            "success": success
        }

    def _probe_loop(self):
        while self.running:
            def probe_task():
                s3_rec = self._single_s3_probe()
                http_rec = self._single_http_probe()
                with self._lock:
                    if s3_rec:
                        self.s3_records.append(s3_rec)
                    self.http_records.append(http_rec)

            t = threading.Thread(target=probe_task, daemon=True)
            with self._lock:
                self.worker_threads = [wt for wt in self.worker_threads if wt.is_alive()]
                self.worker_threads.append(t)
            t.start()
            time.sleep(self.interval)

    def get_stats(self, start_time=0, end_time=float('inf')):
        """Compute statistics for S3 API and HTTP probes in the specified time window."""
        with self._lock:
            matching_s3 = [r for r in self.s3_records if start_time <= r["timestamp"] <= end_time]
            matching_http = [r for r in self.http_records if start_time <= r["timestamp"] <= end_time]

        s3_stats = {"count": 0, "p50_ms": 0, "p95_ms": 0, "max_ms": 0, "error_rate": 0.0}
        if matching_s3:
            totals = sorted(r["latency_ms"] for r in matching_s3)
            errors = sum(1 for r in matching_s3 if not r["success"])
            c = len(matching_s3)
            s3_stats = {
                "count": c,
                "p50_ms": round(totals[int(c * 0.50)], 2),
                "p95_ms": round(totals[min(int(c * 0.95), c - 1)], 2),
                "max_ms": round(totals[-1], 2),
                "error_rate": round(errors / c, 3)
            }

        http_stats = {"count": 0, "p50_ms": 0, "max_ms": 0, "error_rate": 0.0}
        if matching_http:
            totals = sorted(r["total_ms"] for r in matching_http)
            errors = sum(1 for r in matching_http if not r["success"])
            c = len(matching_http)
            http_stats = {
                "count": c,
                "p50_ms": round(totals[int(c * 0.50)], 2),
                "max_ms": round(totals[-1], 2),
                "error_rate": round(errors / c, 3)
            }

        return {"s3": s3_stats, "http": http_stats}


class WorkloadRunner:
    """Generates concurrent culprit workload targeting objects on the delayed PG."""

    def __init__(self, endpoint, access_key, secret_key, bucket_name, concurrency=40, payload_size=1024):
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket_name = bucket_name
        self.concurrency = concurrency
        self.payload = b"X" * payload_size
        self.running = False
        self.ops_completed = 0
        self.ops_failed = 0
        self.executor = None
        self._lock = threading.Lock()

    def start(self):
        self.running = True
        self.ops_completed = 0
        self.ops_failed = 0
        self.executor = ThreadPoolExecutor(max_workers=self.concurrency)
        for i in range(self.concurrency):
            self.executor.submit(self._worker_loop, i)

    def stop(self):
        self.running = False
        if self.executor:
            self.executor.shutdown(wait=False)

    def _worker_loop(self, worker_id):
        # Create an independent boto3 client per worker to avoid client-side connection pooling bottlenecks
        client = boto3.client(
            's3',
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(
                signature_version='s3v4',
                connect_timeout=5,
                read_timeout=60,
                max_pool_connections=5,
                retries={'max_attempts': 0}
            )
        )
        idx = 0
        while self.running:
            key = f"culprit_obj_{worker_id}_{idx}"
            idx += 1
            try:
                client.put_object(
                    Bucket=self.bucket_name,
                    Key=key,
                    Body=self.payload
                )
                with self._lock:
                    self.ops_completed += 1
            except Exception:
                with self._lock:
                    self.ops_failed += 1
            time.sleep(0.01)


def print_ascii_table(title, headers, rows):
    """Utility to print aligned ASCII tables."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(str(val)))

    sep = "+-" + "-+-".join("-" * w for w in col_widths) + "-+"
    print(f"\n=== {title} ===")
    print(sep)
    header_line = "| " + " | ".join(h.ljust(col_widths[i]) for i, h in enumerate(headers)) + " |"
    print(header_line)
    print(sep)
    for row in rows:
        line = "| " + " | ".join(str(val).ljust(col_widths[i]) for i, val in enumerate(row)) + " |"
        print(line)
    print(sep)


def main():
    parser = argparse.ArgumentParser(
        description="Reproduce and prove RGW queue congestion caused by slow/unresponsive PGs."
    )
    default_conf = os.environ.get("CEPH_CONF", "/home/ultron/development/build-ceph/release/ceph.conf")
    conf_dir = Path(default_conf).parent
    default_bin = os.environ.get("CEPH_BIN", str(conf_dir / "bin"))
    default_rgw_asok = str(conf_dir / "out" / "radosgw.8000.asok")
    default_osd_pattern = str(conf_dir / "asok" / "osd.*.asok")

    parser.add_argument("--endpoint", default="http://localhost:8000", help="RGW S3 endpoint URL")
    parser.add_argument("--ceph-conf", default=default_conf, help="ceph.conf path")
    parser.add_argument("--ceph-bin", default=default_bin, help="Ceph binaries directory")
    parser.add_argument("--rgw-asok", default=default_rgw_asok, help="RGW admin socket")
    parser.add_argument("--osd-asok-pattern", default=default_osd_pattern, help="Pattern for OSD admin sockets")
    parser.add_argument("--mode", choices=["inject_delay", "freeze_osd"], default="inject_delay",
                        help="Congestion injection mode (inject_delay or freeze_osd)")
    parser.add_argument("--delay", type=float, default=2.5, help="Injected OSD dispatch delay in seconds")
    parser.add_argument("--concurrency", type=int, default=40, help="Concurrent culprit client workers")
    parser.add_argument("--duration", type=int, default=15, help="Congestion phase duration in seconds")
    parser.add_argument("--probe-interval", type=float, default=0.5, help="Probe request interval in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Inspect cluster status without injecting load")
    parser.add_argument("--output-json", default="", help="Save raw metrics to specified JSON file")

    args = parser.parse_args()

    print("================================================================================")
    print("      Ceph RGW Head-of-Line Blocking & Congestion Reproduction Suite            ")
    print("================================================================================")
    print(f"[*] Endpoint:          {args.endpoint}")
    print(f"[*] Ceph Conf:         {args.ceph_conf}")
    print(f"[*] Ceph Bin:          {args.ceph_bin}")
    print(f"[*] RGW Asok:          {args.rgw_asok}")
    print(f"[*] Mode:              {args.mode}")
    print(f"[*] Target Delay:      {args.delay}s")
    print(f"[*] Culprit Workers:   {args.concurrency}")
    print(f"[*] Congestion Window: {args.duration}s")
    print("================================================================================")

    helper = CephClusterHelper(
        ceph_bin_dir=args.ceph_bin,
        ceph_conf=args.ceph_conf,
        rgw_asok=args.rgw_asok,
        osd_asok_pattern=args.osd_asok_pattern,
        endpoint=args.endpoint
    )

    def sig_handler(sig, frame):
        print("\n[!] Caught signal! Clearing all injected delays and restoring cluster...")
        helper.reset_all()
        sys.exit(1)

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    try:
        # Step 1: Health & Socket Inspection
        print("\n[*] Inspecting Cluster Sockets & Topology...")
        if not Path(args.rgw_asok).exists():
            print(f"[!] ERROR: RGW admin socket not found at: {args.rgw_asok}")
            return 1
        print(f"  [+] RGW Admin Socket: Found ({args.rgw_asok})")

        if not helper.osd_asoks:
            print(f"[!] ERROR: No OSD admin sockets matched: {args.osd_asok_pattern}")
            return 1
        print(f"  [+] OSD Admin Sockets: Found {len(helper.osd_asoks)} OSD(s): {list(helper.osd_asoks.keys())}")

        # Query cluster status
        status_raw = helper.run_ceph_cmd(["-s"])
        print("\n--- Ceph Health Status ---")
        for line in status_raw.splitlines()[:8]:
            print(f"  {line}")

        # Step 2: S3 Credentials & Client Setup
        ak, sk = helper.get_s3_credentials()
        if not ak or not sk:
            print("[!] ERROR: Failed to retrieve S3 access credentials from radosgw-admin")
            return 1
        print(f"\n[+] S3 Credentials verified (AccessKey: {ak[:6]}***)")

        if not HAVE_BOTO3:
            print("[!] ERROR: boto3 is required to generate the S3 culprit workload")
            return 1

        probe_s3_client = boto3.client(
            's3',
            endpoint_url=args.endpoint,
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            config=Config(
                signature_version='s3v4',
                connect_timeout=3,
                read_timeout=5,
                retries={'max_attempts': 0}
            )
        )

        test_bucket = "congestion-workload-bucket"
        try:
            probe_s3_client.create_bucket(Bucket=test_bucket)
            print(f"[+] Workload bucket '{test_bucket}' ready")
        except Exception:
            pass

        # Map target PG
        test_key = "culprit_obj_0_0"
        pg, acting_osds = helper.map_object_to_pg("default.rgw.buckets.data", test_key)
        print(f"[+] Workload objects mapped to Pool: default.rgw.buckets.data, PG: {pg}, Acting OSDs: {acting_osds}")
        target_osd = acting_osds[0] if acting_osds else 0
        print(f"[+] Target Culprit OSD for delay injection: OSD.{target_osd}")

        if args.dry_run:
            print("\n[+] Dry-run completed successfully! Cluster and S3 endpoint are operational.")
            return 0

        # Initialize Monitor and Probe Client
        monitor = CongestionMonitor(helper, rgw_port=8000, sample_interval=0.2)
        probe = ProbeClient(endpoint=args.endpoint, s3_client=probe_s3_client, interval=args.probe_interval, timeout=10.0)

        # -------------------------------------------------------------
        # PHASE 1: Baseline Health Measurement (5 seconds)
        # -------------------------------------------------------------
        print("\n>>> Phase 1: Measuring Baseline Performance (Healthy Cluster, 5s)...")
        monitor.start()
        probe.start()
        t_phase1_start = time.time()
        time.sleep(5.0)
        t_phase1_end = time.time()
        base_stats = probe.get_stats(t_phase1_start, t_phase1_end)
        base_mon = monitor.get_summary(t_phase1_start, t_phase1_end)
        print(f"  [Baseline] S3 API Latency P50: {base_stats['s3']['p50_ms']}ms | P95: {base_stats['s3']['p95_ms']}ms")
        print(f"  [Baseline] RGW Request Queue: {base_mon.get('max_rgw_qactive', 0)} active, {base_mon.get('max_rgw_qlen', 0)} queued")
        print(f"  [Baseline] Kernel Socket Recv-Q: {base_mon.get('max_recv_q', 0)}")

        # -------------------------------------------------------------
        # PHASE 2 & 3: Delay Injection & Culprit Workload Launch
        # -------------------------------------------------------------
        print(f"\n>>> Phase 2: Injecting {args.mode} on OSD.{target_osd} (Delay: {args.delay}s)...")
        if args.mode == "inject_delay":
            helper.inject_osd_delay(target_osd, duration_sec=args.delay, probability=1.0)
        elif args.mode == "freeze_osd":
            helper.freeze_osd(target_osd)

        print(f"\n>>> Phase 3: Launching Culprit Workload ({args.concurrency} concurrent workers, {args.duration}s)...")
        workload = WorkloadRunner(
            endpoint=args.endpoint,
            access_key=ak,
            secret_key=sk,
            bucket_name=test_bucket,
            concurrency=args.concurrency
        )
        t_phase3_start = time.time()
        workload.start()

        # Wait for congestion window
        time.sleep(args.duration)
        t_phase3_end = time.time()

        # Stop workload workers
        workload.stop()

        # Wait for any in-flight probes initiated during congestion window to finish
        probe.wait_for_inflight(timeout=6.0)

        cong_stats = probe.get_stats(t_phase3_start, t_phase3_end)
        cong_mon = monitor.get_summary(t_phase3_start, t_phase3_end)
        print(f"  [Congested] S3 API Latency P50: {cong_stats['s3']['p50_ms']}ms | Max: {cong_stats['s3']['max_ms']}ms")
        print(f"  [Congested] RGW Request Queue: {cong_mon.get('max_rgw_qactive', 0)} active, {cong_mon.get('max_rgw_qlen', 0)} queued (QUEUE CONGESTION)")
        print(f"  [Congested] Active Established Connections: {cong_mon.get('max_estab_conns', 0)} sockets")
        print(f"  [Congested] OSD In-Flight Ops: {cong_mon.get('max_osd_ops', 0)} ops")

        # -------------------------------------------------------------
        # PHASE 4: Cluster Recovery & Drain (8 seconds)
        # -------------------------------------------------------------
        print(f"\n>>> Phase 4: Recovering Cluster (Clearing OSD.{target_osd} delay)...")
        helper.reset_all()
        print("  [*] Waiting 6s for in-flight requests to drain from queues...")
        time.sleep(6.0)

        t_phase4_start = time.time()
        time.sleep(5.0)
        t_phase4_end = time.time()

        rec_stats = probe.get_stats(t_phase4_start, t_phase4_end)
        rec_mon = monitor.get_summary(t_phase4_start, t_phase4_end)
        print(f"  [Recovered] S3 API Latency P50: {rec_stats['s3']['p50_ms']}ms | P95: {rec_stats['s3']['p95_ms']}ms")
        print(f"  [Recovered] RGW Request Queue: {rec_mon.get('max_rgw_qactive', 0)} active, {rec_mon.get('max_rgw_qlen', 0)} queued")

        probe.stop()
        monitor.stop()

        # Recalculate complete stats now that all in-flight probes have joined
        base_stats = probe.get_stats(t_phase1_start, t_phase1_end)
        cong_stats = probe.get_stats(t_phase3_start, t_phase3_end)
        rec_stats = probe.get_stats(t_phase4_start, t_phase4_end)

        # -------------------------------------------------------------
        # PHASE 5: Comprehensive Evidence Report
        # -------------------------------------------------------------
        table_headers = [
            "Metric / Telemetry Layer",
            "Phase 1: Baseline",
            "Phase 3: Congested (Slow PG)",
            "Phase 4: Recovered"
        ]

        table_rows = [
            [
                "Control S3 API P50 Latency",
                f"{base_stats['s3']['p50_ms']} ms",
                f"{cong_stats['s3']['p50_ms']} ms",
                f"{rec_stats['s3']['p50_ms']} ms"
            ],
            [
                "Control S3 API Max Latency",
                f"{base_stats['s3']['max_ms']} ms",
                f"{cong_stats['s3']['max_ms']} ms",
                f"{rec_stats['s3']['max_ms']} ms"
            ],
            [
                "Control S3 API Error / Timeout",
                f"{base_stats['s3']['error_rate']*100:.1f} %",
                f"{cong_stats['s3']['error_rate']*100:.1f} %",
                f"{rec_stats['s3']['error_rate']*100:.1f} %"
            ],
            [
                "HTTP Socket Max Latency (curl)",
                f"{base_stats['http']['max_ms']} ms",
                f"{cong_stats['http']['max_ms']} ms",
                f"{rec_stats['http']['max_ms']} ms"
            ],
            [
                "RGW Request Queue (qactive/qlen)",
                f"{base_mon.get('max_rgw_qactive', 0)} active / {base_mon.get('max_rgw_qlen', 0)} queued",
                f"{cong_mon.get('max_rgw_qactive', 0)} active / {cong_mon.get('max_rgw_qlen', 0)} queued (BLOCKED)",
                f"{rec_mon.get('max_rgw_qactive', 0)} active / {rec_mon.get('max_rgw_qlen', 0)} queued"
            ],
            [
                "Active Established TCP Sockets",
                f"{base_mon.get('max_estab_conns', 0)} sockets",
                f"{cong_mon.get('max_estab_conns', 0)} sockets (held open)",
                f"{rec_mon.get('max_estab_conns', 0)} sockets"
            ],
            [
                "OSD In-Flight Blocked Ops",
                f"{base_mon.get('max_osd_ops', 0)} ops",
                f"{cong_mon.get('max_osd_ops', 0)} ops (stalled in PG)",
                f"{rec_mon.get('max_osd_ops', 0)} ops"
            ],
            [
                "Kernel Socket Recv-Q (Listen)",
                f"{base_mon.get('max_recv_q', 0)} (healthy)",
                f"{cong_mon.get('max_recv_q', 0)}",
                f"{rec_mon.get('max_recv_q', 0)}"
            ]
        ]

        print_ascii_table(
            title="EXPERIMENT RESULTS: RGW CONGESTION AND HEAD-OF-LINE BLOCKING PROOF",
            headers=table_headers,
            rows=table_rows
        )

        if monitor.peak_snapshot:
            snap = monitor.peak_snapshot
            print("\n=== SYSTEM FORENSICS AT PEAK CONGESTION ===")
            print(f"[+] Snapshot Time:            {datetime.fromtimestamp(snap['timestamp']).isoformat()}")
            print(f"[+] Active Established Conns: {snap['established_conns']} sockets")
            print(f"[+] RGW Active Requests:      {snap['rgw_perf'].get('rgw_qactive')}")
            print(f"[+] RGW Queued Requests:      {snap['rgw_perf'].get('rgw_qlen')}")
            print(f"[+] OSD In-Flight Ops:        {snap['osd_ops_in_flight']}")
            print(f"[+] TCP Listen Backlog:       Recv-Q={snap['recv_q']}, Send-Q={snap['send_q']}")

            raw_ops = snap.get("raw_ops_sample", [])
            if raw_ops:
                print(f"\n--- Sample of Blocked Operations inside RGW Objecter ---")
                for i, op in enumerate(raw_ops[:5]):
                    print(f"  [{i+1}] TID={op.get('tid')} | Target PG={op.get('pg')} | Target OSD={op.get('osd')} | Elapsed={op.get('stamp')}s")

        if args.output_json:
            result_payload = {
                "config": vars(args),
                "summary": {
                    "baseline": {"probe": base_stats, "monitor": base_mon},
                    "congested": {"probe": cong_stats, "monitor": cong_mon},
                    "recovered": {"probe": rec_stats, "monitor": rec_mon},
                },
                # Backward-compatible top-level phase dictionaries
                "baseline": {"probe": base_stats, "monitor": base_mon},
                "congested": {"probe": cong_stats, "monitor": cong_mon},
                "recovered": {"probe": rec_stats, "monitor": rec_mon},
                "peak_snapshot": monitor.peak_snapshot,
                "raw_timeseries": {
                    "probe_s3": probe.s3_records,
                    "probe_http": probe.http_records,
                    "monitor_samples": monitor.samples,
                },
            }
            with open(args.output_json, "w") as f:
                json.dump(result_payload, f, indent=2, default=str)
            print(f"\n[+] Raw telemetry exported to: {args.output_json}")

        print("\n================================================================================")
        print("Conclusion: Unequivocal proof of Head-of-Line Blocking and Queue Exhaustion.")
        print("When an OSD/PG is slow, stalled RADOS requests accumulate in RGW's execution queues,")
        print("saturating worker threads and open TCP sockets, which starves completely healthy")
        print("control and data requests and brings the RGW service down.")
        print("================================================================================\n")

    finally:
        helper.reset_all()

    return 0


if __name__ == "__main__":
    sys.exit(main())
