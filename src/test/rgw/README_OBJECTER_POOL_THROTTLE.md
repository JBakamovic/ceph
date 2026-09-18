# Objecter Per-Pool Throttling, Asynchronous Queueing & Priority Tiering (Class-of-Service)

## 1. Architectural Overview

Ceph's legacy client-side I/O engine (`Objecter`) enforced admission control through two monolithic global throttles:
- `op_throttle_ops` (capped by `objecter_inflight_ops`, default 1024)
- `op_throttle_bytes` (capped by `objecter_inflight_op_bytes`, default 100 MiB)

In multi-tenant or multi-pool environments—such as Ceph RGW with separate data, index (`default.rgw.buckets.index`), metadata (`default.rgw.meta`), and control pools—this global design exhibited two severe failure modes:
1. **Cross-Pool Head-of-Line Blocking**: Under degraded Placement Groups (PGs) or slow/blocked OSDs, operations destined for the degraded pool freeze in RADOS while holding global in-flight slots. Benign operations destined for 100% healthy pools are starved inside `Objecter::_take_op_budget()` and queue indefinitely.
2. **Control-Plane Starvation**: Non-data operations (bucket listings, metadata lookups, health probes) share the same admission tier as multi-megabyte object payload writes. Heavy write bursts or write stalls lock out bucket listings and metadata queries, precipitating catastrophic cluster-wide timeouts.

To address these vulnerabilities, this subsystem implements three synergistic architectural enhancements:

```
                  +-------------------------------------------------------------+
                  |                  Objecter Admission Engine                  |
                  +-------------------------------------------------------------+
                                                 |
                   +-----------------------------+-----------------------------+
                   |                                                           |
      [Priority Class-of-Service]                                   [Normal Data Ops]
      Pools matching: *meta*,*index*,*control*                      Standard data pools
                   |                                                           |
       (Can consume up to 100%                                      (Capped at Normal Ceiling:
        of global in-flight budget)                                  global_max * (1.0 - reserved))
                   |                                                           |
                   +-----------------------------+-----------------------------+
                                                 |
                               +---------------------------------+
                               |   Per-Pool Throttle Partition   |
                               |    (ops cap: ratio * max_ops)   |
                               +---------------------------------+
                                                 |
                               +---------------------------------+
                               | Option A Async Throttling Queue |
                               |  (Non-blocking Boost.Asio drain)|
                               +---------------------------------+
                                                 |
                                                 v
                                    Dispatched to OSD Messenger
```

---

## 2. Key Components & Implementation Details

### A. Per-Pool Throttle Partitioning (`PoolThrottle`)
- **Location**: `src/osdc/Objecter.h`, `src/osdc/Objecter.cc`
- Each active pool dynamically instantiates a dedicated `PoolThrottle` tracker containing separate `ops` and `bytes` token throttles.
- Pool throttles are tracked in `std::map<int64_t, std::shared_ptr<PoolThrottle>> pool_throttles` protected by `pool_throttle_lock`.
- Stale throttles for deleted pools are pruned automatically upon OSDMap updates via `prune_pool_throttles()`.

### B. Option A: Asynchronous Non-Blocking Bounded Queue
- **Location**: `src/osdc/Objecter.h`, `src/osdc/Objecter.cc`
- Eliminates synchronous kernel futex sleeps (`Throttle::get()`) which previously blocked RGW Beast coroutine worker threads.
- When pool or global limits are saturated, operations are enqueued non-blocking into `pt->throttled_ops` (bounded by `max_queue_ops = max_ops * queue_ratio`).
- If the bounded queue is full, `Objecter` rejects admission immediately with `-EAGAIN`, enabling fast upstream client backpressure (HTTP 503 SlowDown).
- **Monotonic Wire TID Invariant**: Enqueued operations maintain `op->tid = 0`. Wire TIDs are strictly assigned under session lock `s->lock` inside `_op_submit()` at the exact moment of wire dispatch, preventing out-of-order protocol aborts on OSDs.
- **Asynchronous Event-Loop Draining**: When completing operations invoke `put_op_budget_bytes()`, all waiting pools are scheduled for drainage via `boost::asio::post(service.get_executor(), ...)`.

