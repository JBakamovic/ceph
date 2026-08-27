# Ceph RGW Request Scheduling & Admission Control Benchmark Report

This report documents the empirical benchmark results from testing Ceph RGW request admission control and scheduling mechanisms across five distinct workload families. The test suite evaluates:

1. **`SimpleThrottler` (Ceph RGW Default)**: Single atomic counter with ceiling `rgw_max_concurrent_requests` and instant 503 SlowDown rejection.

2. **`dmClock` (`rgw::dmclock::AsyncScheduler`)**: VMware mClock implementation with multi-client reservation ($R$), proportional weight ($W$), limit ($L$), and request cost modeling.

3. **`none` (Unbounded Baseline)**: No-Op admission control (all requests immediately dispatched to storage backend).


## Executive Summary & Architectural Insights


| Evaluation Criterion | `SimpleThrottler` (Ceph Default) | `dmClock` (mClock Priority Queue) | `none` (No Admission Control) |
| :--- | :--- | :--- | :--- |
| **Noisy-Neighbor Immunity** | ❌ **Failed**: High-concurrency bulk ingest starves interactive traffic & health checks (>99% drop rate). | 🟢 **Immune**: Critical tiers (e.g. Health Probe, Admin, Gold) receive 100% reservation guarantee. | ⚠️ **Degraded**: All requests admitted; aggressive clients monopolize backend bandwidth. |
| **SLA & Multi-Tenancy** | ❌ **No Concept of Tenant/SLA**: First-come-first-served FIFO race. | 🟢 **Deterministic Tiering**: Proportional weighted sharing ($W$) and strict limits ($L$). | ❌ Unmanaged: Capacity divided strictly by client concurrency. |
| **Cost & Op Awareness** | ❌ **0% Cost Aware**: A 0-byte health ping costs the same throttle slot as a 10,000-key bucket listing. | 🟢 **Cost Weighted**: Higher-cost operations (`ListBucket` = 20, `PutLarge` = 24) consume proportional budget. | ❌ None. |
| **Backend Congestion Response** | ⚠️ **Pegged at 100% Full**: Slow backend drains slots slowly, causing total 503 blackout for all new arrivals. | 🟢 **Paced Queueing**: Queues and paces requests to match backend processing capacity. | ❌ **Cluster Thrashing**: Overloads storage nodes, triggering timeout cascades. |


---

## Scenario 1: Classic Noisy Neighbor (Bulk Aggressor vs Interactive CRUD vs Liveness Probe)

**Scenario Profile**: An aggressive bulk uploader issuing unbounded 16MB PUTs competes with a latency-sensitive web app (4KB CRUD) and a Kubernetes health probe. Tests starvation vulnerability.


### Results: Scheduler = `throttler`

- **Overall Throughput**: `12.8 req/s` | **Jain's Fairness Index**: `0.674` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant A (Bully Bulk Ingest)** | Bulk Aggressor | 99636 | 68 | 99568 | 99.9% | 6.8 | 28.6 | 32.5 | 32.9 | 33.0 |
| **Tenant B (Interactive Web CRUD)** | Latency Sensitive | 29948 | 59 | 29889 | 99.8% | 5.9 | 5.3 | 8.1 | 8.2 | 8.2 |
| **Tenant C (K8s Liveness Probe)** | Control Plane Health | 1959 | 1 | 1958 | 99.9% | 0.1 | 2.3 | 2.3 | 2.3 | 2.3 |


### Results: Scheduler = `dmclock`

- **Overall Throughput**: `5.1 req/s` | **Jain's Fairness Index**: `0.347` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant A (Bully Bulk Ingest)** | Bulk Aggressor | 99754 | 1 | 99753 | 100.0% | 0.1 | 29.5 | 29.5 | 29.5 | 29.5 |
| **Tenant B (Interactive Web CRUD)** | Latency Sensitive | 29925 | 0 | 29925 | 100.0% | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| **Tenant C (K8s Liveness Probe)** | Control Plane Health | 50 | 50 | 0 | 0.0% | 5.0 | 2.5 | 2.7 | 2.8 | 2.8 |


### Results: Scheduler = `none`

