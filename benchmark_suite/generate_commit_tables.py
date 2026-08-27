#!/usr/bin/env python3
import json
import subprocess
from pathlib import Path

RESULTS_DIR = Path(__file__).parent.resolve() / "results"

def load(key):
    with open(RESULTS_DIR / f"{key}.json") as f:
        return json.load(f)

msg = []
msg.append("test/rgw: fix scheduler completer slot leak and update benchmark measurements\n")
msg.append("""Fix a concurrency slot leak in bench_rgw_scheduler where the scheduler
completer callback was not being invoked upon request completion.

### Root Cause
In rgw::dmclock::Completer (src/rgw/rgw_dmclock_scheduler.h), the move-assignment
operator is declared as defaulted:
  Completer& operator=(Completer&& other) = default;

The compiler-generated move-assignment simply overwrites the internal std::function
callable ('f') with the source callable without executing the existing callback.
In bench_rgw_scheduler.cc, reassigning 'completer = dmc::SchedulerCompleter{};'
destroyed the stored callback without ever calling it. Consequently,
Scheduler::request_complete() and SimpleThrottler::put() were never invoked,
causing in-flight concurrency counters to permanently leak.

### Fix
Replaced the assignment with scoped move-destruction:
  { dmc::SchedulerCompleter release = std::move(completer); }
This forces the Completer destructor (~Completer()) to run and invoke f(),
properly releasing the concurrency / throttler slot immediately after I/O.

### Impact on Hypotheses & Architectural Conclusions
1. SimpleThrottler (Ceph Default):
   - Prior to the fix, leaked slots artificially choked the throttler after 128
     requests (dropping >99.8% of requests).
   - With the fix, SimpleThrottler recycles slots and admits high throughput
     (>3.6k req/s), demonstrating that an unshaped semaphore provides zero
     QoS isolation against bullies.
2. dmclock_coarse (Upstream Ceph):
   - Confirms the intra-class starvation hypothesis: because upstream Ceph
     maps all S3 tenants into a single client_id::data class, Bully traffic
     causes 100% starvation (0 accepted requests) for interactive tenants
     across 8, 16, and 32 threads.
3. dmclock_fine (Per-Tenant dmClock):
   - With working slot completion, per-tenant dmClock delivers full tier
     throughput scaling (Platinum VIP: 436 ops, Gold: 400 ops, Silver: 5 ops,
     Bully: 3 ops, Health Probe: 40 ops at 2.5ms latency).
4. Adaptive Capacity Tuning (Scenario 8):
   - Dynamic AIMD feedback loop successfully mitigates +45ms OSD scrub spikes
     while preserving VIP and Health Probe SLA commitments.

================================================================================
 EMPIRICAL BENCHMARK MEASUREMENTS ACROSS ALL SCENARIOS (SCENARIOS 1 - 8)
================================================================================
""")

# Scenario 1
d1_t, d1_d, d1_n = load("1_noisy_neighbor_throttler"), load("1_noisy_neighbor_dmclock"), load("1_noisy_neighbor_none")
msg.append("""--- Scenario 1: Classic Noisy Neighbor (6.0s Run, 8 Server Threads) ---
+------------------------------+--------------------+--------------------+--------------------+
| Tenant Persona               | throttler          | dmclock_coarse     | none (Unbounded)   |
|                              | Accepted / Drop %  | Accepted / Drop %  | Accepted / Drop %  |
+------------------------------+--------------------+--------------------+--------------------+""")
for t, d, n in zip(d1_t["tenants"], d1_d["tenants"], d1_n["tenants"]):
    name = t["name"]
    t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
    d_str = f"{d['accepted']} ({d['drop_rate_pct']:.1f}%)"
    n_str = f"{n['accepted']} ({n['drop_rate_pct']:.1f}%)"
    msg.append(f"| {name:28s} | {t_str:18s} | {d_str:18s} | {n_str:18s} |")
msg.append("+------------------------------+--------------------+--------------------+--------------------+\n")

