# Full Suite Results

Every scenario in `benchmark_suite/scenarios/`, all three schedulers, n = 2, with
the throttle-slot leak fixed and the CLI-defaults bug fixed so the scenario files
are honoured for the first time. No run timed out.

Previous conclusions in this project rested almost entirely on scenario 6. Eight
independent workload families tell a much clearer story, and it is not the story
the imported analysis told.

## Total throughput (req/s, mean of 2)

| scenario | `throttler` | `dmclock_coarse` | `dmclock_fine_upstream` |
| :-- | --: | --: | --: |
| 1_noisy_neighbor | **4070** | 5 | 28 |
| 2_metadata_crawler_storm | **4266** | 1 | 26 |
| 3_backend_congestion_and_spikes | **972** | 7 | 8 |
| 4_tiered_sla_isolation | **2151** | 42 | 22 |
| 5_retry_storm_dynamics | **16** | 1 | 3 |
| 6_intra_class_tenant_starvation | **3607** | 4 | 46 |
| 7_multi_tier_tenant_qos | **3333** | 7 | 91 |
| 8_adaptive_capacity_tuning | **2880** | 7 | 88 |
| 0_production_defaults | 4072 | 4036 | 4066 |

## Finding 1: the "throttler starves everyone" claim fails in every family

The imported analysis reports the default throttler dropping >99% of requests and
starving health probes across scenarios 1–8. With the slot leak fixed, the
throttler serves **every tenant at its full offered rate**, health probes
included:

| scenario | probe offered | probe served under `throttler` |
| :-- | --: | --: |
| 1_noisy_neighbor | 5.0/s (1 worker @ 200 ms) | **4.9/s** |
| 3_backend_congestion | 6.7/s (@ 150 ms) | **5.9/s** |
| 6_intra_class | 4.0/s (@ 250 ms) | **4.0/s** |
| 7_multi_tier | 4.0/s | **3.9/s** |
| 8_adaptive | 4.0/s | **3.7/s** |

There is no starvation because these scenarios never overload the gateway. Each
is closed-loop with fewer workers than `rgw_max_concurrent_requests`, so the
throttler is structurally incapable of rejecting anything. The >99% drop rates in
the original results were the jammed slot counter, not the scheduler.

## Finding 2: dmClock as configured is catastrophically worse — everywhere

In every scenario with a finite limit, both dmClock arms collapse to 1–91 req/s
against the throttler's 972–4266. That is a 50–500× loss.

The cause is the same throughout. The legacy scenarios set limits of 20–100 ops/s
(and scenarios 1–5 set none at all, so tenants inherit the harness default of
`lim = 50`) while real capacity is 2000–4000 ops/s. Every tenant therefore offers
far above its limit, and the
[limit-tag runaway](dmclock-reject-lockout.md) locks it out entirely rather than
shaping it. Limits at 1–2% of capacity do not throttle to 1–2% of capacity; they
throttle to nothing.

**Scenario 0 is the control that proves it.** With `lim = 0` (unlimited — the
shipped default) all three schedulers are indistinguishable and all serve
everything:

| | Tenant A | Tenant B | probe |
| :-- | --: | --: | --: |
| `throttler` | 2130.0 | 1937.9 | 4.0 |
| `dmclock_coarse` | 2113.8 | 1918.5 | 4.0 |
| `dmclock_fine_upstream` | 2128.1 | 1933.9 | 4.0 |

No finite limit, no lockout. The collapse is entirely a function of setting one.

## Finding 3: intra-class isolation is real, and now well replicated

This is the one claim from the imported analysis that survives, and the full
suite supports it far better than scenario 6 alone did. In every family where the
shared `data` queue starves a tenant to zero, per-tenant keying rescues it:

| scenario | starved tenant | `dmclock_coarse` | `dmclock_fine_upstream` |
| :-- | :-- | --: | --: |
| 1_noisy_neighbor | Interactive Web CRUD | **0.0/s** | 22.5/s |
| 2_metadata_crawler_storm | Mobile Client | **0.0/s** | 25.0/s |
| 6_intra_class | Interactive Data | **0.0/s** | 42.0/s |
| 7_multi_tier | Platinum VIP / Gold | 1.8 / 1.4 | 44.5 / 40.9 |
| 8_adaptive | Platinum VIP / Gold | 1.9 / 1.3 | 43.5 / 40.0 |

Five independent workloads, consistent direction, large effect. The composite
`client_id` does what it was proposed to do.

The caveat is regime: this rescue happens inside a configuration where dmClock has
already crippled the gateway. Going from 0 req/s to 42 req/s is a real
improvement in isolation and an irrelevance next to the throttler's 3607.

## Finding 4: scenario 5 is the only closed-loop family that stresses admission control

It is the sole scenario whose worker count (65) exceeds its ceiling (64), and the
only one with `allow_retries = true`. There the throttler collapses to **16 req/s**
— by far its worst result anywhere, and worse than several dmClock runs.

That is worth pursuing separately. It suggests `SimpleThrottler` combined with
client-side retry backoff has a congestion-collapse mode of its own, which is
much closer to the original project's thesis than anything scenarios 1–4 and 6–8
showed. It is also the family whose design should be copied when rebuilding the
others, since it is the only one that actually creates back-pressure.

## What this means for the project

1. The problem the imported analysis set out to solve — one aggressive S3 tenant
   starving others through the shared `client_id::data` queue — **is real**, and
   the composite `client_id` fixes it, now shown across five workloads.
2. The evidence offered for it — that the default throttler fails catastrophically
   — **does not hold**. The throttler serves every tenant fully in seven of eight
   families. The scenarios were never overload tests.
3. **dmClock's limit must not be used for shaping.** Every legacy scenario is
   configured in the regime where it locks tenants out, which is why dmClock looks
   50–500× worse than doing nothing.
4. The next honest step is to rebuild the workload families around scenario 5's
   design — genuine back-pressure — with limits left at 0 and isolation carried by
   reservation and weight, then re-ask whether per-tenant keying beats the
   throttler when both are actually under load.
