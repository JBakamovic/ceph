#!/usr/bin/env python3
"""
sanitize_congestion_telemetry.py

Inspects and sanitizes Ceph RGW congestion experiment JSON files into a clean,
pure raw-measurements schema (v2.0).

Key Capabilities:
1. --inspect: Analyzes legacy files and reports exact byte bloat (e.g. raw_diagnostics text dumps,
   duplicate summary phases) vs raw telemetry streams without touching any files.
2. --dry-run: Simulates the sanitization and prints the clean v2.0 structure.
3. --output-dir <DIR>: Exports sanitized v2.0 files to an isolated directory, leaving original
   files completely unmodified and intact.
4. --in-place: Upgrades files in-place with automatic .bak preservation.
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path


def inspect_file(file_path):
    """Analyze file size distribution and return breakdown metrics."""
    p = Path(file_path)
    total_bytes = p.stat().st_size
    try:
        with open(p, "r") as f:
            data = json.load(f)
    except Exception as e:
        return {"file": p.name, "error": str(e), "total_bytes": total_bytes}

    diag_bytes = 0
    if "peak_snapshot" in data and isinstance(data["peak_snapshot"], dict):
        raw_diag = data["peak_snapshot"].get("raw_diagnostics")
        if raw_diag:
            diag_bytes = len(json.dumps(raw_diag))

    summary_bytes = 0
    for k in ("summary", "baseline", "congested", "recovered"):
        if k in data:
            summary_bytes += len(json.dumps(data[k]))

    raw_ts_bytes = 0
    if "raw_timeseries" in data:
        raw_ts_bytes += len(json.dumps(data["raw_timeseries"]))
    if "telemetry" in data:
        raw_ts_bytes += len(json.dumps(data["telemetry"]))

    bg_bytes = len(json.dumps(data.get("background_workload", {})))
    mw_bytes = len(json.dumps(data.get("main_workload", {})))
    cfg_bytes = len(json.dumps(data.get("config", {})))

    # Estimate sanitized size
    sanitized_bytes = total_bytes - diag_bytes - summary_bytes
    if sanitized_bytes < 0:
        sanitized_bytes = raw_ts_bytes + bg_bytes + mw_bytes + cfg_bytes

    reduction_pct = ((total_bytes - sanitized_bytes) / total_bytes * 100.0) if total_bytes > 0 else 0.0

    return {
        "file": p.name,
        "path": str(p),
        "total_bytes": total_bytes,
        "diag_bytes": diag_bytes,
        "summary_bytes": summary_bytes,
        "raw_bytes": raw_ts_bytes + bg_bytes + mw_bytes,
        "sanitized_bytes": sanitized_bytes,
        "reduction_pct": reduction_pct,
        "schema_version": data.get("schema_version", "1.0"),
    }


def convert_to_v2(data, source_path=None):
    """Convert legacy v1.0 JSON data to pure raw-measurements schema v2.0."""
    if data.get("schema_version") == "2.0":
        return data  # Already v2.0

    cfg = data.get("config", {})
    fn = Path(source_path).name.lower() if source_path else ""

    # Condition detection
    if "circuit_breaker" in fn:
        condition = "circuit_breaker"
    elif "baseline" in fn:
        condition = "baseline"
    else:
        condition = "unknown"

    # Extract raw timeseries
    raw_ts = data.get("raw_timeseries", {})
    probe_s3 = raw_ts.get("probe_s3", [])
    probe_http = raw_ts.get("probe_http", [])
    mon_samples = raw_ts.get("monitor_samples", [])

    # Clean system monitor samples: ensure numeric OSD ops and wire BW are clean
    clean_monitor = []
    for s in mon_samples:
        rx = s.get("nic", {}).get("rx_mb_s", 0.0) if "nic" in s else 0.0
        tx = s.get("nic", {}).get("tx_mb_s", 0.0) if "nic" in s else 0.0
        wire_mb = max(rx, tx)
        clean_s = {
            "timestamp": s.get("timestamp"),
            "rgw_qactive": s.get("rgw_perf", {}).get("rgw_qactive", 0),
            "rgw_qlen": s.get("rgw_perf", {}).get("rgw_qlen", 0),
            "rgw_requests_total": s.get("rgw_perf", {}).get("rgw_req", 0),
            "osd_ops_in_flight": s.get("osd_ops_in_flight", 0),
            "tcp_established": s.get("established_conns", 0),
            "objecter_inflight": s.get("inflight_objecter_requests", 0),
            "wire_rx_mb_s": rx,
            "wire_tx_mb_s": tx,
            "wire_mb_s": wire_mb,
            "stuck_pgs": s.get("stuck_pgs", {}),
            "by_gateway": s.get("rgw_perf", {}).get("by_gateway", {}),
        }
        clean_monitor.append(clean_s)

    # Determine timeline boundaries
    all_stamps = []
    for r in probe_s3:
        if "timestamp" in r:
            all_stamps.append(r["timestamp"])
    for m in mon_samples:
        if "timestamp" in m:
            all_stamps.append(m["timestamp"])
    t_start = min(all_stamps) if all_stamps else 0.0
    t_end = max(all_stamps) if all_stamps else 0.0
    duration_sec = round(t_end - t_start, 2) if (t_end and t_start) else cfg.get("duration", 15)

    # Background Workload
    bg = data.get("background_workload", {})
    bg_records = bg.get("raw_records", [])

    # Main Workload
    mw = data.get("main_workload", {})
    mw_records = mw.get("records", [])  # May be empty in legacy files

    v2_payload = {
        "schema_version": "2.0",
        "metadata": {
            "condition": condition,
            "start_time": t_start,
            "end_time": t_end,
            "duration_sec": duration_sec,
            "mode": cfg.get("mode", "inject_delay"),
            "delay_sec": cfg.get("delay", 2.5),
            "rate": cfg.get("rate", 0.0),
            "concurrency": cfg.get("concurrency", 40),
            "payload_size_bytes": cfg.get("payload_size", 1048576),
            "culprit_payload_size_bytes": cfg.get("culprit_payload_size", cfg.get("payload_size", 1048576)),
            "background_payload_size_bytes": cfg.get("background_payload_size", cfg.get("payload_size", 1048576)),
            "endpoints": cfg.get("endpoints", "").split(",") if isinstance(cfg.get("endpoints"), str) else cfg.get("endpoints", []),
            "config": cfg,
        },
        "main_workload": {
            "mode": mw.get("mode", "open_loop" if cfg.get("rate", 0) > 0 else "closed_loop"),
            "rate_target": mw.get("rate_target", cfg.get("rate", 0.0)),
            "concurrency": mw.get("concurrency", cfg.get("concurrency", 40)),
            "client_timeout": mw.get("client_timeout", cfg.get("client_timeout")),
            "payload_size": cfg.get("culprit_payload_size", cfg.get("payload_size", 1048576)),
            "summary": {k: v for k, v in mw.items() if k != "records"} if mw else {},
            "records": mw_records,
        },
        "background_workload": {
            "concurrency": cfg.get("background_workers", 0),
            "payload_size": cfg.get("background_payload_size", cfg.get("payload_size", 1048576)),
            "records": bg_records,
        },
        "telemetry": {
            "control_probes_s3": probe_s3,
            "control_probes_http": probe_http,
            "system_monitor": clean_monitor,
        },
    }

    return v2_payload


def resolve_files(targets):
    """Resolve CLI target arguments into file paths."""
    resolved = []
    for t in targets:
        p = Path(t)
        if p.is_file():
            resolved.append(p)
        elif p.is_dir():
            resolved.extend(sorted(p.glob("congestion_results_*.json")))
        else:
            # Check glob or development subfolder
            matched = [Path(f) for f in glob.glob(t)]
            if not matched:
                dev_cand = Path(f"/home/ultron/development/{t}")
                if dev_cand.is_dir():
                    matched = sorted(dev_cand.glob("congestion_results_*.json"))
                else:
                    matched = [Path(f) for f in glob.glob(f"/home/ultron/development/{t}/**/congestion_results_*.json", recursive=True)]
            resolved.extend(matched)
    return sorted(list(set(resolved)))


def main():
    parser = argparse.ArgumentParser(
        description="Inspect and sanitize Ceph RGW congestion experiment JSON files to clean raw-measurements schema v2.0."
    )
    parser.add_argument("targets", nargs="+", help="File paths, directories, or patterns (e.g. 30_1, '26*', '/home/ultron/development/30_1/*.json')")
    parser.add_argument("--inspect", action="store_true", help="Inspect and display bloat breakdown table without modifying files")
    parser.add_argument("--dry-run", action="store_true", help="Print converted v2.0 JSON structure for first matching file without saving")
    parser.add_argument("--output-dir", default="", help="Export sanitized v2.0 files to this directory (preserves originals intact)")
    parser.add_argument("--in-place", action="store_true", help="Sanitize files in-place (creates .bak backups)")

    args = parser.parse_args()
    files = resolve_files(args.targets)

    if not files:
        print(f"[!] No matching JSON files found for targets: {args.targets}")
        sys.exit(1)

    print(f"[*] Found {len(files)} experiment JSON file(s).")

    if args.inspect or (not args.dry_run and not args.output_dir and not args.in_place):
        print("\n" + "=" * 115)
        print("  CEPH RGW TELEMETRY JSON FORENSIC INSPECTION REPORT")
        print("=" * 115)
        header = f"| {'File Name':<36} | {'Total Size':<11} | {'Raw Diag Bloat':<16} | {'Summary Bloat':<14} | {'Sanitized Size':<15} | {'Reduction':<9} |"
        sep = "+" + "-" * 38 + "+" + "-" * 13 + "+" + "-" * 18 + "+" + "-" * 16 + "+" + "-" * 17 + "+" + "-" * 11 + "+"
        print(sep)
        print(header)
        print(sep)

        tot_orig = 0
        tot_diag = 0
        tot_sum = 0
        tot_clean = 0

        for f in files:
            res = inspect_file(f)
            if "error" in res:
                print(f"| {res['file'][:36]:<36} | ERROR: {res['error'][:60]} |")
                continue
            tot_orig += res["total_bytes"]
            tot_diag += res["diag_bytes"]
            tot_sum += res["summary_bytes"]
            tot_clean += res["sanitized_bytes"]

            orig_str = f"{res['total_bytes']/1024:>7.1f} KB"
            diag_str = f"{res['diag_bytes']/1024:>8.1f} KB"
            sum_str = f"{res['summary_bytes']/1024:>7.1f} KB"
            clean_str = f"{res['sanitized_bytes']/1024:>8.1f} KB"
            red_str = f"{res['reduction_pct']:>6.1f} %"

            print(f"| {res['file'][:36]:<36} | {orig_str:<11} | {diag_str:<16} | {sum_str:<14} | {clean_str:<15} | {red_str:<9} |")

        print(sep)
        overall_red = ((tot_orig - tot_clean) / tot_orig * 100.0) if tot_orig > 0 else 0.0
        tot_orig_mb = f"{tot_orig / (1024*1024):>7.1f} MB"
        tot_diag_mb = f"{tot_diag / (1024*1024):>7.1f} MB"
        tot_sum_mb = f"{tot_sum / (1024*1024):>7.1f} MB"
        tot_clean_mb = f"{tot_clean / (1024*1024):>7.1f} MB"
        tot_red_str = f"{overall_red:>6.1f} %"
        print(f"| {'TOTAL AGGREGATED':<36} | {tot_orig_mb:<11} | {tot_diag_mb:<16} | {tot_sum_mb:<14} | {tot_clean_mb:<15} | {tot_red_str:<9} |")
        print(sep)
        print(f"\n[+] Inspection complete across {len(files)} files.")
        if not args.output_dir and not args.in_place and not args.dry_run:
            print("[i] To export sanitized copies without touching originals: pass --output-dir <DIR>")
            print("[i] To preview a sanitized JSON structure:                pass --dry-run")
        return

    if args.dry_run:
        sample_file = files[0]
        print(f"\n[*] Generating dry-run sanitized v2.0 structure for: {sample_file}")
        with open(sample_file) as f:
            d = json.load(f)
        v2 = convert_to_v2(d, sample_file)

        # Print skeleton structure
        print("\n--- Schema v2.0 Top-Level Keys ---")
        for k, v in v2.items():
            if isinstance(v, dict):
                print(f"  {k}: dict with keys {list(v.keys())}")
            elif isinstance(v, list):
                print(f"  {k}: list with {len(v)} items")
            else:
                print(f"  {k}: {v}")

        print("\n--- Sample System Monitor Telemetry Record (v2.0) ---")
        if v2.get("telemetry", {}).get("system_monitor"):
            print(json.dumps(v2["telemetry"]["system_monitor"][0], indent=2))

        print("\n--- Sample Control S3 Probe Record (v2.0) ---")
        if v2.get("telemetry", {}).get("control_probes_s3"):
            print(json.dumps(v2["telemetry"]["control_probes_s3"][0], indent=2))

        print(f"\n[+] Dry-run complete. Original file left untouched: {sample_file}")
        return

    if args.output_dir:
        out_p = Path(args.output_dir)
        out_p.mkdir(parents=True, exist_ok=True)
        print(f"\n[*] Exporting {len(files)} sanitized v2.0 files to: {out_p}")
        for f in files:
            with open(f) as fp:
                d = json.load(fp)
            v2 = convert_to_v2(d, f)
            dest = out_p / f.name
            with open(dest, "w") as fp:
                json.dump(v2, fp, indent=2, default=str)
            print(f"  [+] Wrote: {dest} ({dest.stat().st_size / 1024:.1f} KB)")
        print(f"\n[+] All {len(files)} files successfully exported to {out_p}. Originals preserved.")
        return

    if args.in_place:
        print(f"\n[*] Sanitizing {len(files)} files in-place with .bak preservation...")
        for f in files:
            bak = f.with_suffix(".json.bak")
            if not bak.exists():
                f.rename(bak)
                src = bak
            else:
                src = f
            with open(src) as fp:
                d = json.load(fp)
            v2 = convert_to_v2(d, f)
            with open(f, "w") as fp:
                json.dump(v2, fp, indent=2, default=str)
            print(f"  [+] Sanitized: {f} (backup: {bak.name})")
        print(f"\n[+] In-place sanitization completed for {len(files)} files.")


if __name__ == "__main__":
    main()
