# Review of the Imported Findings

The analysis in [findings.md](findings.md) and [architecture.md](architecture.md)
was produced in Antigravity against the simulation harness.

**Nothing the benchmark validated has been invalidated.** The central result —
all S3 tenants share `client_id::data`, an aggressive tenant starves interactive
ones completely, per-tenant queues fix it, and the effect is architectural rather
than capacity-bound — was independently reproduced here and stands in full.

What follows is of three kinds, and only the first is about the benchmark at all:

- **Constraints found outside the benchmark's scope** (§1, §2). Discovered by
  changing `src/rgw/` rather than by measuring. The simulator could not have
  posed these questions; they constrain *how* the fix is built, not *whether* it
  works.
- **Statistics that do not carry the weight placed on them** (§3, §5). The
  underlying results hold; some of the numbers used to summarise them are
  artifacts of the measurement loop or single observations reported as findings.
- **One claim that should be withdrawn on both sides** (§4) — including a
  stronger counter-claim made in an earlier draft of this document, which further
  measurement did not support.

Everything below was checked against the code or re-measured, not inferred.

---

## 1. The tenant cannot be identified at admission time

*Not a benchmark result. The harness assigns `tenant_idx` by construction, so it
never had occasion to ask where identity comes from.*

**Imported claim** (architecture §9): replace `client_id` with
`{tenant_id, op_class}` and store per-tenant profiles in `RGWUserInfo`.

