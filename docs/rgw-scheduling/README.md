# RGW Admission Control & Multi-Tenant QoS Scheduling

Investigation into RGW's request admission control: why the current schedulers
cannot isolate S3 tenants from one another, and what a per-tenant dmClock
scheduler would buy.

The work was done in Antigravity IDE over 19–20 Aug 2026 and recovered into this
repository afterwards — Antigravity's chat history has been lost twice, so
everything now lives here instead. See [Recovering the sessions](#recovering-the-sessions).

## The problem in one paragraph

RGW's Beast/ASIO frontend accepts every connection and only applies admission
control after request parsing, where it either admits the request or replies
`503 SlowDown`. Two schedulers exist. `SimpleThrottler` is a single global
counter capped at `rgw_max_concurrent_requests` — no notion of tenant, cost, or
priority. The dmClock `AsyncScheduler` is better, but
[`client_id`](../../src/rgw/rgw_dmclock.h) is a hardcoded four-value enum
(`admin`, `auth`, `data`, `metadata`), and every S3 data operation in
[`rgw_op.h`](../../src/rgw/rgw_op.h) unconditionally returns `client_id::data`.
So dmClock protects health probes from S3 traffic, but **all S3 tenants share a
single queue** — one aggressive bulk uploader starves every interactive tenant on
the gateway, no matter how many tenants or cores are involved.

## Documents

| Document | What's in it |
| :-- | :-- |
| [architecture.md](architecture.md) | The fine-grained per-tenant dmClock design: queue topology, the single-strand-scheduler + multi-coroutine-worker concurrency model, request lifecycle, AIMD controller |
| [findings.md](findings.md) | Condensed empirical results across all three schedulers — the headline numbers |
| [benchmark-analysis.md](benchmark-analysis.md) | Full benchmark report, scenarios 1–8, per-tenant tables |
| [benchmark-family-analysis.md](benchmark-family-analysis.md) | Earlier analysis of workload families, scenarios 1–5 |
| [original-implementation-plan.md](original-implementation-plan.md) | The plan as written during the original session |
| [full-suite-results.md](full-suite-results.md) | **All 8 workload families, all 3 schedulers**, post-fix — what replicates and what doesn't |
| [evidence-status.md](evidence-status.md) | **Start here.** Every claim sorted by what it rests on: proven by code, proven by measurement, or void |
| [dmclock-reject-lockout.md](dmclock-reject-lockout.md) | **Biggest finding.** `AtLimit::Reject` — the mode production uses — locks clients out instead of throttling them, and tightening the limit makes it worse |
| [overload-results.md](overload-results.md) | First results from a run that actually stresses admission control — what holds, and the open problem blocking the rest |
| [throttle-slot-leak.md](throttle-slot-leak.md) | **Read first.** A harness bug jammed admission control in every run; it invalidates the throttler baseline and all absolute figures |
| [proposal.md](proposal.md) | **Start here for what to do next** — sequenced around the one experiment that decides how large the upstream patch has to be |
| [harness-fidelity.md](harness-fidelity.md) | Where the benchmark's *control* arm diverges from shipped RGW, what the full proposal actually is, and where a harness stops being able to help |
| [review-of-imported-findings.md](review-of-imported-findings.md) | **Read this alongside findings.md** — what upstreaming changed, and what it did not. The benchmark's conclusions stand. |
| [transcripts/](transcripts/) | Full recovered conversations, in chronological order |

## The three schedulers under test

| Mode | What it models |
| :-- | :-- |
| `throttler` | RGW's default `SimpleThrottler` — one global FIFO semaphore |
| `dmclock_coarse` | Upstream RGW's `AsyncScheduler` — four static daemon-level classes |
| `dmclock_fine` | One isolated mClock queue per tenant — hand-rolled, predates the composite `client_id` |
| `dmclock_fine_upstream` | The same, on the **production** `rgw::dmclock::AsyncScheduler`; prefer this arm |

Headline result, scenario 6 (bully tenant vs interactive tenant vs health probe):

| | `throttler` | `dmclock_coarse` | `dmclock_fine` |
| :-- | :-- | :-- | :-- |
| Health probe | 99.9% dropped | protected, 2.5 ms | protected, 2.5 ms |
| Interactive tenant | 5.4 req/s, 99.8% dropped | **0 req/s — fully starved** | **11.2 req/s, 88% accepted** |

Starvation under `dmclock_coarse` is unchanged at 8, 16 and 32 threads, which is
what makes it an architectural rather than a capacity problem.

## Running the benchmarks

The harness is [`src/test/rgw/bench_rgw_scheduler.cc`](../../src/test/rgw/bench_rgw_scheduler.cc),
built as `bin/bench_rgw_scheduler`. It simulates a RADOS backend, so it runs on a
workstation without a cluster.

```bash
ninja -C ~/development/build-ceph/release bin/bench_rgw_scheduler

# Validate a scenario without running it
bin/bench_rgw_scheduler --config benchmark_suite/scenarios/7_multi_tier_tenant_qos.json --dump_config

# Compare the three schedulers on the same workload
for sched in throttler dmclock_coarse dmclock_fine; do
  bin/bench_rgw_scheduler \
    --config benchmark_suite/scenarios/7_multi_tier_tenant_qos.json \
    --scheduler $sched --runtime 10 \
    --export_json benchmark_suite/results/7_multi_tier_$sched.json
done

# Closed-loop adaptive tuning vs static limits, under OSD-scrub latency spikes
bin/bench_rgw_scheduler --config benchmark_suite/scenarios/8_adaptive_capacity_tuning.json \
  --scheduler dmclock_fine --adaptive --runtime 12
```

Scenarios live in [`benchmark_suite/scenarios/`](../../benchmark_suite/scenarios/),
results in [`benchmark_suite/results/`](../../benchmark_suite/results/), and
[`benchmark_suite/run_suite.py`](../../benchmark_suite/run_suite.py) drives the
whole matrix.

## Recovering the sessions

Antigravity keeps each conversation in a SQLite database under
`~/.gemini/antigravity-ide/conversations/<uuid>.db`, with the messages in an
undocumented protobuf blob. When its chat-history UI loses a conversation, the
data is usually still on disk.

```bash
python3 docs/rgw-scheduling/tools/extract_antigravity_session.py --list
python3 docs/rgw-scheduling/tools/extract_antigravity_session.py --transcript <uuid> -o out.md
python3 docs/rgw-scheduling/tools/extract_antigravity_session.py --extract-writes <uuid> --restore ./recovered
```

`--extract-writes` recovers files the agent wrote; that is how
`benchmark_suite/scenarios/7_multi_tier_tenant_qos.json` and
`8_adaptive_capacity_tuning.json` were restored after being deleted. Verified
byte-identical against a scenario file that had survived on disk.

Sessions making up this investigation:

| Session | Date | Content |
| :-- | :-- | :-- |
| `6b6ffb64` | 19 Aug, morning | RGW source exploration, framing the admission-control problem |
| `ad0a5e06` | 19 Aug | Programmable benchmark harness, scenarios 1–5 |
| `a6ac6f37` | 20 Aug | `dmclock_fine`, scenarios 6–8, adaptive AIMD controller |

## Upstreaming status

Section 9 of [architecture.md](architecture.md) lists three steps to graduate
this into production RGW. The first is done:

- **Composite `client_id`** — done. `rgw::dmclock::client_id` is now
  `{tenant_id, op_class}` rather than a four-value enum, so each tenant gets its
  own mClock queue. Gated behind `rgw_dmclock_per_tenant_enabled` (default
  `false`); with the flag off, `tenant_id` is always 0 and queue partitioning is
  identical to before.
- **Per-tenant (R, W, L) profiles** — not started. Every tenant currently shares
  its op class's configured reservation, weight and limit, so the isolation comes
  from having separate queues, not from differing SLAs.
- **AIMD controller in the Beast frontend** — not started; exists only in the
  benchmark harness.

Upstreaming has not invalidated any benchmark result — the starvation finding was
reproduced here and stands. It has surfaced constraints the simulator could not
pose, chiefly that admission control runs *before* authentication, so the identity
available at scheduling time is the bucket rather than the S3 user. See
[review-of-imported-findings.md](review-of-imported-findings.md).