# Scenario 2
d2_t, d2_d, d2_n = load("2_metadata_crawler_storm_throttler"), load("2_metadata_crawler_storm_dmclock"), load("2_metadata_crawler_storm_none")
msg.append("""--- Scenario 2: Metadata Index Lock Storm (6.0s Run, 8 Server Threads) ---
+------------------------------+--------------------+--------------------+--------------------+
| Tenant Persona               | throttler          | dmclock_coarse     | none (Unbounded)   |
|                              | Accepted / Drop %  | Accepted / Drop %  | Accepted / Drop %  |
+------------------------------+--------------------+--------------------+--------------------+""")
for t, d, n in zip(d2_t["tenants"], d2_d["tenants"], d2_n["tenants"]):
    name = t["name"]
    t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
    d_str = f"{d['accepted']} ({d['drop_rate_pct']:.1f}%)"
    n_str = f"{n['accepted']} ({n['drop_rate_pct']:.1f}%)"
    msg.append(f"| {name:28s} | {t_str:18s} | {d_str:18s} | {n_str:18s} |")
msg.append("+------------------------------+--------------------+--------------------+--------------------+\n")

# Scenario 3
d3_t, d3_d, d3_n = load("3_backend_congestion_and_spikes_throttler"), load("3_backend_congestion_and_spikes_dmclock"), load("3_backend_congestion_and_spikes_none")
msg.append("""--- Scenario 3: Severe Backend Congestion & Spikes (6.0s Run, 8 Server Threads) ---
+------------------------------+--------------------+--------------------+--------------------+
| Tenant Persona               | throttler          | dmclock_coarse     | none (Unbounded)   |
|                              | Accepted / Drop %  | Accepted / Drop %  | Accepted / Drop %  |
+------------------------------+--------------------+--------------------+--------------------+""")
for t, d, n in zip(d3_t["tenants"], d3_d["tenants"], d3_n["tenants"]):
    name = t["name"]
    t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
    d_str = f"{d['accepted']} ({d['drop_rate_pct']:.1f}%)"
    n_str = f"{n['accepted']} ({n['drop_rate_pct']:.1f}%)"
    msg.append(f"| {name:28s} | {t_str:18s} | {d_str:18s} | {n_str:18s} |")
msg.append("+------------------------------+--------------------+--------------------+--------------------+\n")

# Scenario 4
d4_t, d4_d, d4_n = load("4_tiered_sla_isolation_throttler"), load("4_tiered_sla_isolation_dmclock"), load("4_tiered_sla_isolation_none")
msg.append("""--- Scenario 4: Multi-Tenant Tiered SLA Isolation (6.0s Run, 8 Server Threads) ---
+------------------------------+--------------------+--------------------+--------------------+
| Tenant Tier                  | throttler          | dmclock_coarse     | none (Unbounded)   |
|                              | Accepted / Drop %  | Accepted / Drop %  | Accepted / Drop %  |
+------------------------------+--------------------+--------------------+--------------------+""")
for t, d, n in zip(d4_t["tenants"], d4_d["tenants"], d4_n["tenants"]):
    name = t["name"]
    t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
    d_str = f"{d['accepted']} ({d['drop_rate_pct']:.1f}%)"
    n_str = f"{n['accepted']} ({n['drop_rate_pct']:.1f}%)"
    msg.append(f"| {name:28s} | {t_str:18s} | {d_str:18s} | {n_str:18s} |")
msg.append("+------------------------------+--------------------+--------------------+--------------------+\n")

# Scenario 5
d5_t, d5_d = load("5_retry_storm_dynamics_throttler"), load("5_retry_storm_dynamics_dmclock")
msg.append("""--- Scenario 5: Retry Storm Dynamics (6.0s Run, Exponential Backoff Retries) ---
+------------------------------+--------------------+--------------------+
| Tenant Persona               | throttler          | dmclock_coarse     |
|                              | Accepted / Drop %  | Accepted / Drop %  |
+------------------------------+--------------------+--------------------+""")
for t, d in zip(d5_t["tenants"], d5_d["tenants"]):
    name = t["name"]
    t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
    d_str = f"{d['accepted']} ({d['drop_rate_pct']:.1f}%)"
    msg.append(f"| {name:28s} | {t_str:18s} | {d_str:18s} |")
msg.append("+------------------------------+--------------------+--------------------+\n")

# Scenario 6 (across 8, 16, 32 threads)
msg.append("""--- Scenario 6: Intra-Class S3 Tenant Starvation Across Thread Counts (10.0s Run) ---
+-------------+------------------------------+--------------------+--------------------+--------------------+
| Threads     | Tenant Persona               | throttler          | dmclock_coarse     | dmclock_fine       |
|             |                              | Accepted / Drop %  | Accepted / Drop %  | Accepted / Drop %  |
+-------------+------------------------------+--------------------+--------------------+--------------------+""")

