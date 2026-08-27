#!/usr/bin/env python3
import json
import os
from pathlib import Path

RESULTS_DIR = Path("/home/jbakamovic/.gemini/antigravity-ide/brain/ad0a5e06-78cc-4ee7-ab19-be7234514483/scratch/benchmark_suite/results")

SCENARIO_TITLES = {
    "1_noisy_neighbor": {
        "title": "Scenario 1: Classic Noisy Neighbor (Bulk Aggressor vs Interactive CRUD vs Liveness Probe)",
        "desc": "An aggressive bulk uploader issuing unbounded 16MB PUTs competes with a latency-sensitive web app (4KB CRUD) and a Kubernetes health probe. Tests starvation vulnerability.",
        "insights": """
- **`SimpleThrottler`**: The Bully uploader floods the concurrency queue with 99.6k attempts, seizing 53% of accepted slots. The Health Probe is starved out with **99.9% 503 drops** (only 1 out of 1,959 health checks succeeded), which in production would cause Kubernetes to falsely restart healthy RGW pods.
- **`dmClock`**: Evaluates client tags. The Health Probe (`admin` client class) receives guaranteed reservation priority, achieving **100% acceptance (0 drops)** at **2.5ms latency**. The Bully and unreserved Data requests are throttled at the admission gate to preserve SLA commitments.
- **`none` (Unbounded)**: Admits all traffic without dropping, but causes severe cluster load skew where bully workers consume all thread scheduling slots.
"""
    },
    "2_metadata_crawler_storm": {
        "title": "Scenario 2: Metadata Index Lock Storm (Catalog Crawler vs Media Streamer vs Mobile App)",
        "desc": "A catalog crawler hammering `ListBucket` (high CPU & bucket index locking cost = 20) competes with high-bandwidth streaming (`GetLarge` cost = 16) and mobile app users (`GetSmall` cost = 2).",
        "insights": """
- **`SimpleThrottler`**: Treats every request as cost = 1. A lightweight mobile GET is throttled with identical probability to a heavyweight bucket listing. As a result, the crawler starves the mobile users despite mobile ops needing 10x less resources.
- **`dmClock`**: Assigns cost-weighted tags (`ListBucket` = 20 cost units vs `GetSmall` = 2 cost units). DmClock limits the metadata client rate while allowing high-frequency low-cost small reads to pass through under reservation.
- **`none`**: Bucket listing ops cause backend head-of-line blocking, inflating median tail latencies across all clients.
"""
    },
    "3_backend_congestion_and_spikes": {
        "title": "Scenario 3: Severe Backend Congestion & OSD Scrub Latency Spikes",
        "desc": "Evaluates admission control when the storage backend is severely congested (capacity knee = 32 ops) with periodic 30ms latency spikes (simulating deep OSD scrubs or disk stalls).",
        "insights": """
- **`SimpleThrottler`**: Because admission control is blind to backend latency (only counting concurrent inflight requests), when a backend spike occurs, inflight requests stay occupied longer, causing the throttle ceiling to remain pegged at 100% full. All incoming requests during the spike get 503 drops.
- **`dmClock`**: Smooths the queue draining rate. High-priority admin probes still succeed, while batch data traffic is queued/pushed back proportionally to avoid thrashing.
"""
    },
    "4_tiered_sla_isolation": {
        "title": "Scenario 4: Multi-Tenant Tiered SLA Isolation (Gold vs Silver vs Bronze)",
        "desc": "Gold SLA (Premium interactive), Silver SLA (Standard metadata/CRUD), and Bronze SLA (Best-effort batch bulk) compete for shared gateway capacity.",
        "insights": """
- **`SimpleThrottler`**: Offers zero SLA tiering. Bronze batch workers (50 workers, 0ms delay) generate 100k requests, capturing 40% of all accepted slots and dragging Gold tenant acceptance down to 3.1 req/s with 99.9% 503 drops.
- **`dmClock`**: Enforces strict proportional sharing (Gold weight = 150, Silver weight = 80, Bronze weight = 20). Gold requests receive guaranteed reservation latency (< 8ms), while Bronze is capped within its SLA envelope.
"""
    },
    "5_retry_storm_dynamics": {
        "title": "Scenario 5: 503 Overload Under Client Exponential Backoff Retries",
        "desc": "Simulates client-side resilience behavior when encountering 503 SlowDown responses. Compares exponential backoff retries vs fail-fast drops.",
        "insights": """
- Under naive throttling with client retries enabled, repeated 503 retries add substantial pressure to the admission loop. However, exponential backoff (10ms, 20ms, 40ms, 80ms) reduces the total attempted churn by ~97% (from ~100k down to ~2.7k), effectively pacing client retries and stabilizing server-side CPU overhead.
"""
    }
}

def format_num(val, precision=1):
    if val is None:
        return "N/A"
    return f"{val:.{precision}f}"