**What the code says**: `process_request()` calls `schedule_request()` at
[rgw_process.cc:378](../../src/rgw/rgw_process.cc#L378) and
`op->verify_requester()` at line 392. `s->set_user()` has installed an empty
`rgw_user()` by then. There is no authenticated user to key on. Deferring
admission until after auth would mean doing signature verification for requests
we are about to reject — which is most of what admission control exists to avoid.

What *is* available is the bucket, parsed by `init_from_header()` inside
`get_handler()`. So this is **per-bucket QoS**, not per-user QoS.

**A consequence of keying on the bucket** — this is a property of the choice made
*here*, not a flaw in the imported analysis, which assumed user keying where it
does not arise: dmClock
grants each queue its own reservation `R`. If the queue key is the bucket, a
tenant with *N* buckets receives *N×R*. Creating buckets is cheap and
unprivileged, so per-bucket keying hands a bully a way to multiply its own
guaranteed share — reintroducing the starvation the change exists to remove, by
a different route.

This has to be resolved before per-tenant profiles (step 2.2) are designed, since
it decides whether profiles attach to buckets or to users. Three options, none
free:

| Option | Identity available pre-auth | Multiplication risk | Cost |
| :-- | :-: | :-- | :-- |
| Bucket | ✔ | **N buckets → N×R** | none |
| Declared access key from `Authorization` | ✔ | none | spoofable: a client can park itself in another tenant's queue |
| Authenticated user | ✖ | none | requires admitting after auth |

A fourth possibility is keying on the bucket but resolving the *owner* from a
cache populated after the first authenticated request — bounded staleness in
exchange for correct attribution.

Recorded in [architecture §8a](architecture.md).

---

## 2. The AIMD controller works, but not for the stated reason

*Not a benchmark result either. The controller demonstrably worked — that part of
the imported analysis is empirically correct. Only its explanation is wrong, and
the difference bites the next step rather than the last one.*

**Imported claim** (session 3 transcript): *"I've confirmed
`crimson::dmclock::PullPriorityQueue` can handle dynamic `ClientInfo` updates,
since it uses a pointer in `client_info_f`. This means I can update
`max_requests` and associated client limits without major issues."*

**What the code says**: `get_cli_info()`
([dmclock_server.h:865](../../src/dmclock/src/dmclock_server.h#L865)) only calls
`client_info_f` again when `is_dynamic_cli_info_f` is true, and that is the `U1`
template parameter, which defaults to `false`. RGW instantiates
`PullPriorityQueue<client_id, Request, IsDelayed>` — so `U1` is `false`, and
`ClientRec::info` is a `const ClientInfo*` captured once when the client is first
seen.

Runtime updates therefore work *only* because the pointed-to object is mutated in
place:

- the harness's `update_capacity()` calls `client_infos[i].update(r, w, l)` on a
  `std::vector` that was `reserve()`d up front and never resized;
- RGW's `ClientConfig::update()` does `clients.clear()` followed by four
  `emplace_back` calls, and `clear()` preserves capacity, so the addresses happen
  to survive.

Both are correct by accident of allocation, not by design.

**Why this matters now**: step 2.2 wants `ClientConfig` to resolve profiles
per tenant, which means a container that grows as tenants appear. The moment that
container reallocates, every `ClientRec::info` pointer into it dangles —
a use-after-free on the request path. The fix is to choose deliberately:
instantiate the queue with `U1=true` so the info function is consulted on every
tag computation, or guarantee node stability (`std::map`/`std::deque`) *and* call
`update_client_infos()` on config change. `update_client_info()` and
`update_client_infos()` exist at
[dmclock_server.h:635](../../src/dmclock/src/dmclock_server.h#L635) and are
currently never called from RGW.

One thing that *does* work in favour of per-tenant queues: idle clients are
erased from `client_map` after `erase_age`
([dmclock_server.h:1228](../../src/dmclock/src/dmclock_server.h#L1228)), so
queues for departed tenants are reaped and the map does not grow without bound.

---

## 3. Drop-rate percentages are not comparable across schedulers

*A metric problem, not a hypothesis problem. The starvation result is unaffected.*

**Imported claim** (findings §3): Tenant B is *"99.8% dropped"* under `throttler`
versus *"88.2% accepted"* under `dmclock_fine`.

**What the harness does**: the worker loop
([bench_rgw_scheduler.cc:1082](../../src/test/rgw/bench_rgw_scheduler.cc#L1082))
is closed-loop, and the offered load is a function of the scheduler under test:

- `schedule_request(..., yield)` **suspends the worker** while the request waits
  in the dmClock queue, so a queueing scheduler depresses `attempted`;
- on rejection the worker sleeps a fixed **5 ms** and `continue`s, **skipping the
  pacing block entirely**, so a rejecting scheduler inflates `attempted`
  regardless of the tenant's configured think time.

The scenario 6 numbers show both effects plainly. Same workload, same 10 s:

| Tenant (configured pacing) | `throttler` attempted | `coarse` attempted | `fine` attempted |
| :-- | --: | --: | --: |
| A — Bully (0 ms) | 119,524 | 119,291 | 119,121 |
| B — Interactive (2 ms, 15 workers) | 29,927 | 29,940 | **127** |
| C — Health probe (250 ms, 1 worker) | **1,948** | 40 | 12 |

Tenant C is configured for ~40 probes in 10 s. Under `throttler` it issued 1,948
— one every 5.1 ms, exactly the rejection sleep. Under `fine`, Tenant B's
attempts collapse by two orders of magnitude because its workers are parked in
the queue.

**What survives**: accepted counts over a fixed wall clock are throughput and
*are* comparable. Tenant B going 54 → 0 → 112 accepted across
throttler/coarse/fine is real, and so is the starvation result. The p50 latencies
are real.

**What does not**: any statement of the form "X% dropped vs Y% accepted" compared
across schedulers. A queueing scheduler converts drops into latency; scoring it
on drop rate rewards it for the conversion. The 88.2%-accepted figure describes
a client that waited 347 ms, not one that was served promptly.

---

## 4. The health-probe cadence metric is unreliable — claim withdrawn on both sides

*An earlier draft of this document asserted that `dmclock_fine` degrades the
health probe's cadence while `dmclock_coarse` protects it. Further measurement
did not support that, and it is withdrawn.*

**What prompted it.** `architecture.md` §197 labels the probe's SLA
*"GUARANTEED"* under both `dmclock_coarse` (0% drops, 2.5 ms) and `dmclock_fine`
(8.3% drops, 2.5 ms). But [findings.md](findings.md) §36 reports, correctly and
in its own table, that the probe made **40 attempts** under coarse and **12**
under fine, against ~40 expected from 1 worker at 250 ms pacing over 10 s. The
data was reported accurately; the executive summary just did not reflect it.

**What the follow-up showed.** Scenario 6, bully worker count swept 1→60, 10 s
each:

| bully workers | `coarse` probe attempted/accepted | `fine` probe attempted/accepted |
| --: | :-: | :-: |
| 1 | **16 / 15** | 11 / 10 |
| 5 | 40 / 40 | 12 / 11 |
| 20 | 40 / 40 | 12 / 11 |
| 60 | 40 / 40 | 12 / 11 |

Two things fall out, and they cut against the counter-claim:

1. `fine` is completely **load-independent** — 11–12 attempts whether the bully
   runs 1 worker or 60. Contention cannot explain it.
2. `coarse` is **not a stable baseline** — at bully=1 it gives 16/15, not 40/40.
   The 40/40 figure the "GUARANTEED" label rests on is itself conditional on
   something uncharacterised.

Per-request latency is 2.5 ms and *max* is 2.7 ms in every cell, so no request
ever waited. The missing time is between requests, outside anything the harness
measures.

**A mechanism was proposed and disproved.** The leading hypothesis was that the
harness's fine scheduler binds client completions to its own strand —
`async_request` passes `strand` as the completion executor
([bench_rgw_scheduler.cc:922](../../src/test/rgw/bench_rgw_scheduler.cc#L922))
where upstream's `AsyncScheduler` passes `timer.get_executor()`
([rgw_dmclock_async_scheduler.h:157](../../src/rgw/rgw_dmclock_async_scheduler.h#L157))
— which would serialise every client's think-time timer behind all scheduling
work. That difference from upstream is real. But changing the harness to use the
io_context executor and re-running three times produced **identical** results
(probe 12/11, Tenant B 127/112 @ ~340 ms, 12.8 tps). The hypothesis is wrong and
the harness change was reverted.

**Where this leaves it.** The probe's `attempted` count is not a trustworthy
measure of client cadence in this harness, and the cause is unknown. Neither
*"GUARANTEED under `dmclock_fine`"* nor *"`dmclock_fine` degrades the probe"* is
supported. It is an open question, not a finding, and it should not be cited in
either direction until someone instruments the worker loop.

---

## 5. The scenario 8 numbers are single-run point estimates and do not replicate exactly

**Imported claim** (findings §5): the AIMD controller cuts Platinum VIP p50 by
41.3% (405.1 → 237.7 ms) and improves Jain's fairness by 12.3% (0.536 → 0.602).

**Re-measured**, five runs of each arm:

| Metric | static | adaptive | imported |
| :-- | :-- | :-- | :-- |
| Jain's fairness | 0.530 ± 0.010 | 0.574 ± 0.008 | 0.536 → 0.602 |
| Platinum VIP p50 | 434.7 ± 7.4 ms | 183.7 ± 56.5 ms | 405.1 → 237.7 ms |

**Both directions hold, and the fairness gain is solid** — 0.530 vs 0.574 is more
than four standard deviations apart, so it is not noise. But:

- the fairness improvement is **+8.3%**, not +12.3%. The imported 0.602 was the
  top of the distribution; five runs never exceeded 0.587.
- the latency improvement is **−58%**, better than the reported −41.3%.
- the adaptive arm's variance is **eight times** the static arm's (56.5 vs 7.4 ms).
  The controller's benefit is real but far less predictable than one run suggests,
  which is exactly the property you want to know about a closed-loop controller
  before putting it on a request path.

None of the imported scenario results carry repeat counts or error bars. They
should be read as single observations.

---

## Summary

| # | Kind | Effect on the imported conclusions |
| :-- | :-- | :-- |
| 1 | Constraint outside the benchmark's scope | None. Constrains *how* per-tenant keying is implemented in real RGW; the simulator never modelled identity resolution. |
| 2 | Explanation, not result | None. The AIMD controller worked and the measurements stand. The stated reason was wrong, which becomes a use-after-free hazard only if step 2.2 is built naively. |
| 3 | Metric | Starvation result unaffected. Cross-scheduler *drop-rate* comparisons are artifacts of the closed-loop worker loop; throughput and latency comparisons are sound. |
| 4 | Withdrawn | None. Both the "GUARANTEED" label and this document's earlier counter-claim are unsupported; the metric itself is unreliable. Open question. |
| 5 | Statistics | Direction confirmed at n=5. Magnitudes revised (fairness +8.3% not +12.3%; VIP latency −58% not −41%), and the adaptive arm's variance is 8× the static arm's. Originals were single runs. |

### Why this session found different things

Not because the benchmark was wrong — it was reproduced here and it holds. The
two efforts were looking from different places:

- The imported session validated hypotheses **inside the simulator**. That
  answers "does per-tenant queueing fix intra-class starvation in this model?"
  Answer: yes, decisively, and unchanged.
- This session's work came from **contact with the production code** — actually
  editing `src/rgw/` — which forces questions the simulator cannot pose: where
  does tenant identity come from, and is the `ClientInfo` pointer still valid?
  Neither is a benchmark question, and neither has a benchmark answer.
- Two smaller differences: repeat runs (n=5 against n=1) turned point estimates
  into distributions, and reading the harness source turned some reported
  percentages into artifacts of the measurement loop.

The imported work established *what* to build. This work is establishing what it
costs to build it for real. The diagnosis is unchanged; the shape of the fix is
what moved.