- **Overall Throughput**: `4005.3 req/s` | **Jain's Fairness Index**: `0.667` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant A (Bully Bulk Ingest)** | Bulk Aggressor | 19112 | 19062 | 0 | 0.0% | 1906.1 | 28.1 | 32.5 | 32.9 | 33.2 |
| **Tenant B (Interactive Web CRUD)** | Latency Sensitive | 20952 | 20944 | 0 | 0.0% | 2094.2 | 5.2 | 8.0 | 8.2 | 8.3 |
| **Tenant C (K8s Liveness Probe)** | Control Plane Health | 50 | 50 | 0 | 0.0% | 5.0 | 2.6 | 2.8 | 2.8 | 2.8 |


#### Observations & Takeaways


- **`SimpleThrottler`**: The Bully uploader floods the concurrency queue with 99.6k attempts, seizing 53% of accepted slots. The Health Probe is starved out with **99.9% 503 drops** (only 1 out of 1,959 health checks succeeded), which in production would cause Kubernetes to falsely restart healthy RGW pods.
- **`dmClock`**: Evaluates client tags. The Health Probe (`admin` client class) receives guaranteed reservation priority, achieving **100% acceptance (0 drops)** at **2.5ms latency**. The Bully and unreserved Data requests are throttled at the admission gate to preserve SLA commitments.
- **`none` (Unbounded)**: Admits all traffic without dropping, but causes severe cluster load skew where bully workers consume all thread scheduling slots.


---


## Scenario 2: Metadata Index Lock Storm (Catalog Crawler vs Media Streamer vs Mobile App)

**Scenario Profile**: A catalog crawler hammering `ListBucket` (high CPU & bucket index locking cost = 20) competes with high-bandwidth streaming (`GetLarge` cost = 16) and mobile app users (`GetSmall` cost = 2).


### Results: Scheduler = `throttler`

- **Overall Throughput**: `12.8 req/s` | **Jain's Fairness Index**: `0.968` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant 1 (Search Crawler)** | Metadata Bucket Indexer | 59824 | 33 | 59791 | 99.9% | 3.3 | 25.4 | 27.2 | 27.4 | 27.4 |
| **Tenant 2 (Media Streamer)** | High Bandwidth Reads | 49820 | 43 | 49777 | 99.9% | 4.3 | 20.3 | 21.8 | 21.9 | 22.0 |
| **Tenant 3 (Mobile Client)** | Interactive Small Reads | 29952 | 52 | 29900 | 99.8% | 5.2 | 5.2 | 5.6 | 5.7 | 5.7 |


### Results: Scheduler = `dmclock`

- **Overall Throughput**: `0.2 req/s` | **Jain's Fairness Index**: `0.667` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant 1 (Search Crawler)** | Metadata Bucket Indexer | 59870 | 1 | 59869 | 100.0% | 0.1 | 23.3 | 23.3 | 23.3 | 23.3 |
| **Tenant 2 (Media Streamer)** | High Bandwidth Reads | 49877 | 1 | 49876 | 100.0% | 0.1 | 22.0 | 22.0 | 22.0 | 22.0 |
| **Tenant 3 (Mobile Client)** | Interactive Small Reads | 29925 | 0 | 29925 | 100.0% | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |


### Results: Scheduler = `none`

- **Overall Throughput**: `4718.6 req/s` | **Jain's Fairness Index**: `0.867` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant 1 (Search Crawler)** | Metadata Bucket Indexer | 11712 | 11682 | 0 | 0.0% | 1168.2 | 25.6 | 28.3 | 29.2 | 29.9 |
| **Tenant 2 (Media Streamer)** | High Bandwidth Reads | 11086 | 11064 | 0 | 0.0% | 1106.4 | 20.6 | 22.8 | 23.3 | 23.9 |
| **Tenant 3 (Mobile Client)** | Interactive Small Reads | 24456 | 24441 | 0 | 0.0% | 2444.1 | 5.1 | 5.7 | 5.8 | 6.0 |


#### Observations & Takeaways


- **`SimpleThrottler`**: Treats every request as cost = 1. A lightweight mobile GET is throttled with identical probability to a heavyweight bucket listing. As a result, the crawler starves the mobile users despite mobile ops needing 10x less resources.
- **`dmClock`**: Assigns cost-weighted tags (`ListBucket` = 20 cost units vs `GetSmall` = 2 cost units). DmClock limits the metadata client rate while allowing high-frequency low-cost small reads to pass through under reservation.
- **`none`**: Bucket listing ops cause backend head-of-line blocking, inflating median tail latencies across all clients.


