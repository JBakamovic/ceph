# First Results Under Genuine Overload

With the [throttle-slot leak](throttle-slot-leak.md) fixed, the open-loop
generator working, and scheduler profiles expressed relative to measured
capacity, the benchmark finally stresses admission control. These are the first
results from that regime.

Setup: capacity measured at ~1900 ops/s (`cluster_capacity = 64`), open-loop
Poisson arrivals, production rejection semantics (`AtLimit::Reject`), production
cost model (uniform cost 1), `dmclock_fine_upstream` running the real
`rgw::dmclock::AsyncScheduler`.

## The intra-class starvation result holds

Offered 5504 ops/s (2.9× capacity), n = 3:

| | `throttler` | `dmclock_coarse` | `dmclock_fine_upstream` |
| :-- | --: | --: | --: |
| Health probe | 0.9/s of 4 — **77.8% fail** | **4.2/s — 0% dropped, 2.5 ms** | **4.2/s — 0% dropped, 2.5 ms** |
| Interactive | 239.9/s, 23.3 ms | **0.1/s — 100% dropped** | 126.8/s, **5.3 ms** |
| Bully | 1071.7/s | 0.0/s | 0.1/s |
| Total | 1313 tps | 4 tps | 131 tps |

Two claims from the original analysis survive, now measured in the regime where
they mean something and with production code on both dmClock arms:

- **The default throttler has no tenant awareness.** Under real overload the
  bully takes 82% of served throughput and 78% of health-probe requests fail. A
  gateway behaving this way would flap its liveness checks.
- **`client_id::data` causes intra-class starvation.** `dmclock_coarse` protects
  the `admin` probe perfectly and still drops the interactive tenant to 100%,
  because every S3 tenant shares one queue. Per-tenant keying fixes exactly that:
  identical probe protection, and the interactive tenant recovers to 127/s at
  5.3 ms.

## But per-tenant dmClock is not currently usable, and the reason is unknown

Total throughput never approaches the throttler's. Offered-load sweep, total
accepted:

| offered | `throttler` | `dmclock_fine_upstream` | fine: probe / interactive / bully |
| --: | --: | --: | :-- |
| 950/s (0.5×) | 951 | 412 | 10.4 / 148.6 / **253.0** |
| 1900/s (1.0×) | 1879 | 265 | 19.6 / 245.5 / **0.1** |
| 3800/s (2.0×) | 1341 | 299 | 34.4 / 264.7 / **0.1** |
| 5700/s (3.0×) | 1339 | 106 | 46.1 / 59.6 / **0.1** |

The throttler peaks at capacity and then holds ~1340 tps. Per-tenant dmClock
never exceeds 412 tps and falls to 106.

The sharp part is the bully: **253 ops/s at half capacity, then 0.1 ops/s at
capacity and above**, despite a reservation of 190 ops/s and a limit of
1140 ops/s. That is a cliff, not throttling, and its own reservation should
protect it. The few requests that do complete run at 29.6 ms — base latency, no
congestion — so when the bully runs it runs fine; it is almost never dispatched.

### Hypotheses tested and rejected

- **Rejections poisoning the limit tag.** Rejected. `do_add_request` returns
  `EAGAIN` before `client.add_request()`
  ([dmclock_server.h:985](../../src/dmclock/src/dmclock_server.h#L985)), so a
  rejected request never advances `prev_tag`.
- **Limit set above the achievable drain rate, causing the client to lock
  itself out.** Rejected. Lowering the bully's limit from 1140 to 95 to 38 ops/s
  leaves it at 0.1 ops/s throughout. Raising it to 380 or 1140 ops/s makes the
  run fail to complete within 120 s at all.

The hang at higher limits is itself a signal worth chasing.

### Why this matters beyond the harness

Production RGW constructs its scheduler with plain `dmc::AtLimit::Reject`
([rgw_asio_frontend.cc:484](../../src/rgw/rgw_asio_frontend.cc#L484)). If that
path excludes a heavy-op tenant regardless of its configured reservation, and
stalls outright at generous limits, then it misbehaves precisely where admission
control is supposed to earn its keep. Whether this is a dmClock property, an
interaction with heavy ops under a uniform cost model, or a harness defect is not
yet established.

## Next step

Stop guessing and instrument. `ClientCounters` already tracks `l_res`, `l_prio`,
`l_limit`, `l_qlen` and `l_cost` per op class
([rgw_dmclock_scheduler_ctx.h](../../src/rgw/rgw_dmclock_scheduler_ctx.h)); the
harness constructs them but never reports them. Exporting those per run would
show directly whether the bully's requests are being rejected at admission,
enqueued and never pulled, or pulled and starved at dispatch — which
distinguishes the remaining hypotheses in a single run.

Tuning ratios before that is premature: it would be fitting parameters to a
dispatch path that is not yet understood.