### C. Priority Tiering / Class-of-Service (CoS)
- **Location**: `src/osdc/Objecter.h`, `src/osdc/Objecter.cc`
- **Pool Identification**: Configured patterns (`objecter_pool_priority_pools`) match against pool names (e.g. `*meta*`, `*index*,*control*`) and numeric pool IDs.
- **Headroom Ceiling**: Normal data operations are subject to a global admission ceiling:
  $$\text{normal\_ceiling\_ops} = \lfloor \text{global\_max\_ops} \times (1.0 - \text{reserved\_ratio}) \rfloor$$
- Priority operations bypass this ceiling, guaranteeing that the reserved buffer (default 20%) remains permanently available for control-plane and index traffic.
- **Draining Precedence**: On budget return, priority pools are scanned and drained strictly ahead of normal data pools.

---

## 3. Configuration Reference

| Option | Type | Default | Dynamic | Description |
| :--- | :--- | :--- | :--- | :--- |
| `objecter_pool_throttle_enable` | `bool` | `false` | Yes | Master switch enabling per-pool partitioning. |
| `objecter_pool_inflight_ops_ratio` | `float` | `0.50` | Yes | Maximum fraction of global in-flight ops allowed for any single normal pool (clamped `0.05`..`1.0`). |
| `objecter_pool_inflight_op_bytes_ratio` | `float` | `0.50` | Yes | Maximum fraction of global in-flight bytes allowed for any single normal pool. |
| `objecter_pool_throttle_async` | `bool` | `true` | Yes | Enables non-blocking asynchronous queueing instead of synchronous thread blocking. |
| `objecter_pool_throttle_queue_ratio` | `float` | `4.0` | Yes | Queue capacity multiplier relative to pool in-flight cap (`max_queue_ops = pool_cap * queue_ratio`). |
| `objecter_pool_throttle_max_queue_ops` | `int` | `0` | Yes | Absolute queue cap (overrides `queue_ratio` if > 0; `0` = use ratio). |
| `objecter_pool_priority_tiering` | `bool` | `false` | Yes | Enables Class-of-Service headroom reservation and priority drain ordering. |
| `objecter_pool_priority_pools` | `str` | `*meta*,*index*,*control*` | Yes | Comma-separated glob patterns or pool IDs designated as priority tier. |
| `objecter_pool_priority_reserved_ratio` | `float` | `0.20` | Yes | Fraction of global budget reserved exclusively for priority pools (clamped `0.0`..`0.50`). |
| `objecter_pool_priority_ops_ratio` | `float` | `0.80` | Yes | In-flight budget multiplier for priority pools. |

---

## 4. Empirical Benchmark Proofs

All benchmarks executed against a live Ceph cluster with dedicated OSDs and RGW on Beast frontend. Complete telemetry archived under `/home/ultron/development/49/`.

### Benchmark A: The Frozen-Write Lockout (Degenerate Outage Proof)
- **Parameters**: `objecter_inflight_ops = 16`, `pool_ratio = 1.0`, 50 degraded workers (frozen on OSD 2), 5 healthy workers, 4 continuous probe workers.

| Metric | Unprioritized Equal Tier | Priority Class-of-Service | Impact |
| :--- | :--- | :--- | :--- |
| **Bucket Listing Success Rate** | **0.0% (100% Outage)** | **100.0% (119/119 OK)** | **Complete Protection** |
| **Bucket Listing Timeouts** | **8 Timeouts** | **0 Timeouts** | **100% Eliminated** |
| **Bucket Listing Median Latency** | **TIMEOUT (>15s)** | **21.3 ms** | **Sub-25ms Response** |
| **Healthy Pool Throughput** | **0.07 ops/s** | **8.23 ops/s** | **117x Throughput Surge** |
| **Healthy Pool Completion** | **5.3% (Catastrophic)** | **100.0% (Perfect)** | **Full Recovery** |

---