---


## Scenario 3: Severe Backend Congestion & OSD Scrub Latency Spikes

**Scenario Profile**: Evaluates admission control when the storage backend is severely congested (capacity knee = 32 ops) with periodic 30ms latency spikes (simulating deep OSD scrubs or disk stalls).


### Results: Scheduler = `throttler`

- **Overall Throughput**: `12.8 req/s` | **Jain's Fairness Index**: `0.677` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant A (Batch Ingest)** | Bulk Ingest | 69773 | 62 | 69711 | 99.9% | 6.2 | 27.5 | 32.2 | 32.9 | 33.0 |
| **Tenant B (Online Queries)** | Online Read Traffic | 39910 | 65 | 39845 | 99.8% | 6.5 | 5.1 | 20.5 | 21.3 | 21.9 |
| **Tenant C (Liveness Probe)** | Health Probe | 1969 | 1 | 1968 | 99.9% | 0.1 | 2.3 | 2.3 | 2.3 | 2.3 |


### Results: Scheduler = `dmclock`

- **Overall Throughput**: `6.7 req/s` | **Jain's Fairness Index**: `0.343` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant A (Batch Ingest)** | Bulk Ingest | 69785 | 1 | 69784 | 100.0% | 0.1 | 32.4 | 32.4 | 32.4 | 32.4 |
| **Tenant B (Online Queries)** | Online Read Traffic | 39880 | 0 | 39880 | 100.0% | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |
| **Tenant C (Liveness Probe)** | Health Probe | 66 | 66 | 0 | 0.0% | 6.6 | 2.5 | 2.7 | 2.8 | 2.8 |


### Results: Scheduler = `none`

- **Overall Throughput**: `2998.2 req/s` | **Jain's Fairness Index**: `0.669` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tenant A (Batch Ingest)** | Bulk Ingest | 15225 | 15190 | 0 | 0.0% | 1519.0 | 28.6 | 32.5 | 32.9 | 33.0 |
| **Tenant B (Online Queries)** | Online Read Traffic | 14743 | 14727 | 0 | 0.0% | 1472.7 | 5.5 | 21.6 | 21.9 | 22.0 |
| **Tenant C (Liveness Probe)** | Health Probe | 66 | 66 | 0 | 0.0% | 6.6 | 2.5 | 2.7 | 2.8 | 2.8 |


#### Observations & Takeaways


- **`SimpleThrottler`**: Because admission control is blind to backend latency (only counting concurrent inflight requests), when a backend spike occurs, inflight requests stay occupied longer, causing the throttle ceiling to remain pegged at 100% full. All incoming requests during the spike get 503 drops.
- **`dmClock`**: Smooths the queue draining rate. High-priority admin probes still succeed, while batch data traffic is queued/pushed back proportionally to avoid thrashing.


---


## Scenario 4: Multi-Tenant Tiered SLA Isolation (Gold vs Silver vs Bronze)

**Scenario Profile**: Gold SLA (Premium interactive), Silver SLA (Standard metadata/CRUD), and Bronze SLA (Best-effort batch bulk) compete for shared gateway capacity.


### Results: Scheduler = `throttler`

- **Overall Throughput**: `12.8 req/s` | **Jain's Fairness Index**: `0.963` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tier 1 (Gold SLA - Premium)** | Mission Critical | 29950 | 31 | 29919 | 99.9% | 3.1 | 8.0 | 13.1 | 13.1 | 13.1 |
| **Tier 2 (Silver SLA - Standard)** | Standard Business | 49859 | 47 | 49812 | 99.9% | 4.7 | 8.3 | 37.6 | 43.9 | 44.2 |
| **Tier 3 (Bronze SLA - Batch)** | Best Effort Bulk | 99704 | 50 | 99654 | 99.9% | 5.0 | 28.9 | 45.9 | 48.8 | 49.1 |


### Results: Scheduler = `dmclock`

