# Implementation Plan: Demonstrating Intra-Class Tenant Starvation in Ceph RGW dmClock

## Overview
Currently, Ceph RGW's dmClock integration hardcodes a static 4-class enum (`admin`, `auth`, `data`, `metadata`). As a result, all S3 users and buckets performing `GetObj` or `PutObj` share the exact same `client_id::data` queue. When an aggressive bulk tenant (Bully) floods the gateway, **dmClock protects `admin` health probes, but completely fails to protect interactive S3 users within the `data` class**.

We will refine the benchmark harness [`src/test/rgw/bench_rgw_scheduler.cc`](../../src/test/rgw/bench_rgw_scheduler.cc) to introduce and compare **three distinct admission control models**:
1. **`throttler` (Ceph RGW Default)**: Naive global FIFO semaphore.
2. **`dmclock_coarse` (Upstream Ceph RGW dmClock)**: 4-class dmClock where all S3 tenant data traffic is pooled into a single `client_id::data` channel.
3. **`dmclock_fine` (True Multi-Tenant dmClock)**: Per-tenant dmClock priority queue where each S3 tenant has its own isolated $(R, W, L)$ SLA channel.

---

## Proposed Changes

### 1. Benchmark Harness ([`src/test/rgw/bench_rgw_scheduler.cc`](../../src/test/rgw/bench_rgw_scheduler.cc))

#### [MODIFY] [`src/test/rgw/bench_rgw_scheduler.cc`](../../src/test/rgw/bench_rgw_scheduler.cc)
- **Add Per-Tenant SLA Profile to `TenantConfig`**:
  Allow each tenant in JSON or CLI to define its own `DmClockProfile` (`reservation`, `weight`, `limit`).
- **Implement `FineGrainedTenantAsyncScheduler`**:
  Use `crimson::dmclock::PullPriorityQueue<uint32_t, Request, false>` with dynamic client info resolver, giving each tenant its own independent mClock priority queue and throttle accounting.
- **Implement Unified `TenantScheduler` Dispatcher**:
  - `throttler`: Bounded global count.
  - `dmclock_coarse`: Routes all data tenants into `client_id::data` (demonstrating the upstream RGW bottleneck).
  - `dmclock_fine`: Routes each tenant into its dedicated per-tenant queue with individual $(R, W, L)$ guarantees.
  - `none`: Unbounded baseline.
- **Update JSON Serialization & CLI Parser**:
  Support `scheduler = "throttler" | "dmclock_coarse" | "dmclock_fine" | "none"` and per-tenant `dmclock` profile overrides.

---

### 2. Benchmark Scenario & Automation Suite

#### [NEW] `benchmark_suite/scenarios/6_intra_class_tenant_starvation.json`
Configure a targeted multi-tenant workload:
- **Tenant A (Bully Data Tenant)**: 60 workers, 16MB PUTs, 0ms pacing (Aggressor).
- **Tenant B (Interactive Data Tenant)**: 15 workers, 4KB GET/PUTs, 2ms pacing (Victim).
- **Tenant C (K8s Health Probe)**: 1 worker, /health ping, 250ms pacing (`admin` class).

#### [MODIFY] `benchmark_suite/run_suite.py`
Update runner script to execute Scenario 6 across all scheduler modes and record the comparison table.

---

## Verification Plan

### Automated Benchmark Execution
1. Compile the updated benchmark binary:
   ```bash
   ninja -C /home/jbakamovic/development/build-ceph/release bin/bench_rgw_scheduler
   ```
2. Run Scenario 6 across the matrix:
   ```bash
   /home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler \
     --config /home/jbakamovic/development/ceph/benchmark_suite/scenarios/6_intra_class_tenant_starvation.json \
     --scheduler throttler --runtime 5

   /home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler \
     --config /home/jbakamovic/development/ceph/benchmark_suite/scenarios/6_intra_class_tenant_starvation.json \
     --scheduler dmclock_coarse --runtime 5

   /home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler \
     --config /home/jbakamovic/development/ceph/benchmark_suite/scenarios/6_intra_class_tenant_starvation.json \
     --scheduler dmclock_fine --runtime 5
   ```
3. Verify that:
   - In `throttler`: **Both** Tenant B and Tenant C suffer >99% 503 drops.
   - In `dmclock_coarse`: Tenant C (Probe) is 100% protected, **but Tenant B (Interactive) is still starved with 100% drops by Tenant A**.
   - In `dmclock_fine`: **Both** Tenant B (Interactive) AND Tenant C (Probe) achieve 0% drops and low tail latency (<5ms).