### Benchmark B: Production Scale (128 In-Flight Ops)
- **Parameters**: `objecter_inflight_ops = 128`, `pool_ratio = 0.60` (76 ops cap), 120 degraded workers, 20 healthy workers @ 10 req/s, 8 continuous probes (`ListObjectsV2` + `HeadBucket`) @ 5 req/s. 30s client timeout.

| Metric | Unprioritized Equal Tier | Priority Class-of-Service | Impact |
| :--- | :--- | :--- | :--- |
| **Good Pool Completed (200 OK)** | 1869 (100.0%) | **2048 (100.0%)** | **+179 Completed Ops** |
| **Good Pool Sustained Throughput** | 40.19 ops/s | **61.75 ops/s** | **+53.6% Throughput Surge** |
| **Good Pool P95 Latency** | 496.8 ms | **293.7 ms** | **-40.9% Latency Reduction** |
| **Bucket List P50 Latency** | 128.6 ms | **107.1 ms** | **-16.7% Lower** |
| **Bucket List P95 Latency** | 374.4 ms | **206.7 ms** | **-44.8% Tail Latency Cut** |
| **Bucket List Max Latency** | 602.1 ms | **350.3 ms** | **-41.8% Tail Latency Cut** |
| **Bucket Head P95 Latency** | 30.0 ms | **23.4 ms** | **-22.0% Faster** |
| **Server Good PUT Max Latency** | 878.0 ms | **535.0 ms** | **-39.1% Lower Tail** |

---

### Benchmark C: High-Capacity Production Scale (256 In-Flight Ops)
- **Parameters**: `objecter_inflight_ops = 256`, `pool_ratio = 0.60` (153 ops cap), 200 degraded workers, 30 healthy workers @ 10 req/s, 10 continuous probes @ 5 req/s. 30s client timeout.

| Metric | Unprioritized Equal Tier | Priority Class-of-Service | Impact |
| :--- | :--- | :--- | :--- |
| **Good Pool Completed (200 OK)** | 1952 (100.0%) | **2052 (100.0%)** | **+100 Completed Ops** |
| **Good Pool Sustained Throughput** | 34.02 ops/s | **58.09 ops/s** | **+70.8% Throughput Surge** |
| **Good Pool P95 Latency** | 653.9 ms | **494.2 ms** | **-24.4% Latency Reduction** |
| **Good Pool Max Tail Latency** | 1473.2 ms | **764.6 ms** | **-48.1% Latency Halved** |
| **Bucket List Max Latency** | 652.6 ms | **495.5 ms** | **-24.1% Lower Tail** |
| **Bucket Head Max Latency** | 419.6 ms | **320.0 ms** | **-23.7% Lower Tail** |
| **Server Good PUT Max Latency** | 854.0 ms | **678.0 ms** | **-20.6% Lower Tail** |

---

## 5. Verification Suite

### Automated Unit Testing
```bash
ninja unittest_objecter_pool_throttle
ctest -R unittest_objecter_pool_throttle --output-on-failure
```
All 13 unit tests execute deterministically in under 60 ms:
- `BasicPoolIsolation`
- `BlockingOnSaturatedPoolDoesNotBlockOtherPool`
- `GlobalThrottleCap`
- `DynamicRatioReconfiguration`
- `PruneDeletedPoolThrottles`
- `DisabledThrottleBehavior`
- `AsyncQueueNonBlockingSubmission`
- `AsyncQueueDrainOnBudgetReturn`
- `AsyncQueueFullRejection`
- `AsyncQueueCancellationOnTimeout`
- `PriorityPoolIdentification`
- `PriorityPoolHeadroomIsolation`
- `PriorityPoolDrainOrdering`

### End-to-End Orchestrator
```bash
python3 src/test/rgw/test_objecter_pool_throttle_contention.py \
  --mode compare-priority \
  --objecter-inflight-ops 128 \
  --pool-ratio 0.60 \
  --priority-reserved-ratio 0.20 \
  --degraded-workers 120 \
  --good-workers 20 \
  --good-rate 10.0 \
  --probe-workers 4 \
  --probe-max-keys 100 \
  --probe-rate 5.0 \
  --duration 20.0 \
  --run-name priority_prod_scale_128ops
```
