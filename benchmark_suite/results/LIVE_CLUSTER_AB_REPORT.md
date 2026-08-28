# Live Ceph Cluster A/B Benchmark Report: Throttler vs dmClock

Executed on actual running Ceph cluster with real BlueStore OSD and Beast RGW.

### Workload 1: Noisy Neighbor Overload

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Latency-Sensitive App | 40.7 | 15.4ms | 6.8 | 419.4ms | 0.17x |
| Bulk Ingestion Bully | 874.1 | 14.8ms | 47.2 | 412.3ms | 0.05x |
| Cluster Health Monitor | 3.8 | 22.2ms | 1.3 | 483.0ms | 0.35x |

### Workload 2: 4-Tier Proportional Sharing

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Tier 1: Platinum VIP | 126.0 | 39.7ms | 52.1 | 32.8ms | 0.41x |
| Tier 2: Gold Standard | 90.1 | 35.0ms | 37.5 | 29.3ms | 0.42x |
| Tier 3: Silver Standard | 45.6 | 34.2ms | 18.7 | 29.6ms | 0.41x |
| Tier 4: Bronze Standard | 22.2 | 36.7ms | 9.2 | 30.7ms | 0.41x |
| System Health Monitor | 1.8 | 40.3ms | 0.7 | 66.8ms | 0.38x |

### Workload 3: Dynamic Tenant Churn

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Enterprise App | 1.5 | 1705.8ms | 51.6 | 17.0ms | 33.64x |
| System Health Monitor | 0.4 | 1402.5ms | 3.1 | 56.2ms | 8.49x |
| Dynamic Tenant 01 | 1.1 | 1122.6ms | 38.2 | 12.4ms | 34.87x |
| Dynamic Tenant 02 | 1.0 | 1213.3ms | 38.1 | 12.6ms | 37.24x |
| Dynamic Tenant 03 | 1.1 | 1112.5ms | 38.3 | 12.1ms | 34.98x |
| Dynamic Tenant 04 | 1.0 | 1478.6ms | 38.3 | 12.3ms | 37.48x |
| Dynamic Tenant 05 | 0.9 | 1110.8ms | 38.3 | 12.6ms | 43.72x |
| Dynamic Tenant 06 | 0.9 | 1342.9ms | 38.2 | 12.2ms | 40.23x |
| Dynamic Tenant 07 | 1.1 | 1110.0ms | 37.6 | 12.7ms | 34.30x |
| Dynamic Tenant 08 | 1.1 | 1108.5ms | 38.0 | 12.4ms | 34.64x |

