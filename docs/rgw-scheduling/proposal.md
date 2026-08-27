# Proposal: What to Do Next

> **Superseded in part.** Steps 1 and 2 below are done. Step 3 is on hold: while
> building its capacity probe, the harness turned out to be leaking admission
> control slots, which invalidates the throttler baseline and every absolute
> figure in the project. See [throttle-slot-leak.md](throttle-slot-leak.md).
> Rebuilding the scenarios so they generate real overload now comes first —
> running a ratio sweep on top of scenarios that never overload anything would
> only produce another set of numbers to redo.

The spine of this proposal is one finding: **dmClock's (R, W, L) are absolute
rates against a capacity RGW does not know**, which is why the shipped defaults
are inert and why the scheduler can be disabled without anyone noticing. Every
other piece either enables capacity-relative tuning or rides on it.

The slot leak reinforces rather than displaces that: with the leak fixed, the
scenarios' own values are revealed to sit far *below* real capacity, throttling
the gateway to 1-2% of what it can serve — the mirror image of the shipped
defaults sitting far above it. Both are the same disease.

## New Step 0 — Rebuild the scenarios around measured capacity

Measured capacity is ~1894 ops/s at `cluster_capacity = 64`. The scenarios offer
244 ops/s. They were never overload tests; they only looked like it because
admission control had seized.

- closed-loop scenarios need worker counts above the throttle ceiling, or they
  cannot produce a 503 by construction (see the table in
  [throttle-slot-leak.md](throttle-slot-leak.md))
- the open-loop scenario needs arrival rates above capacity, not 244/s
- add an assertion to the harness: if a run records zero rejections in every arm,
  say so loudly rather than reporting a comparison of unstressed schedulers

Only once a scenario genuinely overloads the gateway does any of the rest mean
anything.

## Where things stand

| | Status |
| :-- | :-- |
| Composite `client_id` (`{tenant_id, op_class}`), per-tenant queues behind `rgw_dmclock_per_tenant_enabled` | done; 10/10 unit tests, `radosgw` links |
| Harness fine arm runs the production `AsyncScheduler` (`dmclock_fine_upstream`) | done; recovers 407 accepted vs the hand-rolled arm's 112 |
| Rejection semantics, cost model, fidelity manifest, production-defaults scenario | done |

## Step 1 — Make dynamic client info legal (`U1=true`)

`PullPriorityQueue` is instantiated with `U1=false`, so `ClientRec` caches a
`const ClientInfo*` and `client_info_f` is never consulted again. That is both a
**latent lifetime hazard** — per-tenant profiles need a container that grows, and
any reallocation dangles the cached pointer on the request path — and a **blocker
to runtime retuning**, which capacity-relative values require by construction.

Flipping the template parameter fixes both at once:

```cpp
using Queue = crimson::dmclock::PullPriorityQueue<client_id, Request,
                                                  IsDelayed, /*U1=*/true>;
```

**Verified by spike**: compiles, all 10 scheduler unit tests pass, and scenario 6
throughput is unchanged (45.3 vs 44.2 tps, n=3). That workload is backend-bound
at ~45 tps, so it cannot resolve a small per-tag CPU cost — the honest claim is
"no regression at realistic rates", not "free". A CPU-bound microbenchmark should
bound it before this lands.

Small, self-contained, and everything below depends on it.

## Step 2 — Open-loop load generation

The worker loop is closed-loop: it blocks while a request sits in the queue, and
on rejection sleeps a fixed 5 ms while skipping the tenant's configured pacing.
Offered load therefore depends on the scheduler being measured, which is why drop
rates are not comparable across arms
([review §3](review-of-imported-findings.md)).

Replace it with a per-tenant arrival process (Poisson at a configured rate) that
submits independently of completion, and give rejected requests an S3-style
backoff instead of the fixed sleep. Without this, Step 3's sweep cannot be read —
comparing capacities is exactly the case where a scheduler-dependent denominator
destroys the comparison.

Likely also resolves the unexplained low-load probe oddity noted in
[harness-fidelity.md](harness-fidelity.md).

## Step 3 — The pivotal experiment: capacity-relative tuning

Express the profiles as fractions of an estimated capacity rather than absolute
rates, derive the estimate from observed completion throughput, and sweep:

- backend `cluster_capacity` ∈ {16, 64, 256}
- tenant count ∈ {3, 6, 20}
- arms: `throttler`, `dmclock_coarse`, `dmclock_fine_upstream`
- n ≥ 3 per cell, with the fidelity manifest attached

**The question**: does one ratio set preserve the tier ordering and the
interactive tenant's throughput across every cell?

This is a genuine fork, and both outcomes are useful:

| Outcome | Consequence |
| :-- | :-- |
| Ratios hold | dmClock gets shippable defaults. The upstream patch is small: composite `client_id` + ratio config + flip the default. The AIMD controller becomes a refinement. |
| Ratios do not hold | Static defaults are impossible, and the closed-loop capacity estimator is **mandatory**, not optional. That is a much larger patch, and better to know now than after writing it. |

Everything after this depends on the answer, which is the argument for running it
before building anything else.

## Step 4 — Fidelity: call the real decision, not an equivalent one

The harness still assembles its own `schedule_request` call. Production's is in
[rgw_process.cc:50](../../src/rgw/rgw_process.cc#L50) and derives everything from
an `RGWOp` and a `req_state`. Building those is cheap —
[test_rgw_lua.cc:210](../../src/test/rgw/test_rgw_lua.cc#L210) constructs a
`req_state` in three lines with no driver, and `RGWOp::init()` only stores
pointers, so a `nullptr` driver is fine when only `dmclock_client()` and
`dmclock_cost()` are called.

Beyond fidelity, this is the only way to settle the identity question from
[review §1](review-of-imported-findings.md) without a cluster: with the real
`dmclock_tenant_client()` in the path, the harness can measure directly what
per-bucket keying costs when one tenant owns many buckets — the *N* buckets →
*N × R* amplification is currently an argument, not a measurement.

## Step 5 — Production changes, in dependency order

1. `ClientConfig` resolves per-tenant profiles (safe once Step 1 lands)
2. Capacity estimator feeding ratio-based `ClientInfo` (shape decided by Step 3)
3. Per-tenant profiles in `RGWUserInfo` or on the bucket — decided by Step 4
4. AIMD controller in the Beast frontend, if Step 3 says it is required
5. `rgw_scheduler_type = dmclock` as the default

## What I would not do yet

Port the AIMD controller. It is the most invasive piece — it touches the request
path — and Step 3 may show that static capacity-relative ratios are sufficient,
in which case a much smaller patch carries the whole result.

## Recommended first chunk

Steps 1 + 2 + 3 together. Steps 1 and 2 are prerequisites for Step 3, Step 3 is
the fork everything else hangs on, and none of the three touches production
behaviour except the one-line `U1` change that is already spiked and reverted.
