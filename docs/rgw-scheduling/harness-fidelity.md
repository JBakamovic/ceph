# How Close Is the Harness to Production RGW?

[`bench_rgw_scheduler.cc`](../../src/test/rgw/bench_rgw_scheduler.cc) is a
simulator built to evaluate proposed changes to RGW's admission control. It is
supposed to diverge from shipped Ceph — that divergence is the proposal.

What it must *not* diverge in is the control it measures the proposal against,
or in anything that would stop the result transferring to a real gateway. This
document is about those two things, not about differences in general.

## What is already production code

| Component | Harness uses |
| :-- | :-- |
| `throttler` arm | the real `rgw::dmclock::SimpleThrottler` |
| `dmclock_coarse` arm | the real `rgw::dmclock::AsyncScheduler` |
| Priority queue, tags, reject threshold | the real `crimson::dmclock::PullPriorityQueue` |
| `ClientInfo`, `ClientCounters`, `SchedulerCompleter` | real `rgw::dmclock` types |
| Concurrency ceiling | the real `rgw_max_concurrent_requests` on a real `CephContext` |

So two of the three schedulers under test were never simulated.

## The gap that mattered: `dmclock_fine` was not production code

`FineGrainedDmClockTenantScheduler` is a hand-rolled scheduler — its own strand,
its own concurrency accounting, its own timer, and `PullPriorityQueue<uint32_t,
...>` keyed by a bare tenant index.

It had to be. Upstream `client_id` was a four-value enum, so the production
`AsyncScheduler` could not express a per-tenant queue at all. Testing the
hypothesis required building something that could.

Once `client_id` became `{tenant_id, op_class}`, that stopped being true. The
`dmclock_fine_upstream` arm runs the same per-tenant workload through the
**production `AsyncScheduler`**, with tenants distinguished by `client_id`:

```cpp
scheduler->schedule_request(dmc::client_id{tenant_key(tenant_idx), op_class},
                            params, time, cost, yield);
```

The only harness-side code left is the `ClientInfo` lookup, and even that is
shaped the way production must be: a node-based `std::map` so the addresses stay
valid, because `PullPriorityQueue` is instantiated with `U1=false` and caches the
`ClientInfo*` rather than re-reading the info function.

### What changed when the real scheduler was substituted

Scenario 6, 10 s, three runs, identical each time:

| | `dmclock_coarse` | `dmclock_fine` (hand-rolled) | `dmclock_fine_upstream` (production) |
| :-- | :-: | :-: | :-: |
| Tenant B (interactive) accepted | **0 — starved** | 112 | **407** |
| Tenant C (health probe) attempted / accepted | 40 / 40 | **12 / 11** | **40 / 40** |
| Total throughput | 4.5 tps | 12.8 tps | **44.7 tps** |

Scenario 7 (six tiers), three runs, `dmclock_fine_upstream`: probe 40/40 every
run; VIP 438/436/451; Gold 400/403/421; Silver 5; Bronze 3; Free 9–10. The SLA
ordering holds and is far better served than the hand-rolled arm managed
(VIP 55, Gold 50).

Two conclusions:

1. **The health-probe anomaly at the scenario's configured load was a defect in
   the hand-rolled scheduler.** With the production scheduler the probe issues
   all 40 of its 250 ms-interval probes and completes every one, exactly as under
   `dmclock_coarse`. The question left open in
   [review-of-imported-findings.md §4](review-of-imported-findings.md) is
   answered for that configuration: it was the harness, not per-tenant dmClock.

2. **The hand-rolled scheduler understated the result by roughly 3.6×.** The
   interactive tenant recovers 407 accepted requests against the coarse
   scheduler's 0, not 112. The central hypothesis is not merely intact — it is
   better supported by production code than by the simulation built to test it.

### Two caveats, so this is not over-read

**A separate low-load oddity persists, on every arm.** With the bully reduced to
one worker, the probe degrades regardless of scheduler: `coarse` 16/15,
`dmclock_fine` 11/10, `dmclock_fine_upstream` 11/10 (three runs each). Load goes
*down* and the probe does *worse*, which is backwards. Whatever causes it sits in
the harness's worker loop or its use of the io_context, not in any scheduler, and
it is still unexplained. Probe `attempted` counts should not be cited at
configurations other than the ones actually characterised.

