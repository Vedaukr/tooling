#!/usr/bin/env python3
"""
chplot.py - charts from chbench output.

  python chplot.py results/raw_20260810-141500.csv --slo-ms 200

Produces, next to the input file:
  latency_by_query_<tag>.png   p50/p95/p99 per query, cold vs warm
  p99_vs_concurrency_<tag>.png p99 per concurrency level + SLO line
  qps_vs_concurrency_<tag>.png sustained throughput, error rate overlay
  scan_vs_latency_<tag>.png    rows scanned vs duration (index efficiency)

Deps: pandas, matplotlib
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402
import pandas as pd                      # noqa: E402


def p(series: pd.Series, q: float) -> float:
    return float(np.percentile(series.dropna(), q)) if len(series.dropna()) else np.nan


def load(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df = df[df["iteration"] != -1]
    return df


def chart_latency_by_query(df: pd.DataFrame, out: Path, tag: str) -> None:
    lat = df[(df["mode"] == "latency") & (df["ok"] == 1)]
    if lat.empty:
        return
    g = (lat.groupby(["query_class", "query_name", "cache"])["wall_ms"]
            .agg(p50=lambda s: p(s, 50), p95=lambda s: p(s, 95),
                 p99=lambda s: p(s, 99))
            .reset_index()
            .sort_values(["query_class", "query_name"]))

    labels = [f"{r.query_name}\n[{r.query_class}]" for r in
              g.drop_duplicates(["query_class", "query_name"]).itertuples()]
    names = list(g.drop_duplicates(["query_class", "query_name"])["query_name"])
    caches = sorted(g["cache"].unique())

    fig, axes = plt.subplots(len(caches), 1, figsize=(max(9, len(names) * 1.5),
                                                      4.2 * len(caches)),
                             squeeze=False)
    x = np.arange(len(names))
    width = 0.26
    for ax, cache in zip(axes[:, 0], caches):
        sub = g[g["cache"] == cache].set_index("query_name").reindex(names)
        for i, metric in enumerate(("p50", "p95", "p99")):
            ax.bar(x + (i - 1) * width, sub[metric], width, label=metric)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_ylabel("latency (ms)")
        ax.set_yscale("log")
        ax.set_title(f"Query latency - {cache} cache")
        ax.grid(axis="y", alpha=0.3)
        ax.legend()
    fig.tight_layout()
    fig.savefig(out / f"latency_by_query_{tag}.png", dpi=140)
    plt.close(fig)


def chart_p99_vs_concurrency(df: pd.DataFrame, out: Path, tag: str,
                             slo_ms: float | None) -> None:
    con = df[(df["mode"] == "concurrency") & (df["ok"] == 1)]
    if con.empty:
        return
    fig, ax = plt.subplots(figsize=(9, 5.5))
    for name, sub in con.groupby("query_name"):
        agg = (sub.groupby("concurrency")["wall_ms"]
                  .agg(lambda s: p(s, 99)).reset_index())
        ax.plot(agg["concurrency"], agg["wall_ms"], marker="o", label=name)
    overall = (con.groupby("concurrency")["wall_ms"]
                  .agg(lambda s: p(s, 99)).reset_index())
    ax.plot(overall["concurrency"], overall["wall_ms"], marker="s",
            linewidth=2.5, linestyle="--", color="black", label="all queries")
    if slo_ms:
        ax.axhline(slo_ms, linestyle=":", color="red", linewidth=2)
        ax.annotate(f"SLO {slo_ms:.0f} ms", (ax.get_xlim()[0], slo_ms),
                    textcoords="offset points", xytext=(5, 5), color="red")
    ax.set_xlabel("concurrent clients")
    ax.set_ylabel("p99 latency (ms)")
    ax.set_yscale("log")
    ax.set_title("p99 latency vs concurrency")
    ax.grid(alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / f"p99_vs_concurrency_{tag}.png", dpi=140)
    plt.close(fig)


def chart_qps_vs_concurrency(df: pd.DataFrame, out: Path, tag: str) -> None:
    con = df[df["mode"] == "concurrency"]
    if con.empty:
        return
    rows = []
    for level, sub in con.groupby("concurrency"):
        oks = sub[sub["ok"] == 1]
        busy_s = oks["wall_ms"].sum() / 1000 / level
        rows.append({
            "concurrency": level,
            "qps": len(oks) / busy_s if busy_s else 0,
            "err_pct": 100 * (len(sub) - len(oks)) / len(sub),
        })
    t = pd.DataFrame(rows).sort_values("concurrency")

    fig, ax = plt.subplots(figsize=(9, 5))
    ax.plot(t["concurrency"], t["qps"], marker="o", label="throughput")
    ax.set_xlabel("concurrent clients")
    ax.set_ylabel("queries / second")
    ax.grid(alpha=0.3)
    ax2 = ax.twinx()
    ax2.bar(t["concurrency"], t["err_pct"], alpha=0.25, width=0.6,
            color="red", label="error %")
    ax2.set_ylabel("error rate (%)")
    ax.set_title("Sustained throughput vs concurrency")
    fig.tight_layout()
    fig.savefig(out / f"qps_vs_concurrency_{tag}.png", dpi=140)
    plt.close(fig)


def chart_scan_vs_latency(df: pd.DataFrame, out: Path, tag: str) -> None:
    d = df[(df["ok"] == 1) & df["read_rows"].notna() & df["server_ms"].notna()]
    if d.empty:
        return
    fig, ax = plt.subplots(figsize=(9, 6))
    for name, sub in d.groupby("query_name"):
        ax.scatter(sub["read_rows"], sub["server_ms"], s=18, alpha=0.6, label=name)
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("rows read (server-side)")
    ax.set_ylabel("query duration (ms)")
    ax.set_title("Rows scanned vs duration - points far right have lost the index")
    ax.grid(alpha=0.3, which="both")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out / f"scan_vs_latency_{tag}.png", dpi=140)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("raw_csv", type=Path)
    ap.add_argument("--slo-ms", type=float, default=None)
    ap.add_argument("-o", "--outdir", type=Path, default=None)
    args = ap.parse_args()

    df = load(args.raw_csv)
    tag = args.raw_csv.stem.replace("raw_", "")
    out = args.outdir or args.raw_csv.parent
    out.mkdir(parents=True, exist_ok=True)

    chart_latency_by_query(df, out, tag)
    chart_p99_vs_concurrency(df, out, tag, args.slo_ms)
    chart_qps_vs_concurrency(df, out, tag)
    chart_scan_vs_latency(df, out, tag)
    print(f"charts written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