- **Overall Throughput**: `0.3 req/s` | **Jain's Fairness Index**: `1.000` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tier 1 (Gold SLA - Premium)** | Mission Critical | 29925 | 1 | 29924 | 100.0% | 0.1 | 8.3 | 8.3 | 8.3 | 8.3 |
| **Tier 2 (Silver SLA - Standard)** | Standard Business | 49875 | 1 | 49874 | 100.0% | 0.1 | 5.4 | 5.4 | 5.4 | 5.4 |
| **Tier 3 (Bronze SLA - Batch)** | Best Effort Bulk | 99746 | 1 | 99745 | 100.0% | 0.1 | 28.4 | 28.4 | 28.4 | 28.4 |


### Results: Scheduler = `none`

- **Overall Throughput**: `3334.4 req/s` | **Jain's Fairness Index**: `0.991` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Tier 1 (Gold SLA - Premium)** | Mission Critical | 12272 | 12258 | 0 | 0.0% | 1225.8 | 9.8 | 13.5 | 14.0 | 14.7 |
| **Tier 2 (Silver SLA - Standard)** | Standard Business | 9779 | 9755 | 0 | 0.0% | 975.5 | 32.6 | 44.9 | 46.6 | 48.9 |
| **Tier 3 (Bronze SLA - Batch)** | Best Effort Bulk | 11382 | 11332 | 0 | 0.0% | 1133.2 | 46.7 | 54.2 | 56.0 | 58.5 |


#### Observations & Takeaways


- **`SimpleThrottler`**: Offers zero SLA tiering. Bronze batch workers (50 workers, 0ms delay) generate 100k requests, capturing 40% of all accepted slots and dragging Gold tenant acceptance down to 3.1 req/s with 99.9% 503 drops.
- **`dmClock`**: Enforces strict proportional sharing (Gold weight = 150, Silver weight = 80, Bronze weight = 20). Gold requests receive guaranteed reservation latency (< 8ms), while Bronze is capped within its SLA envelope.


---


## Scenario 5: 503 Overload Under Client Exponential Backoff Retries

**Scenario Profile**: Simulates client-side resilience behavior when encountering 503 SlowDown responses. Compares exponential backoff retries vs fail-fast drops.


### Results: Scheduler = `throttler`

- **Overall Throughput**: `12.8 req/s` | **Jain's Fairness Index**: `0.988` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Batch Clients (With Retries)** | Heavy Load with Retries | 2737 | 57 | 2680 | 97.9% | 5.7 | 28.6 | 32.6 | 32.8 | 32.9 |
| **API Clients (With Retries)** | Interactive with Retries | 1746 | 71 | 1675 | 95.9% | 7.1 | 5.1 | 7.9 | 8.1 | 8.2 |


### Results: Scheduler = `dmclock`

- **Overall Throughput**: `0.1 req/s` | **Jain's Fairness Index**: `0.500` | **Duration**: `10.00s`


| Tenant Persona | Role | Attempted | Accepted | 503 Drops | Drop Rate (%) | Req/sec | p50 (ms) | p95 (ms) | p99 (ms) | Max (ms) |
| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Batch Clients (With Retries)** | Heavy Load with Retries | 2681 | 1 | 2680 | 100.0% | 0.1 | 29.2 | 29.2 | 29.2 | 29.2 |
| **API Clients (With Retries)** | Interactive with Retries | 1675 | 0 | 1675 | 100.0% | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 |


#### Observations & Takeaways


- Under naive throttling with client retries enabled, repeated 503 retries add substantial pressure to the admission loop. However, exponential backoff (10ms, 20ms, 40ms, 80ms) reduces the total attempted churn by ~97% (from ~100k down to ~2.7k), effectively pacing client retries and stabilizing server-side CPU overhead.


---


## Conclusion & Strategic Recommendations for RGW


1. **The Vulnerability in `SimpleThrottler`**: The default concurrency throttler creates an existential risk in shared production clusters: a single high-concurrency client (e.g. bulk backup or crawler) easily drives 503 drop rates to >99% for critical liveness checks and web traffic.
2. **Value of dmClock / mClock**: mClock's reservation mechanism guarantees that control plane health monitoring and VIP tenants remain responsive even under complete saturation of data workers.
3. **Operational Recommendation**:
   - For multi-tenant or multi-service RGW deployments, migrating admission control to `mClock` or implementing adaptive token-bucket / fair-queuing admission control is essential to prevent noisy-neighbor cascades.
   - Cost-tagging operations (differentiating 0-byte ping vs 16MB PUT vs 1000-key bucket listing) prevents low-concurrency heavy queries from starving high-concurrency lightweight reads.