**Jain's fairness index is the wrong metric for a tiered system.** The production
arm scores 0.378 against the hand-rolled arm's 0.481 while serving the SLA
hierarchy far better. That is not a regression: Jain's index rewards equal shares,
and a six-tier QoS policy exists to produce *deliberately* unequal ones. Serving
Platinum 438 and Bronze 3 is the policy working. The index should be dropped from
the tiered scenarios, or replaced with something that scores conformance to the
configured ratios.

## Separating the proposal from the control

A benchmark like this compares a **proposed treatment** against a **control that
must represent production as it is today**. Fidelity is only a problem in two
places: when the control arm does not behave like shipped RGW, or when something
shared by every arm changes whether the result would transfer at all. A
divergence that *is* the proposal is not a defect — it is the point.

Sorting what was found by that test gives a different answer than simply listing
differences.

### Control-arm fidelity: two real problems

**`dmclock_fine` was not production code.** Covered above. Fixed by
`dmclock_fine_upstream`, and the fix mattered: the hand-rolled scheduler was
understating the benefit of the proposal by about 3.6×.

**The dmClock control arm queues where upstream rejects.** Production constructs
the scheduler with plain `dmc::AtLimit::Reject`
([rgw_asio_frontend.cc:484](../../src/rgw/rgw_asio_frontend.cc#L484)) — a request
that would exceed its limit is rejected by `add_request()` immediately. The
harness passed `AtLimitParam(RejectThreshold{1.0})`, and dmclock's own comment
says requests under that threshold "are enqueued and processed like
`AtLimit::Wait`", so it queues up to a full second of work first.

This is not part of anyone's proposal; it was an unremarked parameter choice. It
biases the comparison in a specific direction: `throttler` rejects instantly
while `dmclock_coarse` queues, so part of "dmClock beats the throttler" is really
"queueing beats rejecting, when scored on throughput". It is also the whole of
the drop-rate-versus-latency confound in
[review-of-imported-findings.md §3](review-of-imported-findings.md).

Now a knob: `at_limit` = `reject` (default, production) | `reject_threshold` |
`wait` | `allow`.

### Not defects — these *are* the proposal

Two things flagged in an earlier draft of this document as fidelity problems are
nothing of the sort, and calling them that was a mistake:

- **Differentiated op costs.** `RGWOp::dmclock_cost()` returns `1` and no
  subclass overrides it, so production dmClock does no cost-proportional
  budgeting. The harness weights ops 1 / 2 / 3 / 16 / 20 / 24. That is a
  perfectly reasonable improvement to evaluate, and it is applied to every arm
  equally so it biases no comparison.
- **Tuned (R, W, L).** The scenarios use reservations and limits far tighter than
  the shipped defaults. Again: a configuration proposal, applied to all arms.

The only thing wrong here is a label. `benchmark-analysis.md` lists "Cost
Weighted: higher-cost operations consume proportional budget" as a property of
`dmClock` alongside the throttler's shortcomings — presenting a proposed change
as existing behaviour. That belongs in the proposal column.

### What is shared by every arm, and why it still matters

`0_production_defaults.json` runs the scenario 6 workload with radosgw's real
defaults: `rgw_max_concurrent_requests` 1024, `rgw_thread_pool_size` 128, and the
shipped per-class profiles — `data` (500, 500, **0**), `admin` (100, 100, **0**).
`lim = 0` is not "zero limit", it is *unlimited*:
`limit_inv = (0.0 == limit) ? 0.0 : 1.0/limit`
([dmclock_server.h:118](../../src/dmclock/src/dmclock_server.h#L118)), so the
limit tag never advances, no client is ever at its limit, and `AtLimit::Reject`
never fires.

Accepted ops per tenant, three runs each:

| Scheduler | Tenant A (bully) | Tenant B (interactive) | Tenant C (probe) |
| :-- | :-: | :-: | :-: |
| `throttler` | 77, 76, 77 | 50, 51, 50 | **1, 1, 1** |
| `dmclock_coarse` | 74, 76, 78 | 53, 51, 49 | **1, 1, 1** |
| `dmclock_fine_upstream` | 72, 75, 73 | 55, 52, 54 | **1, 1, 1** |

The three are indistinguishable and the probe is starved under all of them. With
unlimited limits and reservations above what the backend can serve, dmClock has
nothing to enforce and degenerates to FIFO.

This is not evidence against the proposal, and it is not a harness defect —
every arm sees the same configuration, so nothing is biased. What it shows is
*why* dmClock can ship disabled without anyone noticing: at its shipped values it
does nothing.

### Why the defaults are inert, and what that implies

The reason is structural, not conservative tuning. dmClock's (R, W, L) are
**absolute rates in cost-units per second**, not shares. From the tag arithmetic
([dmclock_server.h:247](../../src/dmclock/src/dmclock_server.h#L247)):

```
tag = max(time, prev_tag + (1 / rate) * cost)
```

Tags are in the same units as wall-clock `time`. So with `data` reserved at 500/s
on a gateway whose backend delivers ~13/s, `prev + 0.002·cost` is always below
`time`, every client's reservation tag collapses to its arrival time, they all
tie, and the queue degenerates to FIFO. And `lim = 0` gives `limit_inv = 0`,
which makes `tag_calc` return `min_tag` for the limit — the client is never at
its limit, so `AtLimit::Reject` can never fire.

(The documentation for `rgw_scheduler_type` says dmClock is experimental and
requires `experimental_feature_enabled`. No such check exists in the code —
`get_scheduler_t()` reads the string and nothing else. The gating is
documentation-only.)

This is the interesting part for the upstream case. Making dmClock the default —
which is the obvious end state for a proposal whose whole point is a better
scheduler — is blocked not by policy but by the fact that **nobody can pick good
static numbers**. The correct reservation depends on cluster capacity, which RGW
does not know, and which moves with recovery, scrub, and hardware.

That reframes the closed-loop AIMD controller from scenario 8. It is not an
optional refinement to bolt on at the end; it is the mechanism that *discovers*
capacity at runtime, and therefore the thing that makes a shippable default
possible at all. The alternative — or complement — is to change the configuration
model so the values are expressed as shares of measured capacity rather than
absolute rates, which is a change to how RGW configures dmClock rather than just
a change of numbers.

So the full proposal is larger than "add a tenant id to `client_id`", and the
pieces are not independent:

| | Piece | Role |
| :-: | :-- | :-- |
| 1 | per-tenant keying (composite `client_id`) | the isolation mechanism — demonstrated |
| 2 | capacity-relative tuning, static or adaptive | what makes 1 do anything outside a tuned lab |
| 3 | `rgw_scheduler_type = dmclock` as default | the goal; unblocked by 2, not by policy |
| 4 | cost-aware `dmclock_cost()` | optional; improves fairness across op sizes |

The imported analysis demonstrates 1 convincingly and prototypes 2. Landing 1
alone changes nothing for any default deployment — not because the proposal is
wrong, but because 2 is what carries it.

### Keeping old results readable

Scenarios 1–8 now pin `at_limit: reject_threshold`, `at_limit_threshold_s: 1.0`
and `uniform_cost: false` explicitly, so their published numbers still reproduce
exactly (verified: scenario 6 re-runs to `[5, 0, 40]` and `[5, 112, 11]`, matching
`benchmark_suite/results/`) while the divergence from production is visible in the
scenario file rather than buried in a default.

Every exported result now carries a `fidelity` block naming which knobs match
production and whether the scheduler under test was production code, so a result
can be read correctly without tracing back to its scenario.

## What remains simulated, and whether it matters

| Divergence | Matters for | Fixable here |
| :-- | :-- | :-- |
| **Closed-loop load generation.** Workers block until their request resolves, so offered load depends on the scheduler under test. On rejection a worker sleeps a fixed 5 ms and skips its configured pacing entirely. | Any drop-rate comparison across schedulers — see [§3](review-of-imported-findings.md). Throughput and latency are unaffected. | Yes: an open-loop arrival process, or a realistic S3 client backoff on 503. |
| **Simulated RADOS backend.** A timer plus an invented congestion curve, `1 + 3.0·overload^1.5`. | Absolute latencies, and the AIMD controller's tuning, which is fitted against this curve rather than a real cluster. | Partly: the curve can be calibrated against a real OSD set, but it stays a model. |
| **No request pipeline.** No HTTP parsing, no authentication, no `RGWOp`, no `req_state`. | The identity question in [§1](review-of-imported-findings.md) — admission control running before auth is invisible to a harness that hands out tenant indices by construction. Also per-bucket vs per-user keying. | **No.** This needs a real gateway. |

## Getting closer without a cluster

Ordered by correlation gained per hour spent. The first three are done.

| | Step | Status |
| :-: | :-- | :-- |
| 1 | Run the **production scheduler** in the fine arm (`dmclock_fine_upstream`) | done |
| 2 | Align **rejection semantics** and **cost model** with production; pin the old scenarios | done |
| 3 | **Fidelity manifest** in every result, plus a production-defaults scenario | done |
| 4 | Call the **production admission decision** rather than reimplementing the call | next |
| 5 | **Open-loop load generation** | next |
| 6 | **Calibrate** the backend curve against one cheap real measurement | after |

### 4. Call the real decision, not an equivalent one

The harness still constructs its own `schedule_request` call. Production's lives
in [rgw_process.cc:50](../../src/rgw/rgw_process.cc#L50) and derives everything
from an `RGWOp` and a `req_state`:

```cpp
const auto client = op->dmclock_client();   // -> dmclock_tenant_client() for data ops
const auto cost   = op->dmclock_cost();
return scheduler->schedule_request(client, {}, ..., cost, s->yield);
```

Building the inputs is cheap and there is precedent —
[test_rgw_lua.cc:210](../../src/test/rgw/test_rgw_lua.cc#L210) constructs a
`req_state` in three lines with no driver:

```cpp
RGWProcessEnv pe; RGWEnv e;
req_state s(g_ceph_context, pe, &e, 0);
```

`RGWOp::init()` only stores pointers, so a `nullptr` driver is fine when the only
methods called are `dmclock_client()` and `dmclock_cost()`. Point `s->bucket_name`
and `s->bucket_tenant` at the synthetic tenant, instantiate the concrete S3 ops
(`RGWGetObj_ObjStore_S3`, `RGWPutObj_ObjStore_S3`, `RGWListBucket_ObjStore_S3`),
and the harness stops approximating the decision and starts making it.

That single change buys: the real op-to-class mapping, the real cost function,
and the real per-tenant keying including `dmclock_tenant_client()` reading
`s->bucket_name` — which means the harness would finally exercise the pre-auth
identity path from [review §1](review-of-imported-findings.md), and would show
what per-bucket keying costs when one tenant owns many buckets. It is the largest
remaining fidelity gain available without a cluster.

### 5. Open-loop load generation

Offered load must not depend on the scheduler being measured. Replace the
blocking worker loop with an arrival process (Poisson at a configured rate per
tenant) that submits independently of completion, and give rejected requests an
S3-client backoff instead of the current fixed 5 ms sleep that also skips pacing.
Then "drop rate" means what it says and is comparable across arms, and the
unexplained low-load probe oddity in §"Two caveats" very likely resolves with it.

### 6. Calibrate once, simulate forever

The congestion curve `1 + 3.0·overload^1.5` is invented. It does not need a
production cluster to improve — a single `vstart.sh` with a few OSDs on tmpfs,
swept over concurrency once, gives a measured latency-versus-inflight curve whose
coefficients can be baked into the scenario config. One afternoon, then the
simulation inherits the shape of real behaviour indefinitely.

Pair it with a short **validation protocol**: write down three or four
predictions the simulation makes (isolation ratio between two tiers, the
concurrency at which p99 knees, the drop rate at 2× capacity), and check them the
first time a real cluster is available. Correlation is a claim about the
simulator, and it needs its own evidence.

## Where the line falls

For **scheduler behaviour** — starvation, isolation, reservations, the effect of
the concurrency ceiling — the harness can be made faithful, and with
`dmclock_fine_upstream` all three arms now exercise production code. The one
remaining fidelity problem in this class is closed-loop load generation, which is
tractable.

For **request-pipeline behaviour** — where tenant identity comes from, what an
S3 client does with a 503, what per-bucket keying costs when a tenant owns
hundreds of buckets — no amount of harness work will help. Those need `vstart.sh`
with `rgw_scheduler_type=dmclock`, `rgw_dmclock_per_tenant_enabled=true`, and a
real S3 load generator such as warp or s3bench driving several accounts.

The harness answers "does this scheduler behave correctly under contention". It
cannot answer "does RGW behave correctly", and it was never asked to.
