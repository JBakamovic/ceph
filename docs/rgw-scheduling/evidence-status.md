# What Is Actually Established

Three harness defects were found in one session, each invalidating results that
had already been reported. That makes it reasonable to ask whether anything here
is proven. This page sorts every claim by what it rests on.

The short version: **the imported analysis diagnosed the right problem and
proposed the right fix; what broke was the baseline it compared against and the
absolute numbers.** Separately, the conclusions reached in this session rest
mostly on reading code and on arithmetic rather than on the benchmark — and where
the benchmark was the only evidence is where things collapsed.

## Proven by code inspection or arithmetic — no benchmark involved

These do not depend on the harness being correct, and none was affected by any of
the three defects.

| Claim | Evidence |
| :-- | :-- |
| `client_id` was a 4-value enum; every S3 data op returns `client_id::data`, so all tenants share one queue | [rgw_dmclock.h](../../src/rgw/rgw_dmclock.h), [rgw_op.h](../../src/rgw/rgw_op.h) |
| Admission control runs *before* authentication, so no authenticated identity exists at scheduling time | `schedule_request` at [rgw_process.cc:378](../../src/rgw/rgw_process.cc#L378) vs `verify_requester` at :392; `set_user()` installs an empty `rgw_user` |
| `PullPriorityQueue` is built with `U1=false`, so `ClientRec` caches a `ClientInfo*` and the info function is never re-read | [rgw_dmclock_async_scheduler.h:78](../../src/rgw/rgw_dmclock_async_scheduler.h#L78), [dmclock_server.h:865](../../src/dmclock/src/dmclock_server.h#L865) |
| Production uses `AtLimit::Reject`; the harness used `RejectThreshold{1.0}` | [rgw_asio_frontend.cc:484](../../src/rgw/rgw_asio_frontend.cc#L484) |
| `RGWOp::dmclock_cost()` returns 1 and is never overridden — production dmClock does no cost weighting | grep over `src/rgw/` |
| Shipped defaults have `lim = 0`, which means *unlimited*, so `AtLimit::Reject` can never fire | `limit_inv = (0.0 == limit) ? 0.0 : 1.0/limit`, [dmclock_server.h:118](../../src/dmclock/src/dmclock_server.h#L118) |
| (R, W, L) are absolute rates in wall-clock units, not shares | `tag = max(time, prev + cost/rate)`, [dmclock_server.h:247](../../src/dmclock/src/dmclock_server.h#L247) |
| `initial_tag()` advances `prev_tag` unconditionally, *before* the reject check | [dmclock_server.h:891](../../src/dmclock/src/dmclock_server.h#L891) |
| That behaviour is intended, not a bug | upstream test asserts it, with a comment: `test_dmclock_server.cc:1372` |
| **dmClock's `limit` cannot shape a client under sustained overload** | standalone 20-line simulation, independent of the harness: `prev` advances at `R/L` per unit time, so `R > L` runs away to zero admissions. `Reject`, `RejectThreshold` and a clamped variant all give 0.00; `Wait` never limits |
| The old closed-loop scenarios could never make the throttler reject anything | worker count < `max_concurrent_requests` in every scenario but #5 — arithmetic on the config files |
| All three harness defects | slot leak from `Completer`'s defaulted move-assignment; `thread_local` RNG under coroutine thread migration; `vm.count()` true for program_options defaults |

## Proven by measurement, and still standing

One comparison survives, because the parameters it depended on were the ones
being honoured. The dmClock profiles, `load_model`, `at_limit`, `uniform_cost`
and `capacity_relative` are read from JSON and have no CLI counterpart, so the
defaults bug did not touch them. `max_concurrent_requests` was wrongly pinned at
128 — but at 128 for *every arm equally*, so it is a constant, not a confound.

Scenario 11, `lim = 0`, res + wgt only, pristine dmclock, n = 3:

| | probe | probe drop | interactive | total |
| :-- | --: | --: | --: | --: |
| `throttler` | 1.2/s | 72.2% | 242.0/s | 1315 tps |
| `dmclock_coarse` | 4.1/s | 0.0% | 245.6/s | 1315 tps |
| `dmclock_fine_upstream` | 4.1/s | 0.0% | **918.4/s** | **1841 tps** |

Per-tenant keying protects the control plane and gives the interactive tenant
3.7× the throughput of the shared-queue arm. Internally valid as a comparison.
The absolute numbers should not be quoted until the config bug's effects are
re-measured.

## Void

| Claim | Why |
| :-- | :-- |
| The **throttler baseline** in the imported analysis | destroyed by the slot leak. `SimpleThrottler` increments `outstanding_requests` on *every* call including rejections, so with the leak it jammed after 128 calls and 503'd everything thereafter |
| Scenario 8 / adaptive AIMD, including the n=5 replication | `spike_amplitude_ms` was pinned at 0.0 by the CLI defaults bug. No scrub spikes were ever injected; the controller was reacting to nothing |
| "Capacity ≈ cluster_capacity / service_time" | `cluster_capacity` was pinned at 64 in every run. The 546/1894/1078 figures are real throughput-at-N-concurrent measurements, but the attribution was wrong |
| The concurrency-ceiling sweep | 16/32/64/128 all ran at 128, which is why they were identical |

## Correction: the imported analysis's central conclusion was *not* affected

Earlier in this session I said the imported results were void in their entirety.
That was wrong, and the data says so directly. Re-running scenario 6 with an
identical configuration, before and after the slot-leak fix:

| arm | published (leaked) | after the fix | changed? |
| :-- | :-- | :-- | :-- |
| `throttler` | [73, 54, 1] | [18718, 16521, 40] | **destroyed** |
| `dmclock_coarse` | [5, 0, 40] | [5, 0, 40] | **bit-identical** |
| `dmclock_fine` | [5, 112, 11] | [5, 415, 40] | understated |

The reason the arms differ is in the code. `SimpleThrottler` does
`outstanding_requests++` inside the rejection test, so it counts *every* call,
admitted or not — with the leak it jams after `max_requests` calls and never
recovers. `AsyncScheduler` only increments when it actually dispatches in
`process()`, and in scenario 6 the dmClock arms were limit-gated at ~45 dispatches,
far below the 128 ceiling, so the leak never bound on them at all.

Consequences:

- The **coarse-vs-fine comparison — where the central conclusion lives — was
  entirely unaffected.** `dmclock_coarse` starving tenant B to zero is a real
  measurement, reproduced bit-for-bit.
- The fine arm was *understated*: tenant B actually recovers 415 requests, not
  112. The conclusion is stronger than was reported, not weaker.
- It also closes an open thread. The "health probe only gets 11 of 40 probes
  under `dmclock_fine`" anomaly, which I chased at length and withdrew as
  unexplained, was this leak in the hand-rolled scheduler's own concurrency
  accounting. After the fix the probe gets 40/40, matching coarse.

So the imported analysis diagnosed the right problem and proposed the right fix.
What it got wrong was the baseline it measured against, and the absolute numbers.

## Claims I made and withdrew

Recorded because the pattern matters more than any single item: on several
occasions a first result was reported with more confidence than it deserved, and
a follow-up check overturned it.

| Claim | What happened |
| :-- | :-- |
| "`dmclock_fine` degrades health-probe cadence" | withdrawn — the metric was unreliable and `dmclock_coarse` was not a stable baseline either |
| "Rejections don't poison the limit tag" | wrong — I checked `add_request` and missed that `initial_tag` commits first |
| "The lockout is a dmclock bug" | wrong — an upstream test asserts the behaviour deliberately |
| "The diagnosis is confirmed" (of the patch) | overstated — the mechanism was confirmed, the characterisation as a defect was not |
| "N buckets → N×R is a flaw in the imported analysis" | wrong — it is a consequence of *my* choice to key on bucket |
| "Three divergences that dominate correlation" | two of the three were the proposal working as intended, not defects |

## The lesson

Every conclusion that survived rests on source code or arithmetic. Every
conclusion that collapsed rested on the simulator alone. The benchmark's value
here was in raising questions worth chasing into the source — not in settling
them.

That argues for a specific discipline going forward: treat a benchmark result as
a lead, and do not report it as a finding until there is a code-level or
arithmetic reason why it must be true. The guard rails added this session
(provenance stamping, the zero-rejection warning) help, and a config round-trip
assertion — dump what the binary parsed, diff it against the file — would have
caught the defaults bug on the first run.
