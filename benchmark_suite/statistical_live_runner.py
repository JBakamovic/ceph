#!/usr/bin/env python3
"""
Statistical Live S3 Benchmark Runner.
Executes multi-trial live S3 benchmarks with warmup periods, cooldown drainage,
confidence interval estimation (95% CI), variance tracking (CV%), and Welch's t-tests.
"""

import time
import math
import statistics
import concurrent.futures
from typing import List, Dict, Any, Tuple
import scipy.stats as stats

from live_s3_runner import S3Client, worker_loop

def calculate_stats(samples: List[float], confidence: float = 0.95) -> Dict[str, float]:
    """Calculates mean, std, SEM, 95% CI, and CV% for a list of sample values."""
    n = len(samples)
    if n == 0:
        return {"mean": 0.0, "std": 0.0, "sem": 0.0, "ci_lower": 0.0, "ci_upper": 0.0, "cv_pct": 0.0}
    if n == 1:
        return {"mean": samples[0], "std": 0.0, "sem": 0.0, "ci_lower": samples[0], "ci_upper": samples[0], "cv_pct": 0.0}
    
    mean = statistics.mean(samples)
    std = statistics.stdev(samples)
    sem = std / math.sqrt(n)
    t_crit = stats.t.ppf((1 + confidence) / 2.0, df=n - 1)
    margin = t_crit * sem
    cv_pct = (std / mean * 100.0) if mean > 0 else 0.0
    
    return {
        "mean": mean,
        "std": std,
        "sem": sem,
        "ci_lower": max(0.0, mean - margin),
        "ci_upper": mean + margin,
        "cv_pct": cv_pct
    }

def welch_ttest(samples_a: List[float], samples_b: List[float]) -> Tuple[float, float, str]:
    """
    Performs Welch's two-sample t-test for unequal variances.
    Returns: (t_stat, p_value, significance_symbol)
    """
    if len(samples_a) < 2 or len(samples_b) < 2:
        return 0.0, 1.0, "ns"
    
    if len(set(samples_a)) == 1 and len(set(samples_b)) == 1 and samples_a[0] == samples_b[0]:
        return 0.0, 1.0, "ns"

    t_stat, p_val = stats.ttest_ind(samples_a, samples_b, equal_var=False)
    
    if math.isnan(p_val):
        return 0.0, 1.0, "ns"
    
    if p_val < 0.001:
        sig = "***"
    elif p_val < 0.01:
        sig = "**"
    elif p_val < 0.05:
        sig = "*"
    else:
        sig = "ns"
        
    return t_stat, p_val, sig

def run_single_epoch(tenants_config: List[Dict[str, Any]], runtime: float = 4.0, endpoint: str = "http://localhost:8000") -> Dict[str, Dict[str, float]]:
    """Runs a single live workload epoch and returns per-tenant TPS and latency percentiles."""
    tenant_clients = {}
    for t in tenants_config:
        client = S3Client(endpoint, t["access_key"], t["secret_key"])
        bucket = t["bucket"]
        client.request("PUT", bucket)
        tenant_clients[t["name"]] = (client, t)

    futures = []
    tenant_results = {t["name"]: [] for t in tenants_config}
    total_workers = sum(t.get("concurrency", 1) for t in tenants_config)

    start_t = time.time()
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, total_workers)) as executor:
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
    actual_duration = time.time() - start_t

    epoch_summary = {}
    for t in tenants_config:
        name = t["name"]
        res = tenant_results[name]
        if not res:
            epoch_summary[name] = {"tps": 0.0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0, "drops": 0.0}
            continue
        
        successes = [lat for status, lat in res if status in [200, 201]]
        throttled = [lat for status, lat in res if status in [503, 429, 500]]
        drop_pct = (len(throttled) / len(res) * 100.0) if res else 0.0
        tps = len(successes) / actual_duration if actual_duration > 0 else 0.0
        
        if successes:
            successes.sort()
            n = len(successes)
            p50 = successes[int(n * 0.50)] * 1000.0
            p95 = successes[min(int(n * 0.95), n - 1)] * 1000.0
            p99 = successes[min(int(n * 0.99), n - 1)] * 1000.0
        else:
            p50, p95, p99 = 0.0, 0.0, 0.0

        epoch_summary[name] = {
            "tps": tps,
            "p50_ms": p50,
            "p95_ms": p95,
            "p99_ms": p99,
            "drops": drop_pct
        }
    return epoch_summary

def run_statistically_rigorous_benchmark(
    scenario_name: str,
    tenants_config: List[Dict[str, Any]],
    trials: int = 5,
    trial_duration_s: float = 5.0,
    warmup_duration_s: float = 2.0,
    cooldown_s: float = 1.0
) -> Dict[str, Dict[str, Any]]:
    """
    Executes a statistically rigorous multi-trial benchmark:
    1. Warmup period (discarded from statistics).
    2. N independent trials with cooldown periods between trials.
    3. Aggregates mean, standard error, 95% confidence intervals, and CV%.
    """
    print(f"\n{'='*95}")
    print(f" STATISTICAL RUNNER: {scenario_name}")
    print(f" Configuration: {trials} Trials x {trial_duration_s}s (Warmup: {warmup_duration_s}s, Cooldown: {cooldown_s}s)")
    print(f"{'='*95}")

    # 1. Warmup
    if warmup_duration_s > 0:
        print(f"[*] Warming up HTTP connection pools and BlueStore caches ({warmup_duration_s}s)...")
        run_single_epoch(tenants_config, runtime=warmup_duration_s)
        time.sleep(cooldown_s)

    # 2. Multi-Trial Collection
    trial_results = {t["name"]: {"tps": [], "p50_ms": [], "p95_ms": [], "p99_ms": [], "drops": []} for t in tenants_config}

    for trial_idx in range(1, trials + 1):
        print(f"[*] Executing Trial {trial_idx}/{trials} ({trial_duration_s}s)...", end="", flush=True)
        epoch_data = run_single_epoch(tenants_config, runtime=trial_duration_s)
        for t_name, metrics in epoch_data.items():
            for m_key, val in metrics.items():
                trial_results[t_name][m_key].append(val)
        print(" Done.")
        if trial_idx < trials and cooldown_s > 0:
            time.sleep(cooldown_s)

    # 3. Compute Statistical Summaries
    aggregated_stats = {}
    for t_name, metrics in trial_results.items():
        aggregated_stats[t_name] = {
            "tps": calculate_stats(metrics["tps"]),
            "p50_ms": calculate_stats(metrics["p50_ms"]),
            "p95_ms": calculate_stats(metrics["p95_ms"]),
            "p99_ms": calculate_stats(metrics["p99_ms"]),
            "drops": calculate_stats(metrics["drops"]),
            "raw_samples": metrics
        }
        
    return aggregated_stats
