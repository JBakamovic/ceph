#!/usr/bin/env python3
"""
plot_cap_comparison.py

Generates a comparative visualization comparing:
1. Run 42 (Circuit Breaker max_inflight_per_pg = 32)
2. Run 44 (Circuit Breaker max_inflight_per_pg = 128)
3. Corresponding Baseline runs
"""

import os
import sys
import json
from pathlib import Path
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

def load_run(run_dir):
    data = {}
    for r in ['50r', '100r', '200r', '400r']:
        base_f = Path(run_dir) / f"congestion_results_baseline_{r}.json"
        cb_f = Path(run_dir) / f"congestion_results_circuit_breaker_{r}.json"
        data[r] = {}
        if base_f.exists():
            with open(base_f) as f:
                data[r]['baseline'] = json.load(f)
        if cb_f.exists():
            with open(cb_f) as f:
                data[r]['circuit_breaker'] = json.load(f)
    return data

def main():
    run42 = load_run("/home/ultron/development/42")
    run44 = load_run("/home/ultron/development/44")
    out_path = Path("/home/ultron/.gemini/antigravity/brain/2c4a0ed8-da03-4b4e-a19d-5c99b6115c1c/run44_vs_run42_comparison.png")

    rates = ['50r', '100r', '200r', '400r']
    rate_labels = ['50 req/s', '100 req/s', '200 req/s', '400 req/s']
    x = np.arange(len(rates))
    bar_width = 0.22

    # Extract Completed Ops
    cb42_comp = [sum(1 for rec in run42[r]['circuit_breaker']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]
    cb44_comp = [sum(1 for rec in run44[r]['circuit_breaker']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]
    base44_comp = [sum(1 for rec in run44[r]['baseline']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]

    # Extract Peak Queue Depth
    cb42_q = [max(s['rgw_perf']['rgw_qlen'] for s in run42[r]['circuit_breaker']['telemetry']['system_monitor']) for r in rates]
    cb44_q = [max(s['rgw_perf']['rgw_qlen'] for s in run44[r]['circuit_breaker']['telemetry']['system_monitor']) for r in rates]
    base44_q = [max(s['rgw_perf']['rgw_qlen'] for s in run44[r]['baseline']['telemetry']['system_monitor']) for r in rates]

    # Extract CB 44 disposition
    cb44_shed = [sum(1 for rec in run44[r]['circuit_breaker']['main_workload']['records'] if rec['status'] == 'shed') for r in rates]
    cb44_err = [sum(1 for rec in run44[r]['circuit_breaker']['main_workload']['records'] if rec['status'] in ('error', 'timed_out')) for r in rates]

    # Style
    plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')
    fig, axes = plt.subplots(2, 2, figsize=(16, 12), dpi=200)
    fig.patch.set_facecolor('#ffffff')

    palette = {
        'cb32': '#3b82f6',    # Blue
        'cb128': '#10b981',   # Emerald Green
        'base': '#ef4444',    # Coral Red
        'shed': '#f59e0b',    # Amber
        'err': '#94a3b8',     # Slate Gray
    }

    # -------------------------------------------------------------
    # Panel 1: Completed Operations (Yield)
    # -------------------------------------------------------------
    ax1 = axes[0, 0]
    b1 = ax1.bar(x - bar_width, cb42_comp, bar_width, label='Circuit Breaker (Cap=32, Run 42)', color=palette['cb32'], edgecolor='#1d4ed8', alpha=0.9)
    b2 = ax1.bar(x, cb44_comp, bar_width, label='Circuit Breaker (Cap=128, Run 44)', color=palette['cb128'], edgecolor='#047857', alpha=0.95)
    b3 = ax1.bar(x + bar_width, base44_comp, bar_width, label='Baseline (No Cap, Run 44)', color=palette['base'], edgecolor='#b91c1c', alpha=0.85)

    # Bar labels
    for bar in b1:
        h = bar.get_height()
        ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight='bold', color='#1e3a8a')
    for bar in b2:
        h = bar.get_height()
        ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight='bold', color='#065f46')
    for bar in b3:
        h = bar.get_height()
        ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight='bold', color='#991b1b')

    ax1.set_title("Completed Operations Yield (Prior-, During-, & Post-Stall)", fontsize=13, fontweight='bold', pad=12)
    ax1.set_xlabel("Open-Loop Arrival Rate", fontsize=11, fontweight='semibold')
    ax1.set_ylabel("Total Completed 200 OK Ops", fontsize=11, fontweight='semibold')
    ax1.set_xticks(x)
    ax1.set_xticklabels(rate_labels, fontsize=10)
    ax1.legend(loc='upper left', frameon=True, framealpha=0.95)
    ax1.set_ylim(0, max(max(base44_comp), max(cb44_comp)) * 1.25)
    ax1.axhline(32, color=palette['cb32'], linestyle='--', linewidth=1, alpha=0.6, label='Cap 32 Reference')
    ax1.axhline(128, color=palette['cb128'], linestyle='--', linewidth=1, alpha=0.6, label='Cap 128 Reference')

    # -------------------------------------------------------------
    # Panel 2: Peak RGW Request Queue Depth
    # -------------------------------------------------------------
    ax2 = axes[0, 1]
    q1 = ax2.bar(x - bar_width, cb42_q, bar_width, label='Circuit Breaker (Cap=32, Run 42)', color=palette['cb32'], edgecolor='#1d4ed8', alpha=0.9)
    q2 = ax2.bar(x, cb44_q, bar_width, label='Circuit Breaker (Cap=128, Run 44)', color=palette['cb128'], edgecolor='#047857', alpha=0.95)
    q3 = ax2.bar(x + bar_width, base44_q, bar_width, label='Baseline (No Cap, Run 44)', color=palette['base'], edgecolor='#b91c1c', alpha=0.85)

    for bar in q1:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight='bold', color='#1e3a8a')
    for bar in q2:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight='bold', color='#065f46')
    for bar in q3:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 3), textcoords="offset points", ha='center', va='bottom', fontsize=10, fontweight='bold', color='#991b1b')

    ax2.set_title("Peak RGW Request Queue Depth (qlen / qactive)", fontsize=13, fontweight='bold', pad=12)
    ax2.set_xlabel("Open-Loop Arrival Rate", fontsize=11, fontweight='semibold')
    ax2.set_ylabel("Max Queued / Active Requests", fontsize=11, fontweight='semibold')
    ax2.set_xticks(x)
    ax2.set_xticklabels(rate_labels, fontsize=10)
    ax2.legend(loc='upper right', frameon=True, framealpha=0.95)
    ax2.set_ylim(0, 800)

    # -------------------------------------------------------------
    # Panel 3: Run 44 Circuit Breaker Request Disposition
    # -------------------------------------------------------------
    ax3 = axes[1, 0]
    p_comp = ax3.bar(x, cb44_comp, 0.45, label='Completed (200 OK)', color=palette['cb128'], edgecolor='#047857', alpha=0.95)
    p_shed = ax3.bar(x, cb44_shed, 0.45, bottom=cb44_comp, label='Fast-Failed / Shed (503)', color=palette['shed'], edgecolor='#d97706', alpha=0.95)
    bottom_err = [cb44_comp[i] + cb44_shed[i] for i in range(len(rates))]
    p_err = ax3.bar(x, cb44_err, 0.45, bottom=bottom_err, label='Error / Timed Out (Aborted)', color=palette['err'], edgecolor='#64748b', alpha=0.8)

    for i in range(len(rates)):
        tot = cb44_comp[i] + cb44_shed[i] + cb44_err[i]
        ax3.annotate(f"Total: {tot}\n({cb44_comp[i]} ok, {cb44_shed[i]} shed)", xy=(x[i], tot), xytext=(0, 4), textcoords="offset points", ha='center', va='bottom', fontsize=9, fontweight='semibold')

    ax3.set_title("Run 44 Circuit Breaker (Cap=128) Request Disposition", fontsize=13, fontweight='bold', pad=12)
    ax3.set_xlabel("Open-Loop Arrival Rate", fontsize=11, fontweight='semibold')
    ax3.set_ylabel("Submitted Requests Breakdown", fontsize=11, fontweight='semibold')
    ax3.set_xticks(x)
    ax3.set_xticklabels(rate_labels, fontsize=10)
    ax3.legend(loc='upper left', frameon=True, framealpha=0.95)
    ax3.set_ylim(0, 750)

    # -------------------------------------------------------------
    # Panel 4: Latency Profiles Comparison (Completed vs Shed)
    # -------------------------------------------------------------
    ax4 = axes[1, 1]
    
    # 400r Latency distributions
    r = '400r'
    cb44_recs = run44[r]['circuit_breaker']['main_workload']['records']
    cb44_ok_lat = [rec['latency_ms'] / 1000.0 for rec in cb44_recs if rec['status'] == 'completed']
    cb44_shed_lat = [rec['latency_ms'] / 1000.0 for rec in cb44_recs if rec['status'] == 'shed']
    base44_recs = run44[r]['baseline']['main_workload']['records']
    base44_ok_lat = [rec['latency_ms'] / 1000.0 for rec in base44_recs if rec['status'] == 'completed']

    parts = ax4.boxplot([cb44_shed_lat, cb44_ok_lat, base44_ok_lat], 
                        tick_labels=['CB Shed\n(HTTP 503)', 'CB Completed\n(200 OK)', 'Baseline Completed\n(200 OK)'],
                        patch_artist=True, widths=0.5,
                        boxprops=dict(facecolor='#f8fafc', color='#334155'),
                        medianprops=dict(color='#0f172a', linewidth=2))

    box_colors = [palette['shed'], palette['cb128'], palette['base']]
    for patch, col in zip(parts['boxes'], box_colors):
        patch.set_facecolor(col)
        patch.set_alpha(0.75)

    ax4.set_title(f"Request Latency Comparison at 400 req/s Arrival Rate", fontsize=13, fontweight='bold', pad=12)
    ax4.set_ylabel("Latency (Seconds)", fontsize=11, fontweight='semibold')
    ax4.grid(True, linestyle=':', alpha=0.6)
    ax4.axhline(30.0, color='#dc2626', linestyle='--', linewidth=1.2, label='Client Socket Read Timeout (30s)')
    ax4.legend(loc='upper left', frameon=True, framealpha=0.95)

    plt.suptitle("Ceph RGW Circuit Breaker Capacity Analysis: Cap=32 (Run 42) vs Cap=128 (Run 44)\n1x RGW Instance | 4 MB Object Payload | 15s Culprit Fault (2.5s delay on OSD.2)", fontsize=15, fontweight='bold', y=0.99)
    plt.tight_layout(rect=[0, 0.02, 1, 0.96])
    plt.savefig(out_path, dpi=200, bbox_inches='tight')
    print(f"[+] Saved comparison plot to: {out_path}")

if __name__ == "__main__":
    main()
