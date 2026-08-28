# Live Ceph Cluster A/B Benchmark Report: Throttler vs Fine-Grained dmClock

Executed directly on the live running Ceph cluster (1 MON, 1 MGR, 1 BlueStore OSD, Beast RGW on port 8000).

### Workload 1: Noisy Neighbor Overload

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Latency-Sensitive App | 63.4 | 9.9ms | 36.8 | 12.4ms | 0.58x |
| Bulk Ingestion Bully | 1969.2 | 10.1ms | 925.7 | 11.1ms | 0.47x |
| Cluster Health Monitor | 4.7 | 10.7ms | 3.1 | 18.7ms | 0.67x |

### Workload 2: Metadata Crawler vs Media Streamer

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Metadata Crawler (Small Ops) | 1443.9 | 9.6ms | 281.0 | 16.5ms | 0.19x |
| Media Streamer (128KB Writes) | 691.0 | 9.7ms | 139.7 | 20.1ms | 0.20x |
| Mobile Client (Interactive) | 32.0 | 10.0ms | 14.0 | 89.1ms | 0.44x |

### Workload 3: 5-Tier Multi-Tenant SLA QoS

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Tier 1: Platinum VIP | 442.5 | 26.6ms | 181.6 | 79.1ms | 0.41x |
| Tier 2: Gold Standard | 324.4 | 22.5ms | 130.6 | 71.5ms | 0.40x |
| Tier 3: Silver Standard | 166.2 | 21.5ms | 67.1 | 55.3ms | 0.40x |
| Tier 4: Bronze Standard | 75.9 | 24.5ms | 32.3 | 74.9ms | 0.43x |
| Tier 5: Free Bulk | 51.5 | 16.3ms | 17.2 | 71.2ms | 0.33x |
| System Health Monitor | 5.2 | 47.3ms | 3.2 | 176.3ms | 0.62x |

### Workload 4: Dynamic Tenant Surplus Sharing & Churn

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Enterprise App | 53.0 | 41.2ms | 102.7 | 5.8ms | 1.94x |
| System Health Monitor | 3.2 | 74.5ms | 4.1 | 7.8ms | 1.28x |
| Dynamic Tenant 01 | 36.1 | 14.4ms | 79.2 | 6.0ms | 2.20x |
| Dynamic Tenant 02 | 36.1 | 13.5ms | 78.9 | 5.9ms | 2.19x |
| Dynamic Tenant 03 | 36.0 | 13.6ms | 79.1 | 5.8ms | 2.20x |
| Dynamic Tenant 04 | 36.2 | 9.1ms | 79.5 | 6.0ms | 2.19x |
| Dynamic Tenant 05 | 36.5 | 11.5ms | 79.5 | 6.0ms | 2.18x |
| Dynamic Tenant 06 | 36.0 | 10.6ms | 79.2 | 5.9ms | 2.20x |
| Dynamic Tenant 07 | 36.3 | 12.4ms | 79.2 | 5.9ms | 2.18x |
| Dynamic Tenant 08 | 36.5 | 10.4ms | 79.8 | 5.9ms | 2.19x |

### Workload 5: Intra-Class Data Starvation Isolation

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Tenant A (Bully Data Flood) | 1256.7 | 10.1ms | 273.0 | 40.5ms | 0.22x |
| Tenant B (Interactive Data VIP) | 81.2 | 9.9ms | 29.9 | 101.9ms | 0.37x |
| Tenant C (Health Probe) | 3.9 | 11.0ms | 2.8 | 144.3ms | 0.73x |

### Workload 6: Live OSD Scrub Congestion Spike

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Latency-Sensitive App | 23.8 | 92.4ms | 31.0 | 38.4ms | 1.30x |
| Bulk Ingestion Bully | 273.5 | 9.7ms | 375.7 | 16.4ms | 1.37x |
| Cluster Health Monitor | 2.9 | 107.8ms | 3.6 | 62.4ms | 1.22x |

### Workload 7: Line-Rate Disk Saturation (64 Workers)

| Tenant Persona | Throttler TPS | Throttler p50 | dmClock TPS | dmClock p50 | TPS Ratio |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Tenant A (Concurrent Flood) | 105.4 | 101.1ms | 273.5 | 31.2ms | 2.60x |
| Tenant B (Concurrent Flood) | 113.6 | 82.9ms | 285.7 | 28.2ms | 2.52x |
| Control Health Probe | 4.2 | 17.1ms | 2.9 | 7.5ms | 0.70x |

