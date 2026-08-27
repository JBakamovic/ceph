# `AtLimit::Reject` Locks Clients Out Instead of Throttling Them

Production RGW builds its dmClock scheduler with plain `dmc::AtLimit::Reject`
([rgw_asio_frontend.cc:484](../../src/rgw/rgw_asio_frontend.cc#L484)). In that
mode, a client whose offered rate exceeds its configured limit is not throttled
down to the limit — it is rejected almost entirely, and stays rejected.

## The mechanism

`do_add_request` computes the tag, then decides whether to reject
([dmclock_server.h:982](../../src/dmclock/src/dmclock_server.h#L982)):

```cpp
RequestTag tag = initial_tag(TagCalc{}, client, req_params, time, cost);

if (at_limit == AtLimit::Reject &&
    tag.limit > time + reject_threshold) {
  return EAGAIN;                       // rejected
}
client.add_request(tag, std::move(request));
```

The rejection happens before `client.add_request()`, so it is tempting to
conclude that a rejected request leaves no trace. It does. `initial_tag` updates
the client's running tag **unconditionally**, as its own comment says
([dmclock_server.h:891](../../src/dmclock/src/dmclock_server.h#L891)):

```cpp
RequestTag tag(client.get_req_tag(), *client_info, params, time, cost, ...);
// copy tag to previous tag for client
client.update_req_tag(tag, tick);      // <-- advances even when we then reject
return tag;
```

So every *arrival* advances `prev_tag.limit` by `cost / limit`, whether or not it
is admitted. The limit tag therefore advances at the **offered** rate, while it
is only allowed to be compared against wall-clock time. Once

```
offered rate > limit
```

the tag advances faster than real time, runs away into the future, and
`tag.limit > time` is true for every subsequent arrival. The client is locked
out, and each retry pushes it further out — which matters because S3 clients
retry on 503.

## Measured

Scenario: bully tenant offering ~4270 ops/s, per-tenant dmClock on the production
`AsyncScheduler`, capacity ~1900 ops/s.

dmClock's own counters, with the two data tenants split across op classes so
their counters separate:

| op class | tenant | rejected at limit | dispatched (res) | dispatched (prio) | qlen at end |
| :-- | :-- | --: | --: | --: | --: |
| `data` | bully, offers 4270/s | **42692** | 1 | 0 | 0 |
| `metadata` | interactive, offers 993/s | 8650 | 1 | 1278 | 0 |
| `admin` | probe, offers 4/s | 0 | 41 | 1 | 0 |

Both data tenants had an identical limit of 1140 ops/s. The one offering below
its limit was served; the one offering above it was admitted **once in ten
seconds**.

Sweeping the bully's limit against its fixed ~4270 ops/s offered load:

| bully limit | vs offered | bully accepted | total |
| --: | :-- | --: | --: |
| 38/s | below | 0.1/s | 131 tps |
| 95/s | below | 0.1/s | 131 tps |
| 1140/s | below | 0.1/s | 131 tps |
| 1900/s | below | 0.1/s | 131 tps |
| 3800/s | below | 0.3/s | 133 tps |
| 4750/s | **above** | run did not complete in 100 s | — |
| 5700/s | **above** | run did not complete in 100 s | — |

Two things fall out, and both follow from the mechanism:

- **Lowering the limit makes it worse, not better.** A smaller limit means a
  larger `1/limit` increment per arrival, so the tag runs away faster. This is
  the opposite of how a rate limit is supposed to behave.
- **There is no stable operating point where the limit binds.** Below the offered
  rate the client is locked out; at or above it the gate stops rejecting
  altogether and the queue grows without bound until the run stops making
  progress. The only regime that works is the one where the limit is not
  actually limiting anything.

## Why this matters for RGW

This is the mode production runs in. The consequences on a real gateway:

- A tenant that bursts above its configured limit is not shaped down to it — it
  is effectively cut off, and its own retries keep it cut off.
- Because the tag advances per *arrival*, the punishment scales with how hard a
  client retries rather than with how much work it actually gets done.
- Operators tuning limits downward to control an aggressive tenant would make
  the lockout sharper, which is the reverse of the intended effect.

It also explains the earlier observation that per-tenant dmClock delivered only
131 tps against the throttler's 1313: the bully was not being throttled to its
190 ops/s reservation, it was locked out entirely, and roughly a thousand ops/s
of usable capacity simply went unserved.

## The fix, and what it recovers

`do_add_request` now snapshots the client's tag before calling `initial_tag` and
restores it when the request is rejected, so a request that never runs no longer
consumes rate budget:

```cpp
const RequestTag saved_tag = client.get_req_tag();
RequestTag tag = initial_tag(TagCalc{}, client, req_params, time, cost);

if (at_limit == AtLimit::Reject && tag.limit > time + reject_threshold) {
  client.restore_req_tag(saved_tag, tick);   // a request that never runs must not count
  return EAGAIN;
}
```

`restore_req_tag` assigns unconditionally rather than going through
`assign_unpinned_tag`, which deliberately skips pinned min/max values — right
when advancing a tag, wrong when undoing one. `last_tick` is left current, since
a client being rejected is still active and should not age out as idle.
`do_add_request` is the only caller of `initial_tag`, and the `DelayedTagCalc`
path is untouched (it cannot be combined with `AtLimit::Reject` anyway).

**The bully now tracks its configured limit**, offering ~4270 ops/s throughout:

| bully limit | before | after |
| --: | --: | --: |
| 38/s | 0.1/s | **37.7/s** |
| 95/s | 0.1/s | **92.8/s** |
| 1900/s | 0.1/s | **1325.1/s** (capacity-bound) |

Lowering the limit now reduces throughput proportionally instead of deepening a
lockout. That sign flip is the confirmation: it distinguishes a working rate
limit from a runaway tag.

## What it does to the scheduler comparison

Scenario 11, 5504 ops/s offered against ~1900 usable, n = 3, run-to-run variance
under 0.3%:

| | probe | probe drop | interactive | interactive p50 | bully | total |
| :-- | --: | --: | --: | --: | --: | --: |
| `throttler` | 1.0/s | **77.0%** | 239.9/s | 20.9 ms | 1071.1/s | 1312 tps |
| `dmclock_coarse` | 4.2/s | 0.0% | 173.5/s | 5.9 ms | 767.5/s | 945 tps |
| `dmclock_fine_upstream` | 4.2/s | **0.0%** | **533.0/s** | **5.3 ms** | 906.4/s | **1444 tps** |

Per-tenant dmClock now wins on every axis at once: it serves every health probe
where the throttler fails 77% of them, gives the interactive tenant 2.2× the
throughput at a quarter of the latency, and still delivers the highest total
throughput of the three.

The intra-class result is also visible in its proper form. `dmclock_coarse`
protects the `admin` probe perfectly but gives the interactive tenant only
173.5/s, because every S3 tenant shares `client_id::data` and the bully dominates
inside it. Per-tenant keying raises that to 533/s — a 3.1× improvement from the
composite `client_id` alone.

This reverses the reading from earlier in the day. Before the patch, per-tenant
dmClock delivered 131 tps against the throttler's 1313, and that looked
disqualifying: protecting the control plane by discarding nine tenths of the
gateway is not a trade anyone would take. That trade never existed — it was the
lockout.

## Caveats

- The obvious remaining failure mode is unchanged and expected: when the limit is
  set at or above the client's offered rate, the gate stops rejecting and the
  queue grows without bound. A limiter above the offered load is not limiting.
- `unittest_rgw_dmclock_scheduler` passes 10/10, but that only exercises the RGW
  wrapper. dmclock's own suite is gated behind `WITH_DMCLOCK_TESTS`, which
  defaults to `OFF`, and Ceph's CMake notes that even when enabled *"add_test is
  not being called, so dmclock tests aren't part of ceph tests"* — so this
  scheduler's upstream tests never run in Ceph. Until `test_dmclock_server.cc`
  passes against this change, treat the patch as effective rather than correct.
  If some test asserts the present behaviour, the fix belongs elsewhere.
- Whether upstream considers the current behaviour intended is still unknown.
  `AtLimit::Wait` and `AtLimit::Allow` do not take this path, and RGW could use
  either instead.
