# RGW small-object PUT bottleneck: bucket index contention — code-level analysis

**Author:** Adi Bakamovic (with Claude)
**Date:** 2026-08-12
**Code base:** ceph master checkout at `~/development/ceph`
**Motivating claim:** Clyso Enterprise Storage All-Flash Ceph Deployment Guide v1.1, 4KB object results
(178K PUT/s, 20 RGW daemons, 269 RGW cores vs 475 OSD cores): *"CPU usage and round-trip latency
for bucket metadata played a bigger role in aggregate performance than network throughput. Bucket
index contention between the RGW daemons may have played a significant role."*

---

## 1. Executive summary

Static analysis of the code confirms and refines the guide's hypothesis. Two distinct
bottlenecks compound at high small-object PUT rates:

1. **Bucket index contention.** Every PUT performs two cls operations against a bucket index
   shard object. All RGW daemons hash object names onto the same per-bucket set of
   `num_shards` shard objects, so daemon count adds client-side concurrency but zero
   server-side parallelism. Each shard object maps to one PG on one primary OSD, where index
   ops execute serially on a PG shard worker thread, performing 3 RocksDB point reads and 1-2
   omap writes per op inline, with a read-after-write stall (`o->flush()`) that serializes
   back-to-back ops on the same shard even though the object write lock is shared.

2. **RGW per-request CPU.** A 4KB SigV4 PUT hashes the payload up to 3 times (SHA256
   verification, MD5 etag, optional additional checksum), plus an HMAC-SHA256 chain per
   streaming chunk, on top of HTTP parsing, filter-chain setup, and per-request machinery.

The per-shard throughput ceiling is approximately `1 / (index op commit latency)`; RGW count
does not appear in that formula. The effective levers are: more index shards (pre-sharding),
indexless buckets where listing is not needed, minimizing multisite logging on hot buckets,
faster index-pool commits (dedicated NVMe, RocksDB tuning), and upstream code work
(redundant header read elision, stats batching/sharding, crypto acceleration).

One correction to the intuitive model: the OSD object lock (`RWWRITE`) is **shared** among
writers, not exclusive. The serialization comes from (a) single-threaded execution of ops for
a given PG on its shard queue, (b) the BlueStore `o->flush()` read-after-write dependency, and
(c) a convoy effect: a single blocked reader (e.g. a bucket listing) makes all subsequent
writers queue behind it (`object_state.h:84-87`).

---

## 2. Anatomy of a small-object PUT — RGW side

### 2.1 Synchronous RADOS round trips (fresh key, no preconditions)

| # | Operation | Sync? | Reference |
|---|-----------|-------|-----------|
| 1 | Bucket index **prepare** on shard object: `assert_exists` + `guard_bucket_resharding` + `bucket_prepare_op` | Yes — blocks before data write | `rgw_rados.cc:3515`, `10874-10880` |
| 2 | **Head object write** to data pool: one `ObjectWriteOperation` with atomic-modification guards, `mtime2`, `write_full` (4KB payload — fits head, no tail), and ~6-8 `setxattr` (etag, ACL, content-type, manifest, source zone, pg ver, id tag) | Yes | `rgw_rados.cc:3308-3524`, data at `3392-3395`, xattrs at `3414-3450` |
| 3 | Bucket index **complete**: guard + `bucket_complete_op` | No — `aio_operate`, fire-and-forget with retry manager | `rgw_rados.cc:8452`, `10918-10923` |
| 4 | Datalog entry (separate object) | Multisite only | `rgw_rados.cc:8453-8456` |

Notes:

- With no `If-Match`/`If-None-Match`, `assume_noent=true` **skips the head object stat**
  (`rgw_rados.cc:3677`, `7255-7257`); the tombstone cache covers recently deleted keys
  (`7263`). Overwrites of existing objects fail the first attempt with `-EEXIST` and retry
  with a full state read — overwrite-heavy workloads pay extra round trips (`3679-3687`).
- The head write carries data and all metadata in a single op — the data path itself is
  efficient; the index prepare is the extra serial hop on the response critical path.

### 2.2 Crypto passes over the payload

