# Statistically Significant Live Ceph Cluster Benchmark Report

Evaluation across 5 independent trials (4s each) with 2s warmup and 1s cooldown drainage.
Statistical Metrics: Sample Mean, 95% Confidence Interval (CI), Coefficient of Variation (CV%), and Welch's two-sample t-test p-values.

### Noisy Neighbor Overload

| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Latency-Sensitive App | 62.8 ± 1.0 | 26.4 ± 12.1 | 9794.9ms | 73163.6ms | 8.31 | 1.0851e-03 | ** |
| Bulk Ingestion Bully | 1907.1 ± 85.4 | 520.7 ± 494.7 | 10327.4ms | 17504.0ms | 7.67 | 1.2267e-03 | ** |
| Cluster Health Monitor | 4.6 ± 0.1 | 3.0 ± 0.4 | 10513.1ms | 106377.6ms | 10.04 | 4.4902e-04 | *** |

### Dynamic Tenant Churn

| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Enterprise App | 152.1 ± 1.7 | 70.2 ± 54.9 | 4286.5ms | 31784.6ms | 4.14 | 1.4285e-02 | * |
| System Health Monitor | 4.9 ± 0.0 | 3.6 ± 1.0 | 4129.3ms | 62923.4ms | 3.57 | 2.3419e-02 | * |
| Dynamic Tenant 01 | 127.9 ± 2.5 | 51.5 ± 47.9 | 4092.7ms | 19564.8ms | 4.42 | 1.1333e-02 | * |
| Dynamic Tenant 02 | 127.8 ± 2.2 | 51.8 ± 48.6 | 4095.9ms | 20232.0ms | 4.34 | 1.2143e-02 | * |
| Dynamic Tenant 03 | 128.0 ± 2.9 | 51.7 ± 48.1 | 4109.9ms | 20171.5ms | 4.40 | 1.1531e-02 | * |
| Dynamic Tenant 04 | 128.2 ± 2.4 | 51.8 ± 47.5 | 4059.0ms | 19303.5ms | 4.45 | 1.1093e-02 | * |
| Dynamic Tenant 05 | 128.1 ± 2.3 | 51.7 ± 47.8 | 4111.1ms | 18835.2ms | 4.43 | 1.1278e-02 | * |
| Dynamic Tenant 06 | 127.9 ± 2.3 | 51.5 ± 48.0 | 4085.9ms | 21303.8ms | 4.41 | 1.1506e-02 | * |
| Dynamic Tenant 07 | 127.7 ± 2.5 | 51.7 ± 48.2 | 4138.0ms | 20629.6ms | 4.38 | 1.1764e-02 | * |
| Dynamic Tenant 08 | 127.8 ± 2.5 | 51.7 ± 48.1 | 4155.4ms | 20170.3ms | 4.38 | 1.1718e-02 | * |

### 5-Tier SLA QoS

| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Tier 1: Platinum VIP | 208.4 ± 27.7 | 285.6 ± 280.8 | 71546.0ms | 64456.2ms | -0.76 | 4.8851e-01 | ns |
| Tier 2: Gold Standard | 150.2 ± 19.8 | 213.1 ± 212.3 | 66646.9ms | 57872.2ms | -0.82 | 4.5841e-01 | ns |
| Tier 3: Silver Standard | 76.8 ± 10.4 | 108.0 ± 108.3 | 63827.6ms | 55232.7ms | -0.80 | 4.6985e-01 | ns |
| Tier 4: Bronze Standard | 36.0 ± 5.8 | 49.5 ± 47.8 | 69785.9ms | 62302.7ms | -0.78 | 4.7860e-01 | ns |
| Tier 5: Free Bulk | 19.4 ± 2.4 | 26.6 ± 26.5 | 63759.7ms | 56325.2ms | -0.75 | 4.9290e-01 | ns |
| System Health Monitor | 3.8 ± 0.7 | 3.8 ± 1.8 | 107847.5ms | 138897.0ms | -0.06 | 9.5383e-01 | ns |

### Metadata Crawler vs Streamer

| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Metadata Crawler (Small Ops) | 391.5 ± 366.4 | 375.6 ± 364.7 | 13182.8ms | 15957.8ms | 0.09 | 9.3413e-01 | ns |
| Media Streamer (128KB Writes) | 189.5 ± 173.9 | 182.4 ± 172.3 | 13826.0ms | 16916.4ms | 0.08 | 9.3772e-01 | ns |
| Mobile Client (Interactive) | 13.7 ± 6.9 | 13.2 ± 7.1 | 56535.8ms | 66245.6ms | 0.14 | 8.9266e-01 | ns |

### Live OSD Scrub Congestion Spike

| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| VIP Latency-Sensitive App | 29.1 ± 15.7 | 33.0 ± 17.6 | 66342.4ms | 55299.8ms | -0.46 | 6.5614e-01 | ns |
| Bulk Ingestion Bully | 482.6 ± 533.4 | 634.3 ± 670.7 | 8702.2ms | 8369.9ms | -0.49 | 6.3702e-01 | ns |
| Cluster Health Monitor | 3.2 ± 0.8 | 3.3 ± 0.7 | 92503.4ms | 88891.7ms | -0.37 | 7.2192e-01 | ns |

### Line-Rate Disk Saturation

| Tenant Persona | Throttler Mean TPS (95% CI) | dmClock Mean TPS (95% CI) | Throttler p50 | dmClock p50 | Welch t-stat | p-value | Significance |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| Tenant A (Concurrent Flood) | 412.7 ± 366.7 | 298.1 ± 172.8 | 67230.8ms | 62891.5ms | 0.78 | 4.6394e-01 | ns |
| Tenant B (Concurrent Flood) | 443.3 ± 407.0 | 316.5 ± 194.6 | 63877.8ms | 59837.0ms | 0.78 | 4.6603e-01 | ns |
| Control Health Probe | 4.3 ± 2.0 | 3.7 ± 0.9 | 101044.9ms | 114838.2ms | 0.80 | 4.5849e-01 | ns |

