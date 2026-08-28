# Live Ceph Cluster A/B Benchmark Report: Throttler vs Fine-Grained dmClock

Executed directly on the live running Ceph cluster (1 MON, 1 MGR, 1 BlueStore OSD, Beast RGW).

### Workload 1: Noisy Neighbor Overload

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Latency-Sensitive App | 33.0 | 17.2ms | 43.6 | 10.5ms | 1.32x |
| Bulk Ingestion Bully | 621.9 | 17.7ms | 1194.5 | 10.0ms | 1.92x |
| Cluster Health Monitor | 3.1 | 21.7ms | 3.7 | 13.0ms | 1.21x |

### Workload 2: 4-Tier Proportional Sharing

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Tier 1: Platinum VIP | 16.6 | 1299.8ms | 175.8 | 87.4ms | 10.56x |
| Tier 2: Gold Standard | 11.1 | 1434.0ms | 128.6 | 83.3ms | 11.59x |
| Tier 3: Silver Standard | 5.5 | 1433.9ms | 64.0 | 84.6ms | 11.55x |
| Tier 4: Bronze Standard | 2.8 | 1434.8ms | 31.3 | 85.8ms | 11.30x |
| System Health Monitor | 0.7 | 1450.2ms | 3.2 | 171.4ms | 4.61x |

### Workload 3: Dynamic Tenant Churn

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Enterprise App | 61.3 | 15.4ms | 41.0 | 71.4ms | 0.67x |
| System Health Monitor | 3.4 | 21.6ms | 3.3 | 76.2ms | 0.96x |
| Dynamic Tenant 01 | 40.3 | 15.5ms | 27.5 | 56.9ms | 0.68x |
| Dynamic Tenant 02 | 39.8 | 15.9ms | 27.3 | 54.2ms | 0.69x |
| Dynamic Tenant 03 | 40.1 | 15.9ms | 27.4 | 53.8ms | 0.68x |
| Dynamic Tenant 04 | 40.5 | 15.3ms | 27.5 | 54.4ms | 0.68x |
| Dynamic Tenant 05 | 40.6 | 15.4ms | 27.5 | 53.6ms | 0.68x |
| Dynamic Tenant 06 | 40.0 | 15.4ms | 27.4 | 55.6ms | 0.69x |
| Dynamic Tenant 07 | 40.1 | 15.6ms | 27.5 | 53.9ms | 0.69x |
| Dynamic Tenant 08 | 40.5 | 15.2ms | 27.4 | 58.1ms | 0.68x |

### Workload 4: Metadata Crawler vs Media Streamer

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Metadata Crawler (Small Ops) | 348.2 | 18.5ms | 559.5 | 10.8ms | 1.61x |
| Media Streamer (128KB Writes) | 179.4 | 17.6ms | 272.3 | 11.0ms | 1.52x |
| Mobile Client (Interactive) | 15.9 | 20.7ms | 17.8 | 21.1ms | 1.12x |

### Workload 5: Intra-Class Data Starvation Isolation

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Tenant A (Bully Data Flood) | 12.2 | 2399.1ms | 326.4 | 38.6ms | 26.76x |
| Tenant B (Interactive Data VIP) | 2.0 | 2385.4ms | 34.6 | 78.5ms | 17.04x |
| Tenant C (Health Probe) | 0.4 | 2387.4ms | 3.0 | 104.7ms | 7.28x |

