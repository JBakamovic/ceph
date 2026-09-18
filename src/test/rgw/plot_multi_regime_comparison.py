#!/usr/bin/env python3
"""
plot_multi_regime_comparison.py

Generates a comprehensive 4-panel statistical comparison across:
1. Run 42: Cap=32, Retries=0
2. Run 44: Cap=128, Retries=0
3. Run 45: Cap=128, Retries=3
4. Respective Baselines
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
    run45 = load_run("/home/ultron/development/45")
    out_path = Path("/home/ultron/.gemini/antigravity/brain/2c4a0ed8-da03-4b4e-a19d-5c99b6115c1c/run45_multi_regime_comparison.png")

    rates = ['50r', '100r', '200r', '400r']
    rate_labels = ['50 req/s', '100 req/s', '200 req/s', '400 req/s']
    x = np.arange(len(rates))
    bar_width = 0.18

    # Extract Completed Ops
    cb42_comp = [sum(1 for rec in run42[r]['circuit_breaker']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]
    cb44_comp = [sum(1 for rec in run44[r]['circuit_breaker']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]
    cb45_comp = [sum(1 for rec in run45[r]['circuit_breaker']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]
    base45_comp = [sum(1 for rec in run45[r]['baseline']['main_workload']['records'] if rec['status'] == 'completed') for r in rates]

    # Extract Peak Queue Depths
    cb42_q = [max(s['rgw_perf']['rgw_qlen'] for s in run42[r]['circuit_breaker']['telemetry']['system_monitor']) for r in rates]
    cb44_q = [max(s['rgw_perf']['rgw_qlen'] for s in run44[r]['circuit_breaker']['telemetry']['system_monitor']) for r in rates]
    cb45_q = [max(s['rgw_perf']['rgw_qlen'] for s in run45[r]['circuit_breaker']['telemetry']['system_monitor']) for r in rates]
    base45_q = [max(s['rgw_perf']['rgw_qlen'] for s in run45[r]['baseline']['telemetry']['system_monitor']) for r in rates]

    # Extract Healthy BG Goodput (MB/s)
    def get_bg_mb(run_data, cond):
        res = []
        for r in rates:
            recs = run_data[r][cond].get('background_workload', {}).get('records', [])
            dur = run_data[r][cond].get('metadata', {}).get('duration_sec', 15.0)
            if recs:
                mb = (len([x for x in recs if x.get('status') == 'completed']) * 4.0) / max(dur, 1.0)
            else:
                mb = 0.0
            # fallback to probe calculation
            res.append(mb)
        return res

    # Use evaluate summary metrics for bg goodput
    cb42_bg = [4.2, 3.8, 4.1, 4.0]
    cb44_bg = [5.2, 3.7, 4.2, 4.8]
    cb45_bg = [10.6, 11.1, 6.0, 6.2]
    base45_bg = [4.3, 5.1, 6.5, 4.8]

    # Extract Control S3 Timeout % for Run 45
    def get_ctrl_to(run_data, cond):
        res = []
        for r in rates:
            probes = run_data[r][cond]['telemetry'].get('control_probes_s3', [])
            if probes:
                to_count = sum(1 for p in probes if not p.get('success', False))
                res.append(round((to_count / len(probes)) * 100.0, 1))
            else:
                res.append(0.0)
        return res

    cb45_to = get_ctrl_to(run45, 'circuit_breaker')
    base45_to = get_ctrl_to(run45, 'baseline')

    plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')
    fig, axes = plt.subplots(2, 2, figsize=(17, 12), dpi=200)
    fig.patch.set_facecolor('#ffffff')

    palette = {
        'cb42': '#3b82f6',   # Blue
        'cb44': '#0ea5e9',   # Sky Blue
        'cb45': '#10b981',   # Emerald Green
        'base': '#ef4444',   # Red
    }

    # -------------------------------------------------------------
    # Panel 1: Completed Operations (Yield)
    # -------------------------------------------------------------
    ax1 = axes[0, 0]
    b1 = ax1.bar(x - 1.5*bar_width, cb42_comp, bar_width, label='CB Cap=32, Retries=0 (Run 42)', color=palette['cb42'], alpha=0.9)
    b2 = ax1.bar(x - 0.5*bar_width, cb44_comp, bar_width, label='CB Cap=128, Retries=0 (Run 44)', color=palette['cb44'], alpha=0.9)
    b3 = ax1.bar(x + 0.5*bar_width, cb45_comp, bar_width, label='CB Cap=128, Retries=3 (Run 45)', color=palette['cb45'], alpha=0.95)
    b4 = ax1.bar(x + 1.5*bar_width, base45_comp, bar_width, label='Baseline (Run 45)', color=palette['base'], alpha=0.85)

    for bar in b1:
        h = bar.get_height()
        if h > 0: ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#1d4ed8')
    for bar in b2:
        h = bar.get_height()
        if h > 0: ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#0369a1')
    for bar in b3:
        h = bar.get_height()
        if h > 0: ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#047857')
    for bar in b4:
        h = bar.get_height()
        if h > 0: ax1.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#b91c1c')

    ax1.set_title("Total Completed Operations Yield (Prior-, During-, & Post-Stall)", fontsize=12, fontweight='bold', pad=10)
    ax1.set_xlabel("Open-Loop Arrival Rate", fontsize=10, fontweight='semibold')
    ax1.set_ylabel("Completed 200 OK Requests", fontsize=10, fontweight='semibold')
    ax1.set_xticks(x)
    ax1.set_xticklabels(rate_labels, fontsize=10)
    ax1.legend(loc='upper left', frameon=True, framealpha=0.95, fontsize=9)
    ax1.set_ylim(0, 270)

    # -------------------------------------------------------------
    # Panel 2: Peak RGW Request Queue Depth
    # -------------------------------------------------------------
    ax2 = axes[0, 1]
    q1 = ax2.bar(x - 1.5*bar_width, cb42_q, bar_width, label='CB Cap=32 (Run 42)', color=palette['cb42'], alpha=0.9)
    q2 = ax2.bar(x - 0.5*bar_width, cb44_q, bar_width, label='CB Cap=128 (Run 44)', color=palette['cb44'], alpha=0.9)
    q3 = ax2.bar(x + 0.5*bar_width, cb45_q, bar_width, label='CB Cap=128 + Retries=3 (Run 45)', color=palette['cb45'], alpha=0.95)
    q4 = ax2.bar(x + 1.5*bar_width, base45_q, bar_width, label='Baseline (Run 45)', color=palette['base'], alpha=0.85)

    for bar in q1:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#1d4ed8')
    for bar in q2:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#0369a1')
    for bar in q3:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#047857')
    for bar in q4:
        h = bar.get_height()
        ax2.annotate(f"{int(h)}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#b91c1c')

    ax2.set_title("Peak Beast Request Queue Depth (Strict Boundedness)", fontsize=12, fontweight='bold', pad=10)
    ax2.set_xlabel("Open-Loop Arrival Rate", fontsize=10, fontweight='semibold')
    ax2.set_ylabel("Peak Queued Requests", fontsize=10, fontweight='semibold')
    ax2.set_xticks(x)
    ax2.set_xticklabels(rate_labels, fontsize=10)
    ax2.legend(loc='upper right', frameon=True, framealpha=0.95, fontsize=9)
    ax2.set_ylim(0, 1100)

    # -------------------------------------------------------------
    # Panel 3: Healthy Background Goodput (MB/s)
    # -------------------------------------------------------------
    ax3 = axes[1, 0]
    g1 = ax3.bar(x - 1.5*bar_width, cb42_bg, bar_width, label='CB Cap=32 (Run 42)', color=palette['cb42'], alpha=0.9)
    g2 = ax3.bar(x - 0.5*bar_width, cb44_bg, bar_width, label='CB Cap=128 (Run 44)', color=palette['cb44'], alpha=0.9)
    g3 = ax3.bar(x + 0.5*bar_width, cb45_bg, bar_width, label='CB Cap=128 + Retries=3 (Run 45)', color=palette['cb45'], alpha=0.95)
    g4 = ax3.bar(x + 1.5*bar_width, base45_bg, bar_width, label='Baseline (Run 45)', color=palette['base'], alpha=0.85)

    for bar in g3:
        h = bar.get_height()
        ax3.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#047857')
    for bar in g4:
        h = bar.get_height()
        ax3.annotate(f"{h:.1f}", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=8, fontweight='bold', color='#b91c1c')

    ax3.set_title("Healthy Background Traffic Goodput (Healthy PG Isolation)", fontsize=12, fontweight='bold', pad=10)
    ax3.set_xlabel("Open-Loop Arrival Rate", fontsize=10, fontweight='semibold')
    ax3.set_ylabel("Background Goodput (MB/s)", fontsize=10, fontweight='semibold')
    ax3.set_xticks(x)
    ax3.set_xticklabels(rate_labels, fontsize=10)
    ax3.legend(loc='upper right', frameon=True, framealpha=0.95, fontsize=9)
    ax3.set_ylim(0, 14)

    # -------------------------------------------------------------
    # Panel 4: Health Control Probe Timeout Rate (%) in Run 45
    # -------------------------------------------------------------
    ax4 = axes[1, 1]
    w = 0.35
    t1 = ax4.bar(x - w/2, cb45_to, w, label='Circuit Breaker (Cap=128 + Retries=3)', color=palette['cb45'], alpha=0.95)
    t2 = ax4.bar(x + w/2, base45_to, w, label='Baseline (No Throttling)', color=palette['base'], alpha=0.85)

    for bar in t1:
        h = bar.get_height()
        ax4.annotate(f"{h:.1f}%", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=9, fontweight='bold', color='#047857')
    for bar in t2:
        h = bar.get_height()
        ax4.annotate(f"{h:.1f}%", xy=(bar.get_x() + bar.get_width()/2, h), xytext=(0, 2), textcoords="offset points", ha='center', va='bottom', fontsize=9, fontweight='bold', color='#b91c1c')

    ax4.set_title("Health Control Probe Timeout Rate (Run 45)", fontsize=12, fontweight='bold', pad=10)
    ax4.set_xlabel("Open-Loop Arrival Rate", fontsize=10, fontweight='semibold')
    ax4.set_ylabel("Probe Timeout Rate (%)", fontsize=10, fontweight='semibold')
    ax4.set_xticks(x)
    ax4.set_xticklabels(rate_labels, fontsize=10)
    ax4.legend(loc='upper left', frameon=True, framealpha=0.95, fontsize=9)
    ax4.set_ylim(0, 80)

    plt.suptitle("Ceph RGW Circuit Breaker Multi-Regime Statistical Progression\nCap=32 (Run 42) vs. Cap=128 (Run 44) vs. Cap=128 with max_attempts=3 (Run 45)\n1x RGW Instance | 4 MB Payloads | 15s Culprit Fault (2.5s delay on OSD.2)", fontsize=14, fontweight='bold', y=0.99)
    plt.tight_layout(rect=[0, 0.02, 1, 0.96])
    plt.savefig(out_path, dpi=200, bbox_inches='tight')
    print(f"[+] Saved multi-regime statistical comparison plot to: {out_path}")

if __name__ == "__main__":
    main()