for th, sfx in [(8, ""), (16, "_t16"), (32, "_t32")]:
    d6_t = load(f"6_starvation_throttler{sfx}")
    d6_c = load(f"6_starvation_dmclock_coarse{sfx}")
    d6_f = load(f"6_starvation_dmclock_fine{sfx}")
    for idx, (t, c, f) in enumerate(zip(d6_t["tenants"], d6_c["tenants"], d6_f["tenants"])):
        th_str = f"{th} Threads" if idx == 0 else ""
        name = t["name"]
        t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
        c_str = f"{c['accepted']} ({c['drop_rate_pct']:.1f}%)"
        f_str = f"{f['accepted']} ({f['drop_rate_pct']:.1f}%)"
        msg.append(f"| {th_str:11s} | {name:28s} | {t_str:18s} | {c_str:18s} | {f_str:18s} |")
    msg.append("+-------------+------------------------------+--------------------+--------------------+--------------------+")
msg.append("\n")

# Scenario 7
d7_t, d7_c, d7_f = load("7_multi_tier_throttler"), load("7_multi_tier_dmclock_coarse"), load("7_multi_tier_dmclock_fine")
msg.append("""--- Scenario 7: 6-Tier Multi-Tenant Production QoS Hierarchy (10.0s Run, 8 Server Threads) ---
+--------------------------------------+--------------------+--------------------+--------------------+
| Tenant Tier Persona & SLA Parameters | throttler          | dmclock_coarse     | dmclock_fine       |
|                                      | Accepted / Drop %  | Accepted / Drop %  | Accepted / Drop %  |
+--------------------------------------+--------------------+--------------------+--------------------+""")
for t, c, f in zip(d7_t["tenants"], d7_c["tenants"], d7_f["tenants"]):
    name = t["name"]
    t_str = f"{t['accepted']} ({t['drop_rate_pct']:.1f}%)"
    c_str = f"{c['accepted']} ({c['drop_rate_pct']:.1f}%)"
    f_str = f"{f['accepted']} ({f['drop_rate_pct']:.1f}%)"
    msg.append(f"| {name:36s} | {t_str:18s} | {c_str:18s} | {f_str:18s} |")
msg.append("+--------------------------------------+--------------------+--------------------+--------------------+\n")

# Scenario 8
d8_sc, d8_ac = load("8_static_dmclock_coarse"), load("8_adaptive_dmclock_coarse")
d8_sf, d8_af = load("8_static_dmclock_fine"), load("8_adaptive_dmclock_fine")

msg.append("""--- Scenario 8: Closed-Loop Adaptive Capacity Tuning (12.0s Run, OSD Scrub Spikes) ---
+--------------------------------------+--------------------+--------------------+--------------------+--------------------+
| Tenant Tier Persona                  | Static coarse      | Adaptive coarse    | Static fine        | Adaptive fine      |
|                                      | Accepted / p50 ms  | Accepted / p50 ms  | Accepted / p50 ms  | Accepted / p50 ms  |
+--------------------------------------+--------------------+--------------------+--------------------+--------------------+""")
for sc, ac, sf, af in zip(d8_sc["tenants"], d8_ac["tenants"], d8_sf["tenants"], d8_af["tenants"]):
    name = sc["name"]
    sc_str = f"{sc['accepted']} / {sc['p50_ms']:.1f}ms"
    ac_str = f"{ac['accepted']} / {ac['p50_ms']:.1f}ms"
    sf_str = f"{sf['accepted']} / {sf['p50_ms']:.1f}ms"
    af_str = f"{af['accepted']} / {af['p50_ms']:.1f}ms"
    msg.append(f"| {name:36s} | {sc_str:18s} | {ac_str:18s} | {sf_str:18s} | {af_str:18s} |")
msg.append("+--------------------------------------+--------------------+--------------------+--------------------+--------------------+\n")

msg.append("Signed-off-by: Jasmin Bakamovic <jbakamovic@gmail.com>\n")

out_file = Path(__file__).parent.parent.resolve() / ".commit_msg_amended.txt"
with open(out_file, "w") as f:
    f.write("\n".join(msg))

print(f"[+] Wrote commit message to: {out_file}")
