#!/usr/bin/env python3
"""
Live S3 Benchmark Runner for Ceph RGW with dmClock & Adaptive AIMD validation.
Uses standard Python library (urllib, hmac, hashlib, concurrent.futures) with AWS SigV4.
"""

import sys
import os
import time
import json
import hmac
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone
import concurrent.futures
from pathlib import Path

def sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()

def get_signature_key(key, date_stamp, region_name, service_name):
    k_date = sign(('AWS4' + key).encode('utf-8'), date_stamp)
    k_region = sign(k_date, region_name)
    k_service = sign(k_region, service_name)
    k_signing = sign(k_service, 'aws4_request')
    return k_signing

class S3Client:
    def __init__(self, endpoint_url, access_key, secret_key, region="default"):
        self.endpoint = endpoint_url.rstrip('/')
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region

    def request(self, method, bucket, key="", data=b"", headers=None):
        if headers is None:
            headers = {}

        now = datetime.now(timezone.utc)
        amz_date = now.strftime('%Y%m%dT%H%M%SZ')
        date_stamp = now.strftime('%Y%m%d')

        canonical_uri = '/' + bucket
        if key:
            canonical_uri += '/' + key.lstrip('/')

        canonical_querystring = ''
        payload_hash = hashlib.sha256(data).hexdigest()

        host = self.endpoint.replace('http://', '').replace('https://', '')
        
        req_headers = {
            'host': host,
            'x-amz-date': amz_date,
            'x-amz-content-sha256': payload_hash,
        }
        if data:
            req_headers['content-length'] = str(len(data))
        req_headers.update(headers)

        # Sort headers
        signed_headers_list = sorted([k.lower() for k in req_headers.keys()])
        signed_headers = ';'.join(signed_headers_list)

        canonical_headers = ''
        for h in signed_headers_list:
            canonical_headers += f"{h}:{req_headers[h].strip()}\n"

        canonical_request = f"{method}\n{canonical_uri}\n{canonical_querystring}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"

        algorithm = 'AWS4-HMAC-SHA256'
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = f"{algorithm}\n{amz_date}\n{credential_scope}\n{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"

        signing_key = get_signature_key(self.secret_key, date_stamp, self.region, 's3')
        signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

        authorization_header = f"{algorithm} Credential={self.access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"
        req_headers['Authorization'] = authorization_header

        url = f"{self.endpoint}{canonical_uri}"
        req = urllib.request.Request(url, data=data if data or method in ["PUT", "POST"] else None, headers=req_headers, method=method)

        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                status = resp.status
                resp_body = resp.read()
                lat_ms = (time.perf_counter() - t0) * 1000.0
                return status, lat_ms, resp_body
        except urllib.error.HTTPError as e:
            lat_ms = (time.perf_counter() - t0) * 1000.0
            return e.code, lat_ms, e.read()
        except Exception as e:
            lat_ms = (time.perf_counter() - t0) * 1000.0
            return 500, lat_ms, str(e).encode('utf-8')

def worker_loop(client, bucket, worker_id, runtime, is_bully, pacing_interval, obj_payload, results_list):
    end_time = time.time() + runtime
    req_id = 0
    while time.time() < end_time:
        key = f"obj_w{worker_id}_{req_id % 100}"
        req_id += 1
        status, lat_ms, _ = client.request("PUT", bucket, key=key, data=obj_payload)
        results_list.append((status, lat_ms))
        if pacing_interval > 0:
            time.sleep(pacing_interval)

def run_live_scenario(scenario_name, tenants_config, runtime=10, endpoint="http://localhost:8000"):
    print(f"\n{'='*80}\n LIVE S3 BENCHMARK: {scenario_name} ({runtime}s Run)\n{'='*80}")
    
    # Initialize clients and ensure buckets exist
    tenant_clients = {}
    for t in tenants_config:
        client = S3Client(endpoint, t["access_key"], t["secret_key"])
        bucket = t["bucket"]
        # Create bucket if needed
        client.request("PUT", bucket)
        tenant_clients[t["name"]] = (client, t)

    futures = []
    tenant_results = {t["name"]: [] for t in tenants_config}

    start_t = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as executor:
        for t in tenants_config:
            client, cfg = tenant_clients[t["name"]]
            bucket = cfg["bucket"]
            workers = cfg.get("concurrency", 1)
            pacing = cfg.get("pacing_s", 0.0)
            is_bully = cfg.get("is_bully", False)
            payload_size = cfg.get("payload_bytes", 4096)
            payload = b"X" * payload_size
            
            for w in range(workers):
                f = executor.submit(
                    worker_loop, client, bucket, w, runtime, is_bully, pacing, payload, tenant_results[t["name"]]
                )
                futures.append(f)

        concurrent.futures.wait(futures)
    total_duration = time.time() - start_t

    # Format Summary Table
    rows = []
    for t in tenants_config:
        name = t["name"]
        res = tenant_results[name]
        total_reqs = len(res)
        if total_reqs == 0:
            rows.append([name, "0", "0.0%", "0.0", "0.0", "0.0", "0.0"])
            continue
        
        successes = [lat for status, lat in res if status in [200, 201]]
        throttled = [lat for status, lat in res if status in [503, 429, 500]]
        drop_rate = (len(throttled) / total_reqs) * 100.0
        tps = len(successes) / total_duration

        if successes:
            successes.sort()
            p50 = successes[int(len(successes) * 0.50)]
            p95 = successes[int(len(successes) * 0.95)]
            p99 = successes[int(len(successes) * 0.99)]
        else:
            p50, p95, p99 = 0.0, 0.0, 0.0

        rows.append([
            name,
            f"{len(successes):,}",
            f"{drop_rate:.1f}%",
            f"{tps:,.1f}",
            f"{p50:.1f}ms",
            f"{p95:.1f}ms",
            f"{p99:.1f}ms"
        ])

    header = ["Tenant Persona", "Accepted", "Throttle/Drop %", "Throughput (TPS)", "p50 Latency", "p95 Latency", "p99 Latency"]
    col_widths = [len(h) for h in header]
    for r in rows:
        for i, c in enumerate(r):
            col_widths[i] = max(col_widths[i], len(str(c)))

    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    print(sep)
    print("| " + " | ".join(f"{h:<{col_widths[i]}}" for i, h in enumerate(header)) + " |")
    print(sep)
    for r in rows:
        print("| " + " | ".join(f"{str(c):<{col_widths[i]}}" for i, c in enumerate(r)) + " |")
    print(sep)

if __name__ == "__main__":
    pass
