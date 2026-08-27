# Antigravity session `6b6ffb64-fd62-400c-9ae7-0a90336d3dfb`

Extracted from `/home/jbakamovic/.gemini/antigravity-ide/conversations/6b6ffb64-fd62-400c-9ae7-0a90336d3dfb.db` by `docs/rgw-scheduling/tools/extract_antigravity_session.py`.


---

## User (step 0)

let's start by exploring RGW source code and implementation

- 🔧 List RGW source directory — `./src/rgw`
- 🔧 List RGW driver directory — `./src/rgw/driver`
- 🔧 List driver/rados directory — `./src/rgw/driver/rados`
- 🔧 List RGW services directory — `./src/rgw/services`
- 🔧 View RGW main entry file — `./src/rgw/rgw_main.cc`
- 🔧 View end of rgw_main.cc — `./src/rgw/rgw_main.cc`
- 🔧 View RGW request processing pipeline — `./src/rgw/rgw_process.cc`
- 🔧 View rgw_process.cc request flow — `./src/rgw/rgw_process.cc`
- 🔧 View RGW SAL definition — `./src/rgw/rgw_sal.h`

### Assistant (step 22)

The **RADOS Gateway (RGW)** is Ceph's object storage gateway service providing RESTful APIs compatible with Amazon S3 and OpenStack Swift on top of the underlying RADOS cluster.

---

## 1. High-Level Architecture & Layering

```
                     ┌─────────────────────────────────────────┐
                     │          Client Applications            │
                     │    (AWS CLI, boto3, Swift, REST)        │
                     └────────────────────┬────────────────────┘
                                          │ HTTP / HTTPS
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 1. Frontend Layer (Beast / Boost.Asio)                                                │
│    [rgw_asio_frontend.cc] [rgw_asio_client.cc]                                         │
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │ Parses HTTP / creates RGWRestfulIO
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 2. REST & Routing Layer                                                                │
│    [rgw_rest.cc] [rgw_rest_s3.cc] [rgw_rest_swift.cc] [rgw_rest_iam.cc] [rgw_rest_sts.cc] │
│    Maps HTTP URI & method -> RGWHandler -> RGWOp subclass                              │
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │ Dispatches request
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 3. Request Processing Engine & Op Lifecycle                                            │
│    [rgw_process.cc] [rgw_op.cc] [req_state]                                             │
│    Auth (SigV4, Keystone) -> Permissions (IAM, ACL, OPA) -> Rate Limit -> Execute     │
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │ Calls abstract storage interface
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 4. Store Abstraction Layer (SAL)                                                       │
│    [rgw_sal.h] (rgw::sal::Driver, User, Bucket, Object, Writer, etc.)                 │
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │ Implementation
                                          ▼
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 5. Backing Storage Drivers & Subsystem Services                                        │
│    - RADOS Driver: [rgw_sal_rados.cc], [rgw_rados.cc], [svc_bi_rados.cc], [rgw_sync.cc]│
│    - Optional Drivers: [dbstore], [posix], [daos], [motr]                              │
└─────────────────────────────────────────┬──────────────────────────────────────────────┘
                                          │ librados & cls plugins (cls/rgw)
                                          ▼
                     ┌─────────────────────────────────────────┐
                     │           Ceph OSD / RADOS              │
                     └─────────────────────────────────────────┘
```

---

## 2. Request Execution Flow & Lifecycle