| Auth/config case | Full passes over payload | Reference |
|---|---|---|
| SigV4, `UNSIGNED-PAYLOAD` | 1 (MD5 etag) | `rgw_op.cc:4705`, `4991`, `5045` |
| SigV4, signed single payload | 2 (SHA256 verify + MD5) | `AWSv4ComplSingle`, `rgw_auth_s3.h:456-472` |
| SigV4, streaming signed (`aws-chunked`) | 2 (per-chunk SHA256 + MD5) **+ 1 HMAC-SHA256 signature chain per chunk** | `rgw_auth_s3.cc:1161-1175` (chunk sig), `1330` (chunk hash) |
| + `x-amz-checksum-*` requested | +1 (CRC/SHA pass in cksum filter) | `rgw_op.cc:4950-4964` |
| + `Content-MD5` header | MD5 result reused, no extra pass | `rgw_op.cc:4741-4754` |

The MD5 etag is computed unconditionally for regular PUTs (`need_calc_md5` is true unless
DLO/SLO manifest, `rgw_op.cc:4716`) — a mandatory serial CPU cost per request. The SigV4
header signature itself (HMAC over string-to-sign) is a small fixed cost
(`rgw_auth_s3.cc:997-1003`).

### 2.3 Buffer handling

The payload path is largely zero-copy after ingest: `recv_body` copies from the frontend
buffer into a fresh `bufferptr` (`rgw_rest.cc:1053-1061` — copy #1 after the socket read);
the putobj filter chain (`ChunkProcessor`/`StripeProcessor`) moves data via
`claim_append`/`splice` without copying (`rgw_putobj.cc:35-47`, `62-91`); librados appends
the refcounted bufferlist into the outgoing message. Streaming-signed uploads add a
chunk-framing parse buffer (`rgw_auth_s3.cc:1250-1267`), partially avoided by a bulk-read
fast path (`1319-1334`).

Filter-chain construction runs per request even when every filter is disabled
(encryption, compression, torrent, lua, cksum probes — `rgw_op.cc:4900-4964`).

---

## 3. The bucket index critical section — OSD side

### 3.1 What one index op costs

Both prepare and complete carry a `guard_bucket_resharding` cls call **which performs its own
header read** (`cls_rgw.cc:5258`), then the main handler reads the header again
(`cls_rgw.cc:967` / `1177`). Each `read_bucket_header` is a nested
`OMAPGETHEADER` → RocksDB `db->get()` executed inline (`objclass.cc:431-445`,
`PrimaryLogPG.cc:7989-8003`, `BlueStore.cc:14144-14178`). **The header is read and decoded
twice per op — one of the two reads is strictly redundant.**

Per-op RocksDB traffic (common case: non-versioned, no `remove_objs`, no reshard in progress):

| Op | RocksDB point reads | Omap writes | References |
|---|---|---|---|
| prepare | 3 (2× header + 1× entry) | 1 (entry + pending tag) | `cls_rgw.cc:934-1026` |
| complete (ADD) | 3 (2× header + 1× entry) | 2 (entry + **header rewrite**) | `cls_rgw.cc:1147-1383`, header write at `1373` |
| complete, multisite | 3 | 3 (+ bilog omap entry) | `log_index_operation`, `cls_rgw.cc:188-225`, gate at `1236` + `rgw_rados.cc:8450` |

**Per completed PUT: 6 RocksDB point reads (4 of them the same header key) and 3-4 omap
writes**, plus 2 pg log entries (also omap keys on the pgmeta object,
`PG.cc:938-943`), 2 `object_info` rewrites (`PrimaryLogPG.cc:9256-9273`), and dup-op
tracking. Omap writes have no deferred-write path — every one lands in the RocksDB WAL and
is rewritten by compaction (`BlueStore.cc:18590-18674`).

**The shard header is the hottest key**: every complete does a read-modify-write of it to
maintain per-category bucket stats (`cls_rgw.cc:1320-1329`) and bumps its version
(`757-764`). This RMW is what makes index ops on the same shard *semantically*
non-parallelizable — no locking change can fix it without changing the data model.

### 3.2 How serialization actually happens

The intuitive model "exclusive object lock" is wrong in an interesting way:

- Index write ops take the obc lock in `RWWRITE` mode, which is **shared among writers**
  (`object_state.h:94-96`) — multiple index writes can be in flight on one shard object.
- Actual execution is serial anyway: cls methods run synchronously on the PG's shard worker
  thread (`PrimaryLogPG.cc:6343-6401`, `ClassHandler.cc:315-338`), and a PG maps to exactly
  one OSD op-queue shard (default SSD: 8 shards × 2 threads, `osd.yaml.in:966-1027`). All
  RocksDB reads happen inline on that thread.
- **Read-after-write stall:** BlueStore's `omap_get_header`/`omap_get_values` call
  `o->flush()` (`BlueStore.cc:14164`, `14203`), which waits until the previous transaction
  touching that onode has been submitted to RocksDB (`14986-15016`). A `complete` op's first
  header read therefore blocks until the preceding `prepare`'s transaction is in the KV DB —
  back-to-back index ops on one shard serialize on KV submission.
- **Reader convoy:** the lock does block readers (`RWREAD`/`RWEXCL` — bucket listing,
  `bi_list`, stats), and once one reader is waiting, all *new writers* queue behind it
  (`object_state.h:84-87`). A single listing against a hot shard converts it into a hard
  serialization point.
- **Lock held across replication:** the lock moves from the op context into the `RepGather`
  (`PrimaryLogPG.h:889-903`) and is released only in `remove_repop` after local commit AND
  every replica's ack (`PrimaryLogPG.cc:11803`, `ReplicatedBackend.cc:638-640`, `706-761`).
  Blocked ops mark `waiting for rw locks` (`PrimaryLogPG.cc:2519-2524`) — visible in ops
  in flight dumps.

### 3.3 The scaling math

For a bucket with `S` shards and per-index-op effective latency `L` (RocksDB reads + WAL
write + replication ack), sustained PUT/s ≤ `S / (2·L)` in the worst case (prepare +
complete both land on the same shard; complete is async but still consumes shard capacity).
Adding RGW daemons increases offered load, not `S` or `L`. At 178K PUT/s across the guide's
test buckets, per-shard op rates reach the point where queueing (`waiting for rw locks`,
flush stalls, PG shard thread saturation) dominates round-trip latency — exactly what the
guide observed as "round-trip latency for bucket metadata."

Shard selection: `ceph_str_hash_linux(object_name) % num_shards`
(`svc_bi_rados.h:98-103`) — uniform across objects, but concurrency per shard grows linearly
with aggregate PUT rate.

---

## 4. What can be done

### 4.1 Operational levers (no code changes)

1. **Pre-shard buckets for peak concurrency, not object count.** Zone
   `bucket_index_max_shards` / `rgw_override_bucket_index_max_shards`. Sizing guide:
   `shards ≈ target PUT/s × per-op commit latency`, rounded up to a prime. Dynamic
   resharding is a safety net, not a strategy: it triggers at 100K objects/shard
   (`rgw_max_objs_per_shard`) and blocks writes in `block_while_resharding` while running
   (`rgw_rados.cc:8345`); during its logrecord phase every entry write is duplicated into a
   reshard log (`cls_rgw.cc:776-779`).
2. **Indexless (blind) buckets** for workloads that never list: both index ops vanish
   entirely (`rgw_rados.h:988`, `rgw_rados.cc:8383`). Incompatible with listing, multisite
   sync, lifecycle.
3. **Spread hot workloads across buckets** — same effect as sharding, at application level.
4. **Keep multisite logging off hot buckets.** Bilog adds an omap write inside the critical
   section plus `max_marker` header growth, gated by `need_to_log_data()`
   (`svc_zone.h:140`, `rgw_rados.cc:8450`); the datalog adds another RADOS op.
5. **Dedicated NVMe index pool.** The index pool is opened with `mostly_omap` hints
   (`svc_bi_rados.cc:49`); its performance is purely RocksDB. Fast WAL/DB devices shorten
   `L` directly. Watch compaction: index-heavy workloads are WAL+compaction bound.
6. **OSD op-queue shards** on index-pool OSDs (`osd_op_num_shards_ssd`,
   `osd_op_num_threads_per_shard_ssd`) — one slow `db->get` blocks 1 of only ~2 threads
   serving all PGs of that shard.
7. **Avoid listing hot buckets during ingest** (reader convoy, §3.2).
8. **RGW CPU:** prefer `UNSIGNED-PAYLOAD` over streaming-signed where the security model
   allows (saves a SHA256 pass + per-chunk HMAC chain); terminate TLS at the LB; disable ops
   log; fewer, larger daemons (the guide itself notes better efficiency with fewer).

### 4.2 Code optimization opportunities (upstream)

1. **Eliminate the redundant header read** (§3.1): let the guard result carry the decoded
   header to the main handler, or fold guarding into prepare/complete. Saves 2 of 6 RocksDB
   reads per PUT — mechanical, low-risk.
2. **Batch/shard the header stats RMW**: accumulate per-category stat deltas and fold them
   in periodically (or on read), instead of rewriting the header on every complete. This
   relaxes the *semantic* serialization — the highest-leverage structural change.
3. **Prepare elision / single-round-trip index update** for non-versioned, non-multisite
   buckets: the pending-map two-phase protocol exists for crash consistency with dir_suggest
   repair as backstop; a one-op variant would halve index ops per PUT.
4. **Crypto acceleration** (ISA-L / EVP multi-buffer MD5+SHA256) and fusing the MD5/SHA256
   passes into one traversal of the payload.
5. **Skip filter-chain probing** when no filters are configured (`rgw_op.cc:4900-4964`).

### 4.3 Measuring / validating on a live cluster

- `rgw_bucket_index_transaction_instrumentation = true` → per-transaction ENTERING/EXITING
  timestamps for every index op (`ldout_bitx`, `rgw_rados.cc:10870-10881`, `cls_rgw.cc`).
- `ceph daemon osd.N dump_ops_in_flight` on index-pool OSDs → look for
  `waiting for rw locks` and `waiting for ondisk` delays.
- PG stats cross-check: one prepare accounts 4 `num_rd`/1 `num_wr`, one complete 4/2-3
  (`PrimaryLogPG.cc:8000-8164`) — `ceph pg dump` rates on index PGs should match
  2× PUT rate × these factors.
- LTTng tracepoints already exist around prepare/operate/complete
  (`rgw_rados.cc:3514-3523`, `3563`).

---

## Appendix A: key code locations

| What | Where |
|---|---|
| PUT execute, MD5, filter chain | `src/rgw/rgw_op.cc:4697-5060` |
| Frontend data ingest | `src/rgw/rgw_rest.cc:1038-1071` |
| SigV4 completers (single/chunked) | `src/rgw/rgw_auth_s3.{h,cc}`: `AWSv4ComplSingle`, `AWSv4ComplMulti`, chunk sig at `cc:1161` |
| Putobj pipeline (zero-copy) | `src/rgw/rgw_putobj.cc`, `src/rgw/driver/rados/rgw_putobj_processor.cc` |
| Head write + index orchestration | `src/rgw/driver/rados/rgw_rados.cc:3299-3689` (`_do_write_meta`, `write_meta`) |
| UpdateIndex prepare/complete | `rgw_rados.cc:8380-8459`, cls wrappers `10867-10938` |
| Shard selection | `src/rgw/services/svc_bi_rados.h:98-118` |
| cls handlers: prepare/complete/guard | `src/cls/rgw/cls_rgw.cc:934`, `1147`, `5258`; header I/O `525`, `757` |
| cls→OSD omap plumbing | `src/osd/objclass.cc:402-589`, `PrimaryLogPG.cc:7989-8164` |
| Lock lifecycle (RWWRITE, repop) | `PrimaryLogPG.cc:1692-1736`, `2519`, `11744-11808`; `src/osd/object_state.h:81-149` |
| BlueStore omap + flush stall | `src/os/bluestore/BlueStore.cc:14144-14229`, `18590-18674`, flush `4992-5004` |
| Config knobs | `src/common/options/rgw.yaml.in` (shards, reshard), `osd.yaml.in:966-1027` (op shards) |

## Appendix B: guide excerpt (source claim)

4KB PUT: 178K PUT/s — 269 RGW cores, 475 OSD cores (RGW/OSD ratio 11/20).
4KB GET: 312K GET/s — 302 RGW cores, 102 OSD cores (ratio 3/1).
"In small object tests, CPU usage and round-trip latency for bucket metadata played a bigger
role in aggregate performance than network throughput. Bucket index contention between the
RGW daemons may have played a significant role. [...] Clyso believes additional tuning and
code optimization may improve RGW CPU usage and performance in the future."