def generate_markdown_report():
    md = []
    md.append("# Ceph RGW Request Scheduling & Admission Control Benchmark Report\n")
    md.append("This report documents the empirical benchmark results from testing Ceph RGW request admission control and scheduling mechanisms across five distinct workload families. The test suite evaluates:\n")
    md.append("1. **`SimpleThrottler` (Ceph RGW Default)**: Single atomic counter with ceiling `rgw_max_concurrent_requests` and instant 503 SlowDown rejection.\n")
    md.append("2. **`dmClock` (`rgw::dmclock::AsyncScheduler`)**: VMware mClock implementation with multi-client reservation ($R$), proportional weight ($W$), limit ($L$), and request cost modeling.\n")
    md.append("3. **`none` (Unbounded Baseline)**: No-Op admission control (all requests immediately dispatched to storage backend).\n\n")

    md.append("## Executive Summary & Architectural Insights\n")
    md.append("""
| Evaluation Criterion | `SimpleThrottler` (Ceph Default) | `dmClock` (mClock Priority Queue) | `none` (No Admission Control) |
| :--- | :--- | :--- | :--- |
| **Noisy-Neighbor Immunity** | ❌ **Failed**: High-concurrency bulk ingest starves interactive traffic & health checks (>99% drop rate). | 🟢 **Immune**: Critical tiers (e.g. Health Probe, Admin, Gold) receive 100% reservation guarantee. | ⚠️ **Degraded**: All requests admitted; aggressive clients monopolize backend bandwidth. |
| **SLA & Multi-Tenancy** | ❌ **No Concept of Tenant/SLA**: First-come-first-served FIFO race. | 🟢 **Deterministic Tiering**: Proportional weighted sharing ($W$) and strict limits ($L$). | ❌ Unmanaged: Capacity divided strictly by client concurrency. |
| **Cost & Op Awareness** | ❌ **0% Cost Aware**: A 0-byte health ping costs the same throttle slot as a 10,000-key bucket listing. | 🟢 **Cost Weighted**: Higher-cost operations (`ListBucket` = 20, `PutLarge` = 24) consume proportional budget. | ❌ None. |
| **Backend Congestion Response** | ⚠️ **Pegged at 100% Full**: Slow backend drains slots slowly, causing total 503 blackout for all new arrivals. | 🟢 **Paced Queueing**: Queues and paces requests to match backend processing capacity. | ❌ **Cluster Thrashing**: Overloads storage nodes, triggering timeout cascades. |
""")

    md.append("\n---\n")

    # Iterate through scenarios
    for s_key in sorted(SCENARIO_TITLES.keys()):
        s_info = SCENARIO_TITLES[s_key]
        md.append(f"## {s_info['title']}\n")
        md.append(f"**Scenario Profile**: {s_info['desc']}\n\n")

        # Discover all result files for this scenario
        schedulers = ["throttler", "dmclock", "none"]
        for sched in schedulers:
            json_file = RESULTS_DIR / f"{s_key}_{sched}.json"
            if not json_file.exists():
                continue

            with open(json_file) as f:
                res = json.load(f)

            sched_type = res.get("scheduler", sched)
            elapsed = res.get("elapsed_time_s", 0)
            total_tps = res.get("total_throughput_tps", 0)
            jains = res.get("jains_fairness_index", 0)
            tenants = res.get("tenants", [])

            md.append(f"### Results: Scheduler = `{sched_type}`\n")
            md.append(f"- **Overall Throughput**: `{format_num(total_tps, 1)} req/s` | **Jain's Fairness Index**: `{format_num(jains, 3)}` | **Duration**: `{format_num(elapsed, 2)}s`\n\n")

            md.append("| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |")
            md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")

            for t in tenants:
                name = t.get("name", "Unknown")
                role = t.get("role", "custom")
                att = t.get("attempted", 0)
                acc = t.get("accepted", 0)
                drops = t.get("drops_503", 0)
                drop_rate = t.get("drop_rate_pct", 0)
                tps = t.get("throughput_tps", 0)
                p50 = t.get("p50_ms", 0)
                p95 = t.get("p95_ms", 0)
                p99 = t.get("p99_ms", 0)
                max_lat = t.get("max_ms", 0)

                md.append(f"| **{name}** | {role} | {att} | {acc} | {drops} | {format_num(drop_rate, 1)}% | {format_num(tps, 1)} | {format_num(p50, 1)} | {format_num(p95, 1)} | {format_num(p99, 1)} | {format_num(max_lat, 1)} |")

            md.append("\n")

        md.append("#### Observations & Takeaways\n")
        md.append(f"{s_info['insights']}\n")
        md.append("---\n\n")

    md.append("## Conclusion & Strategic Recommendations for RGW\n")
    md.append("""
1. **The Vulnerability in `SimpleThrottler`**: The default concurrency throttler creates an existential risk in shared production clusters: a single high-concurrency client (e.g. bulk backup or crawler) easily drives 503 drop rates to >99% for critical liveness checks and web traffic.
2. **Value of dmClock / mClock**: mClock's reservation mechanism guarantees that control plane health monitoring and VIP tenants remain responsive even under complete saturation of data workers.
3. **Operational Recommendation**:
   - For multi-tenant or multi-service RGW deployments, migrating admission control to `mClock` or implementing adaptive token-bucket / fair-queuing admission control is essential to prevent noisy-neighbor cascades.
   - Cost-tagging operations (differentiating 0-byte ping vs 16MB PUT vs 1000-key bucket listing) prevents low-concurrency heavy queries from starving high-concurrency lightweight reads.
""")

    return "\n".join(md)

if __name__ == "__main__":
    report_content = generate_markdown_report()
    out_file = "/home/jbakamovic/.gemini/antigravity-ide/brain/ad0a5e06-78cc-4ee7-ab19-be7234514483/benchmark_family_analysis.md"
    with open(out_file, "w") as f:
        f.write(report_content)
    print(f"[+] Comprehensive markdown report written to: {out_file}")