When an incoming HTTP request hits RGW, it goes through the following lifecycle defined in [rgw_process.cc](../../../src/rgw/rgw_process.cc#L200-L325):

1. **Connection Handling**:
   - The Beast frontend ([rgw_asio_frontend.cc](../../../src/rgw/rgw_asio_frontend.cc)) accepts the connection asynchronously on Boost.Asio coroutines.
2. **Context Creation**:
   - A [`req_state`](../../../src/rgw/rgw_common.h) structure is initialized. It holds all metadata for the request lifetime (HTTP environment, connection, parsed parameters, trace span, auth identity, target bucket/object).
3. **Routing & Dispatch**:
   - The REST layer ([rgw_rest.cc](../../../src/rgw/rgw_rest.cc)) routes the request through registered [`RGWRESTMgr`](../../../src/rgw/rgw_rest.h) trees, selecting the matching [`RGWHandler`](../../../src/rgw/rgw_rest.h) (e.g. S3 handler in [rgw_rest_s3.cc](../../../src/rgw/rgw_rest_s3.cc)).
   - The handler constructs the appropriate [`RGWOp`](../../../src/rgw/rgw_op.h) subclass (e.g., `RGWGetObj_ObjStore_S3`, `RGWPutObj_ObjStore_S3`, `RGWCreateBucket_ObjStore_S3`).
4. **Op Pipeline Execution** ([`process_request`](../../../src/rgw/rgw_process.cc#L200-L315)):
   - `init_processing()`: Parses payload parameters and headers.
   - `read_permissions()`: Fetches user, bucket, and object policies and ACLs.
   - `verify_op_mask()`: Verifies if the operation is enabled on the target zone/user.
   - `verify_permission()`: Evaluates IAM policies ([rgw_iam_policy.cc](../../../src/rgw/rgw_iam_policy.cc)), S3 ACLs ([rgw_acl_s3.cc](../../../src/rgw/rgw_acl_s3.cc)), and OPA if configured.
   - `verify_params()`: Validates request payload constraints.
   - `pre_exec()`: Prepares buffers and quota checks.
   - **Rate Limiting & Lua Hooks**: Applies rate limits ([rgw_ratelimit.h](../../../src/rgw/rgw_ratelimit.h)) and executes post-auth Lua scripts ([rgw_lua_request.cc](../../../src/rgw/rgw_lua_request.cc)).
   - `execute()`: Invokes the underlying storage layer methods through SAL (`rgw::sal`).
   - `complete()` / `send_response()`: Sends HTTP status, response headers, and XML/JSON/data payload back to the client.

---

## 3. Key Subsystems & Source Organization

### 3.1 Store Abstraction Layer (SAL)
* **Headers/Interface**: [rgw_sal.h](../../../src/rgw/rgw_sal.h) & [rgw_sal_fwd.h](../../../src/rgw/rgw_sal_fwd.h)
* **RADOS Implementation**: [src/rgw/driver/rados/](../../../src/rgw/driver/rados/)
  * [`rgw::sal::RadosStore`](../../../src/rgw/driver/rados/rgw_sal_rados.h): Concrete implementation of `rgw::sal::Driver`.
  * [`RGWRados`](../../../src/rgw/driver/rados/rgw_rados.h): Lower-level bridge to librados and RADOS object layouts.
* **Other Drivers**: [driver/dbstore](../../../src/rgw/driver/dbstore/), [driver/posix](../../../src/rgw/driver/posix/), [driver/daos](../../../src/rgw/driver/daos/), [driver/motr](../../../src/rgw/driver/motr/).

### 3.2 Object Layout & Manifests (RADOS Driver)
* **Object Head & Tails**: Objects larger than a configurable stripe size (default 4MB) are broken into:
  * **Head object**: Contains object metadata, first stripe (or inline data), and the manifest.
  * **Tail objects**: Stored across striped RADOS objects referenced by [`RGWObjManifest`](../../../src/rgw/driver/rados/rgw_obj_manifest.h).
* **PutObj Pipeline**: [rgw_putobj_processor.cc](../../../src/rgw/driver/rados/rgw_putobj_processor.cc) handles streaming writes, encryption, compression, and checksum computation on the fly.

### 3.3 Metadata, Bucket Index (BI) & Resharding
* **Bucket Index**: Maps object names within a bucket to their RADOS head objects, maintaining directory listings, versioning records, and bilog entries.
* **Services**: Managed via [`RGWSI_BucketIndex_RADOS`](../../../src/rgw/services/svc_bi_rados.h) using Ceph CLS plugins (`cls_rgw`).
* **Dynamic Resharding**: [rgw_reshard.cc](../../../src/rgw/driver/rados/rgw_reshard.cc) periodically monitors bucket shard fill rates and splits/reshards bucket indices to avoid single OSD contention.

### 3.4 Multi-Site Replication & Synchronization
* **Zone / Zonegroup / Realm**: Described in [rgw_zone.h](../../../src/rgw/rgw_zone.h) and [rgw_realm.cc](../../../src/rgw/rgw_realm.cc).
* **Metadata Sync (mdlog)**: Replicates user/bucket metadata changes via metadata logs ([svc_mdlog.h](../../../src/rgw/services/svc_mdlog.h), [rgw_trim_mdlog.cc](../../../src/rgw/driver/rados/rgw_trim_mdlog.cc)).
* **Data Sync (datalog & bilog)**: Tracks modified buckets via the cluster datalog ([rgw_datalog.cc](../../../src/rgw/driver/rados/rgw_datalog.cc)) and replicates object data using the bucket index log (bilog) in [rgw_data_sync.cc](../../../src/rgw/driver/rados/rgw_data_sync.cc).
* **Coroutines Architecture**: Asynchronous multi-site sync operations use a coroutine scheduler ([rgw_coroutine.h](../../../src/rgw/rgw_coroutine.h) & [rgw_cr_rados.h](../../../src/rgw/driver/rados/rgw_cr_rados.h)).

### 3.5 Garbage Collection (GC) & Object Lifecycle (LC)
* **Garbage Collection**: [rgw_gc.cc](../../../src/rgw/driver/rados/rgw_gc.cc) manages queued tail objects from overwritten or deleted multipart uploads.
* **Lifecycle Management**: [rgw_lc.cc](../../../src/rgw/rgw_lc.cc) scans buckets and evaluates S3 lifecycle rules (transitioning tiers or expiring objects).

### 3.6 Authentication & Authorization
* **Auth Framework**: [rgw_auth.h](../../../src/rgw/rgw_auth.h)
  * S3 v2 / v4 Signature authentication: [rgw_auth_s3.cc](../../../src/rgw/rgw_auth_s3.cc)
  * OpenStack Keystone tokens: [rgw_auth_keystone.cc](../../../src/rgw/rgw_auth_keystone.cc)
  * AWS STS / AssumeRole & WebIdentity: [rgw_sts.cc](../../../src/rgw/rgw_sts.cc), [rgw_rest_sts.cc](../../../src/rgw/rgw_rest_sts.cc)
* **Policy Engine**: AWS IAM policy parser and evaluator in [rgw_iam_policy.cc](../../../src/rgw/rgw_iam_policy.cc).

### 3.7 Additional Features
* **PubSub & CloudEvents**: Notifications sent to Kafka, AMQP, or HTTP endpoints on object mutations ([rgw_pubsub.cc](../../../src/rgw/rgw_pubsub.cc), [rgw_kafka.cc](../../../src/rgw/rgw_kafka.cc), [rgw_amqp.cc](../../../src/rgw/rgw_amqp.cc)).
* **Server-Side Encryption (SSE-S3, SSE-KMS, SSE-C)**: [rgw_crypt.cc](../../../src/rgw/rgw_crypt.cc), [rgw_kms.cc](../../../src/rgw/rgw_kms.cc), [rgw_kmip_client.cc](../../../src/rgw/rgw_kmip_client.cc).
* **Lua Scripting**: In-flight inspection and transformation of HTTP requests/responses and data payloads ([rgw_lua.cc](../../../src/rgw/rgw_lua.cc), [rgw_lua_request.cc](../../../src/rgw/rgw_lua_request.cc)).
* **S3 Select**: Pushdown SQL queries over CSV, JSON, and Parquet data ([rgw_s3select.cc](../../../src/rgw/rgw_s3select.cc)).
* **Object Lock & Retention**: WORM (Write Once, Read Many) compliance ([rgw_object_lock.cc](../../../src/rgw/rgw_object_lock.cc)).

---

## 4. Key File Reference Map

| Component | Key Source Files |
| :--- | :--- |
| **Daemon Entry Point** | [rgw_main.cc](../../../src/rgw/rgw_main.cc), [rgw_appmain.cc](../../../src/rgw/rgw_appmain.cc) |
| **HTTP Frontends** | [rgw_asio_frontend.cc](../../../src/rgw/rgw_asio_frontend.cc), [rgw_frontend.cc](../../../src/rgw/rgw_frontend.cc) |
| **Request Engine** | [rgw_process.cc](../../../src/rgw/rgw_process.cc), [rgw_common.h](../../../src/rgw/rgw_common.h), [rgw_op.h](../../../src/rgw/rgw_op.h) / [rgw_op.cc](../../../src/rgw/rgw_op.cc) |
| **REST APIs** | [rgw_rest.cc](../../../src/rgw/rgw_rest.cc), [rgw_rest_s3.cc](../../../src/rgw/rgw_rest_s3.cc), [rgw_rest_swift.cc](../../../src/rgw/rgw_rest_swift.cc), [rgw_rest_iam.cc](../../../src/rgw/rgw_rest_iam.cc) |
| **SAL (Store Abstraction Layer)** | [rgw_sal.h](../../../src/rgw/rgw_sal.h), [rgw_sal.cc](../../../src/rgw/rgw_sal.cc) |
| **RADOS Driver** | [rgw_sal_rados.h](../../../src/rgw/driver/rados/rgw_sal_rados.h), [rgw_sal_rados.cc](../../../src/rgw/driver/rados/rgw_sal_rados.cc), [rgw_rados.h](../../../src/rgw/driver/rados/rgw_rados.h) / [rgw_rados.cc](../../../src/rgw/driver/rados/rgw_rados.cc) |
| **Multi-Site Sync** | [rgw_sync.cc](../../../src/rgw/driver/rados/rgw_sync.cc), [rgw_data_sync.cc](../../../src/rgw/driver/rados/rgw_data_sync.cc), [rgw_coroutine.cc](../../../src/rgw/rgw_coroutine.cc) |
| **Admin CLI (`radosgw-admin`)** | [src/rgw/radosgw-admin/](../../../src/rgw/radosgw-admin/) |

---

Which area of RGW would you like to explore or work on in detail (e.g. S3 REST operation handling, the RADOS SAL driver, multi-site sync, resharding, authentication, or building/debugging a specific feature)?


---

## User (step 23)

i am concerned with workloads that become prohibitive - for example saturating the RGW such that the follow up requests aren't serviced or serviced very slow. i want to be smarter about that. perhaps some sort of adaptive workload scheduling or admission control - a policy and mechanism by which a server (web server, reverse proxy, load balancer, gateway, application front end) decides to accept, queue, delay, redirect, degrade, or reject an incoming request, connection, or session before fully committing server resources to it. There is no accept queue or connection cap in RGW admission control. RGW's Beast / ASIO frontend accepts every connection and creates a stackful strand (async execution unit). Admission control comes right after basic request parsing. It either accepts the request or relies immediately with a 503 SlowDown. Currently two schedulers exists:
(1) throttler (default) allows maximum 1024 requests. One global atomic counter, one ceiling, instant 503. No queueing, no fairness, no notion of who the request belongs to, no notion of cost. Config: rgw_max_concurrent_requests. Rationale: Give operators a memory pressure and usage heuristic.
(2) mClock (experimental) - [https://www.usenix.org/legacy/event/osdi10/tech/full_papers/Gulati.pdf](https://www.usenix.org/legacy/event/osdi10/tech/full_papers/Gulati.pdf) A algorithm devised by VMware to schedule VM IO based on configurable limits, weight, reservations.

- 🔧 Find scheduler configuration usage
- 🔧 View dmclock scheduler creation and types — `./src/rgw/rgw_dmclock.h`
- 🔧 Find scheduler usage in Beast frontend
- 🔧 View Scheduler interface and SimpleThrottler — `./src/rgw/rgw_dmclock_scheduler.h`
- 🔧 View SimpleThrottler implementation — `./src/rgw/rgw_dmclock_sync_scheduler.h`
- 🔧 View AsyncScheduler and SimpleThrottler in rgw_dmclock_async_scheduler.h — `./src/rgw/rgw_dmclock_async_scheduler.h`
- 🔧 View SimpleThrottler in rgw_dmclock_async_scheduler.h — `./src/rgw/rgw_dmclock_async_scheduler.h`
- 🔧 Find schedule_request callers
- 🔧 View scheduler call in process_request — `./src/rgw/rgw_process.cc`
- 🔧 View AsyncScheduler implementation — `./src/rgw/rgw_dmclock_async_scheduler.cc`
- 🔧 View dmclock async scheduling process loop — `./src/rgw/rgw_dmclock_async_scheduler.cc`
- 🔧 Find dmclock_client implementations in RGWOp
- 🔧 Find dmclock_cost in RGWOp
- 🔧 View token-bucket rate limiting in RGW — `./src/rgw/rgw_ratelimit.h`

### Assistant (step 52)

To address prohibitive workloads and saturation in Ceph RGW, let's analyze **why the current design breaks down**, **the failure modes under mixed/heavy workloads**, and **how we can architect adaptive admission control and intelligent scheduling for RGW**.

---

## 1. Anatomy of the Problem: Why Current RGW Saturates

### 1.1 Beast Coroutine Explosion & Memory Footprint
* **Accept Without Backpressure**: In [`rgw_asio_frontend.cc`](../../../src/rgw/rgw_asio_frontend.cc), Boost.Asio accepts incoming TCP connections in a loop and immediately spawns a stackful coroutine (strand) for each connection.
* **Resource Binding**: Even before admission control, each coroutine allocates stack space, buffers HTTP request headers, and constructs a [`req_state`](../../../src/rgw/rgw_common.h).
* **Connection Overload**: Under a sudden traffic spike (e.g., thousands of concurrent clients), memory pressure skyrockets before any 503 is returned, causing socket buffer exhaustion and context-switching overhead.

### 1.2 The Default `SimpleThrottler` Flaws
In [`rgw_dmclock_async_scheduler.h`](../../../src/rgw/rgw_dmclock_async_scheduler.h#L160-L215):
```cpp
if (outstanding_requests++ >= max_requests) {
  return -EAGAIN; // -> HTTP 503 SlowDown / RateLimited
}
```
* **Single Static Ceiling**: `rgw_max_concurrent_requests` (default 1024) is static. If RADOS OSDs slow down, 1024 concurrent requests will pile up, latencies will spike to tens of seconds, and client timeouts will trigger massive retry storms (thundering herd). Conversely, if the cluster is fast, 1024 might artificially throttle high-throughput workloads.
* **No Queueing / Binary Cliff**: Instant rejection with no short-term micro-buffering. A temporary micro-burst at request #1025 is dropped immediately even if request #1024 finishes 2 milliseconds later.
* **Zero Fairness & Noisy Neighbor**: A single bulk script (e.g., parallel multi-part upload or large `ListBucket`) can grab all 1024 slots, starving health checks, UI operations, and other tenants.
* **Cost Agnostic**: A 0-byte `HEAD` request consumes the exact same 1 slot as a 5GB streaming `GET`, a 10,000-key `ListBucket` (which holds bucket index locks), or an expensive `PutBucketPolicy`.

### 1.3 The Experimental `dmclock` Gaps
While mClock is a solid proportional-share / reservation algorithm ([rgw_dmclock_async_scheduler.cc](../../../src/rgw/rgw_dmclock_async_scheduler.cc)):
* **Coarse Bucketing**: Only 4 static categories exist: `admin`, `auth`, `data`, `metadata` ([rgw_dmclock.h](../../../src/rgw/rgw_dmclock.h#L23-L29)). It does not know about tenants, users, buckets, or source IPs.
* **Hardcoded Cost**: In [`rgw_op.h`](../../../src/rgw/rgw_op.h#L333), `virtual dmc::Cost dmclock_cost() { return 1; }`. It does not factor payload size or operation complexity into the scheduling cost.
* **Static Limits**: It does not dynamically adapt its capacity based on downstream cluster health, RADOS latency feedback, or gateway host load.

---

## 2. Adaptive Admission Control & Scheduling Design

To prevent saturation and guarantee quality-of-service (QoS), an intelligent admission control architecture operates across multiple stages:

```
 Incoming TCP Connection
            │
            ▼
┌────────────────────────────────────────────────────────┐
│ Stage 0: Connection / Socket Level Gate                │  -> TCP Accept backpressure & max active socket bounds
└───────────────────────────┬────────────────────────────┘
                            │ Read & parse HTTP Verb + URI + Headers
                            ▼
┌────────────────────────────────────────────────────────┐
│ Stage 1: Fast-Path / Early Admission Filter            │  -> Drop/Shed load before deep context/auth overhead
└───────────────────────────┬────────────────────────────┘
                            │ Resolve Op, Tenant / Identity, Cost
                            ▼
┌────────────────────────────────────────────────────────┐
│ Stage 2: Adaptive Workload Scheduler & Admission Ctrl │
│                                                        │
│  ┌──────────────────────────────────────────────────┐  │
│  │ A. Dynamic Concurrency Limiter (Little's Law)    │  │  -> Adapts max inflight based on RTT / queue delay
│  │    (Gradient / Vegas / CoDel feedback loop)      │  │
│  └────────────────────────┬─────────────────────────┘  │
│                           │                            │
│  ┌────────────────────────▼─────────────────────────┐  │
│  │ B. Cost Model & Multi-Tenant Fair Queuing        │  │  -> DRR / Virtual Clock per Tenant & Op class
│  │    (Cost estimation: size, metadata vs data)     │  │
│  └────────────────────────┬─────────────────────────┘  │
│                           │                            │
│  ┌────────────────────────▼─────────────────────────┐  │
│  │ C. Action Spectrum:                              │  │
│  │    - Dispatch Immediately                        │  │
│  │    - Short-Term Bounded Queue (Micro-delay)      │  │
│  │    - Degrade / Partial Response                  │  │
│  │    - Reject (503 SlowDown + Retry-After hint)    │  │
│  └──────────────────────────────────────────────────┘  │
└───────────────────────────┬────────────────────────────┘
                            │ Granted Execution Slot
                            ▼
┌────────────────────────────────────────────────────────┐
│ Stage 3: Execution in SAL / RADOS                      │
└───────────────────────────┬────────────────────────────┘
                            │ Measure Execution Latency & Feedback
                            └────────────────────────────► (Feeds back into Dynamic Limiter)
```

---

## 3. Core Architectural Mechanisms

### 3.1 Adaptive Concurrency Limits (Feedback-Driven Capacity)
Instead of a fixed integer `rgw_max_concurrent_requests = 1024`, implement an **Adaptive Concurrency Controller (ACC)** based on queuing theory (Little's Law: $L = \lambda \times W$) and congestion control algorithms (like TCP Vegas, Netflix Gradient2, or CoDel):

1. **Baseline Tracking**:
   - Track $RTT_{min}$ (minimum round-trip execution latency seen when the system is unloaded).
2. **Current Metric Tracking**:
   - Measure exponential moving average of recent latency $RTT_{sample}$ and queue delay.
3. **Gradient Adaptation**:
   $$\text{Gradient} = \frac{RTT_{min}}{RTT_{sample}}$$
   $$\text{NewLimit} = \text{CurrentLimit} \times \text{Gradient} + \beta$$
   - **If downstream RADOS slows down** ($RTT_{sample} \gg RTT_{min}$): The gradient $< 1.0$. The concurrency limit automatically shrinks, aggressively shedding excess load before queue buildup triggers OSD timeouts and memory bloat.
   - **If RADOS is healthy and underutilized**: The gradient $\approx 1.0$. The limit expands (up to a safe physical ceiling) to maximize throughput.

### 3.2 Operation Cost Modeling
Operations have vastly different resource footprints. The scheduler should assign a weighted `Cost`:

$$\text{OpCost} = \text{BaseWeight}(\text{OpType}) + \alpha \cdot \text{PayloadSize} + \beta \cdot \text{ExpectedIndexContention}$$

| Operation Category | Base Cost | Dynamic Factor | Rationale |
| :--- | :--- | :--- | :--- |
| **Lightweight Probe** (`HEAD`, `GetBucketLocation`, Health) | `1` | None | Low CPU, zero data transfer, prioritize to keep keepalives and load balancers healthy. |
| **Data Streaming** (`GetObj`, `PutObj` part) | `5` | `+ (ContentLength / 4MB)` | Network & OSD bandwidth heavy; throttles high-bandwidth hogs. |
| **Bucket Index Intensive** (`ListBucket`, `DeleteObjects`) | `20` | `+ (max_keys / 1000)` | Consumes bucket index shards, serializing locks in `cls_rgw`. |
| **Heavy Admin / Metadata** (`CreateBucket`, `PutBucketLifecycle`) | `15` | None | Modifies system metadata pools and zone configurations. |

### 3.3 Multi-Tenant Fair Queueing (Deficit Round Robin / WFQ)
To eliminate the **noisy-neighbor problem**:
* Group requests into queues by **Tenant / Account / User** (or Source IP / Subnet for anonymous traffic).
* Use **Deficit Round Robin (DRR)** or **Weighted Fair Queueing (WFQ)**:
  * Each tenant gets a quantum of cost tokens per round.
  * If a bursty tenant exhausts their quantum, their requests are queued or delayed, while other tenants' lightweight requests bypass the queue instantly.
* **Bounded Wait Queue**:
  * Requests are allowed to wait in the queue up to a short deadline (e.g. `rgw_scheduler_queue_timeout_ms = 250ms`).
  * If the deadline expires before execution, reject immediately with `503 SlowDown` and a `Retry-After: <seconds>` header.

### 3.4 Multi-Tier Action Spectrum
Rather than a binary "accept vs instant 503", the admission controller can apply graduated responses:

1. **Immediate Execution**: Available capacity and within tenant quota.
2. **Micro-Queue / Yield**: If momentarily at capacity, suspend the coroutine via Boost.Asio timer/channel for $\le 50\text{--}200\text{ms}$. If capacity frees up, resume without error.
3. **Load Shedding / Early 503**: If queues exceed threshold or latency gradient indicates severe overload, reject immediately at the admission boundary *before* reading the full payload body or executing auth queries against RADOS.
4. **Degrade / Selective Shedding**: Under emergency load, drop optional background hooks (e.g., usage batch logging, async tracing) or clamp `ListBucket` `max-keys` to smaller chunk sizes.

---

## 4. Implementation Touchpoints in the Ceph Codebase

Here is where these components map directly into Ceph RGW:

| Subsystem / Feature | Primary Code Files | Modification Strategy |
| :--- | :--- | :--- |
| **Scheduler Core Interface** | [rgw_dmclock_scheduler.h](../../../src/rgw/rgw_dmclock_scheduler.h) | Extend `Scheduler` interface to accept `req_state*` and return admission verdicts (`ALLOW`, `QUEUE`, `REJECT_SHED`, `REJECT_QUOTA`). |
| **Adaptive Concurrency Scheduler** | [rgw_dmclock_async_scheduler.h](../../../src/rgw/rgw_dmclock_async_scheduler.h), [rgw_dmclock_async_scheduler.cc](../../../src/rgw/rgw_dmclock_async_scheduler.cc) | Replace / supplement `SimpleThrottler` with an `AdaptiveLimiter` class calculating gradient/Vegas limits on `request_complete()`. |
| **Cost Calculation** | [rgw_op.h](../../../src/rgw/rgw_op.h), [rgw_op.cc](../../../src/rgw/rgw_op.cc) | Override `dmclock_cost()` across `RGWGetObj`, `RGWPutObj`, `RGWListBucket`, etc., computing cost dynamically from request headers/params. |
| **Admission Control Dispatch Point** | [rgw_process.cc:L378](../../../src/rgw/rgw_process.cc#L378) (`schedule_request`) | Update `process_request()` to pass client tenant/account ID and cost to the scheduler, and handle `Retry-After` headers on rejection. |
| **Beast Frontend Connection Bounds** | [rgw_asio_frontend.cc](../../../src/rgw/rgw_asio_frontend.cc) | Introduce active connection limit and socket accept rate-limiting to prevent unconstrained coroutine spawning. |

---

## 5. Potential Next Steps

We can take several approaches depending on your goals:

1. **Design & Implement an Adaptive Concurrency Limiter (Gradient / Vegas algorithm)** as a new `rgw_scheduler_type = adaptive` in RGW.
2. **Extend `RGWOp` Cost Modeling** so different operation types (e.g. `ListBucket`, large `PutObj`, small `HEAD`) have realistic cost metrics.
3. **Enhance Multi-Tenant Fair Scheduling** (per-tenant / per-user queues with Deficit Round Robin).
4. **Add Early Connection/Request Load Shedding** in the Beast ASIO frontend.

How would you like to proceed? We can dive into a specific component's design, inspect current benchmarks/configs, or start prototyping a solution.


---

## User (step 54)

how about constructing the workload first? so that we have something to work with ...


- 🔧 List test/rgw directory — `./src/test/rgw`
- 🔧 View dmclock unit test — `./src/test/rgw/test_rgw_dmclock_scheduler.cc`
- 🔧 List s3-tests directory — `./src/test/rgw/s3-tests`
- 🔧 View existing benchmark harness in test/rgw — `./src/test/rgw/bench_rgw_ratelimit.cc`

### Assistant (step 63)

To design and evaluate smarter admission control and workload scheduling, we need a **realistic, multi-tenant synthetic workload harness** that reproduces RGW saturation, noisy-neighbor starvation, and tail-latency explosions.

Here is the blueprint for constructing this workload.

---

## 1. Workload Profiles & Traffic Dimensions

To expose the shortcomings of the `SimpleThrottler` (global 1024 cap) and test adaptive schedulers, the workload must combine three conflicting tenant personas and diverse operation costs:

```
                                  RGW INGRESS
                                       │
            ┌──────────────────────────┼──────────────────────────┐
            ▼                          ▼                          ▼
   [Tenant 1: The "Bully"]    [Tenant 2: "Interactive"]   [Tenant 3: "Probe/Admin"]
   - Heavy bulk uploader      - Web/App end-users         - K8s health checks
   - 100+ concurrent workers  - 10 concurrent workers     - 1 periodic worker
   - 32MB PUTs & ListBucket   - 4KB GET / HEAD            - Ping / BucketLocation
   - High throughput demand   - Strict SLA (<20ms p99)    - Must NEVER fail/503
            │                          │                          │
            └──────────────────────────┼──────────────────────────┘
                                       │
                                       ▼
                       [ RGW Admission Controller ]
                                       │
                                       ▼
                  [ Backend Storage / RADOS Cluster ]
                  - Normal Capacity: ~150-200 concurrency
                  - Injected Saturation / Latency Spikes
```

### Profile Breakdown

| Tenant Persona | Concurrency | Operations & Sizes | Arrival Pattern | SLA / Expectation |
| :--- | :--- | :--- | :--- | :--- |
| **Tenant A: Bulk Aggressor ("Bully")** | High (50–200) | Large `PutObj` (16–64MB), `ListBucket` (10,000 keys) | Sustained flood / burst loops | High throughput, tolerant of controlled queuing. |
| **Tenant B: Latency Sensitive ("Victim")** | Medium (5–20) | Small `GetObj` (4KB–64KB), `PutObj` (4KB), `HEAD` | Poisson / interactive user traffic | Low latency ($p99 < 30\text{ms}$), zero drops. |
| **Tenant C: Health & Monitoring ("Probe")** | Low (1–2) | `HEAD /health`, `GetBucketLocation` | Periodic (every 1s) | **Zero tolerance for 503 SlowDown**. |

---

## 2. Realistic Backend Delay & Saturation Model

In a real Ceph cluster, downstream OSDs and network bandwidth have physical limits. When concurrent requests exceed cluster capacity $C_{cluster}$, downstream service time degrades non-linearly:

$$T_{service} = T_{base}(\text{OpSize}) + \text{QueueDelay}(\text{ActiveConcurrentReqs})$$

* **Light load** ($\text{Inflight} \le C$): $T_{service} \approx 5\text{ms}$ (small) / $40\text{ms}$ (large).
* **Saturated load** ($\text{Inflight} > C$): $T_{service}$ grows exponentially due to OSD disk I/O contention, lock serialization on bucket index PG shards, and network queueing.
* **Perturbation / Fault Injection**: Injected latency spikes (simulating OSD scrub or disk tail latency) to test if the adaptive scheduler automatically contracts its concurrency limit.

---

## 3. Two-Tier Implementation Plan for the Workload

We can construct the workload at two complementary levels:

### Tier 1: In-Tree Simulation Micro-Benchmark (`src/test/rgw/bench_rgw_scheduler.cc`)
* **Technology**: C++ using Boost.Asio coroutines (`boost::asio::spawn` / `yield_context`) matching Ceph's test infrastructure (similar to [`bench_rgw_ratelimit.cc`](../../../src/test/rgw/bench_rgw_ratelimit.cc) and [`test_rgw_dmclock_scheduler.cc`](../../../src/test/rgw/test_rgw_dmclock_scheduler.cc)).
* **Purpose**: Ultra-fast iteration. Directly plugs into [`rgw::dmclock::Scheduler`](../../../src/rgw/rgw_dmclock_scheduler.h) implementations (`SimpleThrottler`, `AsyncScheduler`, and our new adaptive controllers).
* **Capabilities**:
  * Runs millions of simulated multi-tenant requests in seconds without needing a running Ceph cluster.
  * Injects synthetic backend latencies, OSD saturation curves, and cost models.
  * Outputs detailed metrics:
    - Throughput per tenant (ops/sec, MB/sec).
    - Latency distribution ($p50$, $p90$, $p99$, max) per tenant.
    - Error rates (503s, drop count, queue timeouts).
    - **Jain's Fairness Index** ($J = \frac{(\sum x_i)^2}{n \sum x_i^2}$).

### Tier 2: Real S3 Async Load Generator (Python / `asyncio` / `aiobotocore` / `aiohttp`)
* **Technology**: Asynchronous Python S3 load generator.
* **Purpose**: End-to-end black-box benchmarking against a live RGW daemon (e.g. running via `vstart.sh` or local `radosgw`).
* **Capabilities**:
  * Authenticates as distinct S3 tenants (`tenantA`, `tenantB`, `admin`).
  * Emits mixed S3 requests (PUT, GET, HEAD, ListObjectsV2) with configurable concurrency and arrival rates.
  * Real-time metrics dashboard with response code histograms and TTFB (Time-To-First-Byte) tracking.

---

## 4. Key Metrics to Measure & Compare

When we run the workload against the existing `SimpleThrottler` vs an adaptive scheduler:

1. **Starvation & Noisy Neighbor**: Does Tenant B (interactive 4KB reads) get 503'd or experience high $p99$ latency when Tenant A floods RGW with 32MB writes?
2. **Cluster Goodput vs Latency**: As total concurrency climbs past cluster capacity:
   - Does `SimpleThrottler` let latencies climb into multi-second timeouts before hitting 1024?
   - Does an Adaptive Limiter keep queue delay stable by dynamically trimming inflight slots?
3. **503 Recovery & Thundering Herd**: When traffic bursts, does the system smooth out via micro-queuing, or does it trigger cascade retries?

---

## Next Step

Would you like to start by creating the **C++ in-tree simulation benchmark** (`src/test/rgw/bench_rgw_scheduler.cc`) to test scheduler algorithms directly, or the **Python async multi-tenant S3 client generator** (or both)?


---

## User (step 65)

ultimatelly we will need to prove this issue on an actual ceph cluster so tier 2 would be the goal but for now let's start with tier 1

- 🔧 View test/rgw CMake configuration — `./src/test/rgw/CMakeLists.txt`
- 🔧 Find bench_rgw_ratelimit target definition
- 🔧 View setup and main in bench_rgw_ratelimit.cc — `./src/test/rgw/bench_rgw_ratelimit.cc`
- 🔧 Check build directories — `ls -d ./build* 2>/dev/null || true`
- 🔧 Find available compilers and build tools — `which ninja cmake gcc g++ clang++ 2>/dev/null || true`
- 🔧 View benchmark targets in CMakeLists.txt — `./src/test/rgw/CMakeLists.txt`
- 🔧 Find test_rgw_dmclock_scheduler target
- 🔧 Find rgw_schedulers library target
- 🔧 View rgw_schedulers library in CMakeLists.txt — `./src/rgw/CMakeLists.txt`
- 🔧 Create bench_rgw_scheduler.cc — `./src/test/rgw/bench_rgw_scheduler.cc`
- 🔧 View bench_rgw_ratelimit in CMakeLists.txt — `./src/test/rgw/CMakeLists.txt`
- 🔧 Add bench_rgw_scheduler target to CMakeLists.txt — `./src/test/rgw/CMakeLists.txt`
- 🔧 Check git status — `git status`
- 🔧 Check development directory — `ls -la /home/jbakamovic/development/`
- 🔧 Check build-ceph directory — `ls -la /home/jbakamovic/development/build-ceph`
- 🔧 Check ninja build target for bench_rgw_scheduler — `ninja -C /home/jbakamovic/development/build-ceph/release -n bench_rgw_scheduler 2>&1 | head -n 20`
- 🔧 Build bench_rgw_scheduler target — `ninja -C /home/jbakamovic/development/build-ceph/release bin/bench_rgw_scheduler`
- 🔧 View FindRDKafka.cmake — `./cmake/modules/FindRDKafka.cmake`
- 🔧 View find_package RDKafka in CMakeLists.txt — `./src/rgw/CMakeLists.txt`
- 🔧 Find rdkafka_min_version definition
- 🔧 View rdkafka_min_version in CMakeLists.txt — `./src/rgw/CMakeLists.txt`
- 🔧 Fix rdkafka_min_version in CMakeLists.txt — `./src/rgw/CMakeLists.txt`
- 🔧 Build bench_rgw_scheduler — `ninja -C /home/jbakamovic/development/build-ceph/release bin/bench_rgw_scheduler`

### Assistant (step 112)

I have created the **Tier 1 workload benchmark harness** in [`src/test/rgw/bench_rgw_scheduler.cc`](../../../src/test/rgw/bench_rgw_scheduler.cc) and added the build target in [`src/test/rgw/CMakeLists.txt`](../../../src/test/rgw/CMakeLists.txt).

The compilation is currently running via `ninja`. Once finished, we can immediately execute baseline benchmark runs comparing the `SimpleThrottler` (default) vs `mClock` and simulated backend congestion under our multi-tenant workloads.

- 🔧 Check bench_rgw_scheduler options — `/home/jbakamovic/development/build-ceph/release/bin/bench_rgw_scheduler --help`
