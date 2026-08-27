# The Throttle-Slot Leak

The harness leaked its admission-control slots. Once `outstanding_requests`
passed `max_requests`, admission control seized and every subsequent request was
rejected for the remainder of the run. Every throughput and drop-rate figure
produced before this was fixed was measured against a jammed gateway.

## The bug

The harness released its slot by assigning an empty completer:

```cpp
completer = dmc::SchedulerCompleter{};   // intended: release the slot
```

`SchedulerCompleter` is `Completer<std::function<void()>>`, whose move-assignment
is defaulted ([rgw_dmclock_scheduler.h:62](../../src/rgw/rgw_dmclock_scheduler.h#L62)):

```cpp
~Completer() { if (f) { f(); } }
Completer& operator=(Completer&& other) = default;
```

The completion only runs from the **destructor**. Assigning over the object
replaces `f` without ever invoking it, so `request_complete()` is never called
and the slot is gone for good.

Production gets this right by never assigning: `rgw_process.cc` declares
`rgw::dmclock::SchedulerCompleter c;` at function scope and lets it die at the
end of `process_request()`.

The fix is to release by destruction:

```cpp
{ dmc::SchedulerCompleter release = std::move(completer); }
```

## Why the published throttler results were impossible anyway

This does not rest on any measurement. In a closed-loop generator, in-flight
requests can never exceed the worker count, so a throttler whose ceiling is above
that count cannot reject anything — ever.

| Scenario | workers | `max_concurrent_requests` | can the throttler reject? |
| :-- | --: | --: | :-- |
| 0_production_defaults | 76 | 1024 | **no** |
| 1_noisy_neighbor | 66 | 128 | **no** |
| 2_metadata_crawler_storm | 70 | 128 | **no** |
| 3_backend_congestion_and_spikes | 56 | 128 | **no** |
| 4_tiered_sla_isolation | 90 | 128 | **no** |
| 5_retry_storm_dynamics | 65 | 64 | yes |
| 6_intra_class_tenant_starvation | 76 | 128 | **no** |
| 7_multi_tier_tenant_qos | 121 | 128 | **no** |
| 8_adaptive_capacity_tuning | 111 | 128 | **no** |

Every one of those scenarios reports the throttler dropping >99% of requests.
With the ceiling above the worker count that cannot happen, which is the
arithmetic signature of the leak: the counter was climbing on rejections and
never coming back down.

## What the corrected run shows

Scenario 6, identical configuration, accepted ops per tenant, n = 3:

| Scheduler | Tenant A (bully) | Tenant B (interactive) | Tenant C (probe) | total |
| :-- | --: | --: | --: | --: |
| `throttler` — published | 73 | 54 | 1 | 12.8 tps |
| `throttler` — after fix | **18718 / 19561 / 19828** | **16521 / 17505 / 17804** | **40 / 40 / 40** | **3487–3725 tps** |
| `dmclock_coarse` — after fix | 5 / 5 / 5 | **0 / 0 / 0** | 40 / 40 / 40 | 4 tps |
| `dmclock_fine_upstream` — after fix | 5 / 5 / 5 | 400 / 433 / 429 | 40 / 40 / 40 | 44–47 tps |

With a working throttler the workload produces **zero drops for every tenant**,
the health probe completes all 40 of its probes, and the gateway sustains around
3600 tps. There is no starvation, because scenario 6 never overloaded anything —
it only appeared to because admission control had seized.

The same is true of the production-defaults scenario, where all three schedulers
are now indistinguishable *and healthy* (n = 3, accepted per tenant):

| Scheduler | total tps |
| :-- | --: |
| `throttler` | 3489 / 3533 / 3594 |
| `dmclock_coarse` | 3626 / 3547 / 3557 |
| `dmclock_fine_upstream` | 3695 / 3554 / 3491 |

Every tenant is fully served in every arm, probe included. The earlier reading of
this scenario — "all three schedulers identical, health probe starved to 1
request" — was the leak, not the configuration.

The dmClock arms in scenario 6 now look bad for the mirror-image reason described
in [harness-fidelity.md](harness-fidelity.md): the scenario's absolute
reservations and limits (R = 10–50, L = 50–100 ops/s) sit far *below* the
backend's real capacity of roughly 1900 ops/s at `cluster_capacity = 64`, so
dmClock throttles the gateway to 1–2% of what it can do. Production's shipped
defaults have the opposite problem — reservations above capacity, which makes
dmClock inert. Both failures come from the same root: **absolute rates configured
without reference to what the cluster can actually serve.**

## Measured capacity, for reference

Throughput with in-flight pinned at `cluster_capacity` (no congestion, p50 at the
29.3 ms base latency):

| `cluster_capacity` | before the fix | after the fix |
| --: | --: | --: |
| 16 | 2.0/s | **546.4/s** |
| 64 | 8.0/s | **1894.3/s** |
| 256 | 32.0/s | 1078.4/s (bound by the 2000/s offered load) |

546.4/s at capacity 16 is exactly `16 / 29.3 ms`, so the backend model behaves as
designed once slots are returned.

## What this invalidates

- **The throttler baseline, entirely.** Every "the default throttler starves
  everyone" result is an artifact. On these workloads a working throttler does
  not starve anyone.
- **All absolute throughput and drop-rate figures**, in the imported analysis and
  in everything measured earlier in this session.
- **The scenarios themselves.** None of them (except 5) generates real overload.
  A closed-loop generator with fewer workers than the throttle ceiling cannot,
  by construction. They need rebuilding around the now-known capacity: either far
  more workers, or open-loop arrival rates above ~1900/s rather than the 244/s in
  `9_open_loop_starvation.json`.

## What may survive

The comparison *between* the dmClock arms does not involve the throttler and is
not obviously affected by the leak: Tenant B still gets 0 accepted under
`dmclock_coarse` and 400–433 under `dmclock_fine_upstream`, reproducibly. That
gap is caused by every S3 tenant sharing `client_id::data`, which is a property
of the code rather than of the measurement, and the composite `client_id` still
removes it.

The caveat is the regime. That isolation is being demonstrated inside a
configuration where dmClock has throttled the gateway to 4–47 tps out of ~1900
available — dmClock itself is the bottleneck, not the backend. It shows the
mechanism works; it does not show anyone would deploy it that way.

What is no longer supported by anything is "per-tenant dmClock beats the shipped
default". On the corrected scenario 6 the shipped default serves every tenant
with zero drops at roughly 80× the throughput of the per-tenant arm. Rebuilding
the scenarios so they generate genuine overload is now the prerequisite for any
performance claim in this project.
