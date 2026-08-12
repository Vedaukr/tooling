#!/usr/bin/env python3
"""
chbench.py - isolated ClickHouse query benchmark.

Modes
  latency      sequential repetitions per query, cold and/or warm cache
  concurrency  N concurrent workers replaying a weighted query mix
  explain      dump EXPLAIN indexes=1 / EXPLAIN ESTIMATE per query

Client wall time is measured around the HTTP request with the response body
streamed and discarded. Everything server-side (rows scanned, marks, memory,
CPU) is pulled afterwards from system.query_log, joined on an explicit query_id.

Deps: requests, PyYAML
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import re
import string
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
import yaml

FORMAT_TAIL = re.compile(r"\bFORMAT\s+\w+\s*;?\s*$", re.IGNORECASE)

FIELDNAMES = [
    "run_tag", "ts", "mode", "query_name", "query_class", "cache",
    "concurrency", "iteration", "ok", "error",
    "wall_ms", "bytes_received", "query_id",
    # from system.query_log
    "server_ms", "read_rows", "read_bytes", "result_rows", "memory_bytes",
    "cpu_us", "selected_parts", "selected_ranges", "selected_marks",
    "mark_cache_hits", "mark_cache_misses", "os_read_bytes",
]


class CHError(RuntimeError):
    pass


class CHClient:
    """Thin HTTP client. One requests.Session per thread."""

    def __init__(self, url: str, user: str, password: str, database: str,
                 timeout: int = 300, verify: bool = True):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.database = database
        self.timeout = timeout
        self.verify = verify
        self._local = threading.local()

    @property
    def session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({
                "X-ClickHouse-User": self.user,
                "X-ClickHouse-Key": self.password,
            })
            self._local.session = s
        return s

    def execute(self, sql: str, query_id: str | None = None,
                settings: dict[str, Any] | None = None,
                discard_result: bool = True) -> tuple[float, int]:
        """Run sql. Returns (wall_seconds, bytes_received)."""
        params: dict[str, Any] = {"database": self.database}
        if query_id:
            params["query_id"] = query_id
        for k, v in (settings or {}).items():
            params[k] = int(v) if isinstance(v, bool) else v

        body = sql.rstrip().rstrip(";")
        if discard_result and not FORMAT_TAIL.search(body):
            body += "\nFORMAT Null"

        t0 = time.perf_counter()
        resp = self.session.post(
            self.url, params=params, data=body.encode("utf-8"),
            timeout=self.timeout, stream=True, verify=self.verify,
        )
        nbytes = 0
        chunks: list[bytes] = []
        for chunk in resp.iter_content(chunk_size=1 << 16):
            nbytes += len(chunk)
            if len(chunks) < 16:          # keep a head for error reporting
                chunks.append(chunk)
        wall = time.perf_counter() - t0

        # ClickHouse reports errors either via status code, or - if the error
        # happened after headers were flushed - via a trailer header + text
        # appended to a 200 body.
        if resp.status_code != 200 or resp.headers.get("X-ClickHouse-Exception-Code"):
            head = b"".join(chunks)[:2000].decode("utf-8", "replace")
            raise CHError(f"HTTP {resp.status_code}: {head}")
        return wall, nbytes

    def fetch_json_each_row(self, sql: str) -> list[dict]:
        params = {"database": self.database, "default_format": "JSONEachRow"}
        resp = self.session.post(self.url, params=params,
                                 data=sql.encode("utf-8"),
                                 timeout=self.timeout, verify=self.verify)
        if resp.status_code != 200:
            raise CHError(f"HTTP {resp.status_code}: {resp.text[:2000]}")
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]

    def fetch_text(self, sql: str) -> str:
        resp = self.session.post(self.url, params={"database": self.database},
                                 data=sql.encode("utf-8"),
                                 timeout=self.timeout, verify=self.verify)
        if resp.status_code != 200:
            raise CHError(f"HTTP {resp.status_code}: {resp.text[:2000]}")
        return resp.text


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

class Query:
    def __init__(self, d: dict, defaults: dict):
        self.name: str = d["name"]
        self.cls: str = d.get("class", "unclassified")
        self.sql: str = d["sql"]
        self.weight: float = float(d.get("weight", 1.0))
        self.runs: int = int(d.get("runs", defaults.get("runs", 10)))
        self.params: list[dict] = d.get("params", [{}])
        self.settings: dict = {**defaults.get("settings", {}), **d.get("settings", {})}
        self.discard_result: bool = bool(
            d.get("discard_result", defaults.get("discard_result", True)))

    def render(self, rng: random.Random) -> str:
        p = rng.choice(self.params) if self.params else {}
        if not p:
            return self.sql
        return string.Template(self.sql).safe_substitute(p)


def load_config(path: Path) -> tuple[CHClient, dict, list[Query]]:
    cfg = yaml.safe_load(path.read_text())
    conn = cfg["connection"]
    client = CHClient(
        url=conn["url"],
        user=conn.get("user", "default"),
        password=conn.get("password", ""),
        database=conn.get("database", "default"),
        timeout=int(conn.get("timeout_s", 300)),
        verify=bool(conn.get("verify_tls", True)),
    )
    defaults = cfg.get("defaults", {})
    queries = [Query(q, defaults) for q in cfg["queries"]]
    return client, cfg, queries


# --------------------------------------------------------------------------
# execution
# --------------------------------------------------------------------------

def drop_caches(client: CHClient, statements: list[str]) -> None:
    for st in statements:
        try:
            client.execute(st, discard_result=True)
        except CHError as e:
            print(f"  ! cache drop failed ({st}): {e}", file=sys.stderr)


def run_once(client: CHClient, q: Query, rng: random.Random, run_tag: str,
             mode: str, cache: str, concurrency: int, iteration: int) -> dict:
    qid = f"chbench-{run_tag}-{uuid.uuid4().hex[:16]}"
    rec = {k: None for k in FIELDNAMES}
    rec.update({
        "run_tag": run_tag,
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "mode": mode, "query_name": q.name, "query_class": q.cls,
        "cache": cache, "concurrency": concurrency, "iteration": iteration,
        "query_id": qid,
    })
    sql = q.render(rng)
    try:
        wall, nbytes = client.execute(sql, query_id=qid, settings=q.settings,
                                      discard_result=q.discard_result)
        rec["ok"] = 1
        rec["wall_ms"] = round(wall * 1000, 3)
        rec["bytes_received"] = nbytes
    except Exception as e:                                  # noqa: BLE001
        rec["ok"] = 0
        rec["error"] = str(e)[:500].replace("\n", " ")
    return rec


def latency_mode(client: CHClient, cfg: dict, queries: list[Query],
                 run_tag: str, caches: list[str], warmup: int) -> list[dict]:
    drops = cfg.get("cold_cache_statements", [
        "SYSTEM DROP MARK CACHE",
        "SYSTEM DROP UNCOMPRESSED CACHE",
    ])
    out: list[dict] = []
    rng = random.Random(42)
    for q in queries:
        for cache in caches:
            print(f"[latency] {q.name} ({q.cls}) cache={cache} runs={q.runs}")
            if cache == "warm":
                for _ in range(warmup):
                    run_once(client, q, rng, run_tag, "latency", cache, 1, -1)
            for i in range(q.runs):
                if cache == "cold":
                    drop_caches(client, drops)
                rec = run_once(client, q, rng, run_tag, "latency", cache, 1, i)
                out.append(rec)
                flag = "" if rec["ok"] else f"  ERROR: {rec['error'][:120]}"
                print(f"    #{i:<3} {rec['wall_ms'] or -1:>9.1f} ms{flag}")
    return out


def pick_weighted(queries: list[Query], rng: random.Random) -> Query:
    total = sum(q.weight for q in queries)
    r = rng.random() * total
    acc = 0.0
    for q in queries:
        acc += q.weight
        if r <= acc:
            return q
    return queries[-1]


def concurrency_mode(client: CHClient, queries: list[Query], run_tag: str,
                     levels: list[int], duration: float,
                     warmup: int) -> list[dict]:
    out: list[dict] = []
    lock = threading.Lock()

    rng0 = random.Random(7)
    for _ in range(warmup):
        for q in queries:
            run_once(client, q, rng0, run_tag, "concurrency", "warm", 0, -1)

    for level in levels:
        print(f"[concurrency] level={level} duration={duration}s")
        stop_at = time.perf_counter() + duration
        counter = {"n": 0}

        def worker(wid: int) -> None:
            rng = random.Random(1000 + wid)
            local: list[dict] = []
            i = 0
            while time.perf_counter() < stop_at:
                q = pick_weighted(queries, rng)
                local.append(run_once(client, q, rng, run_tag, "concurrency",
                                      "warm", level, i))
                i += 1
            with lock:
                out.extend(local)
                counter["n"] += len(local)

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=level) as ex:
            list(ex.map(worker, range(level)))
        elapsed = time.perf_counter() - t0
        print(f"    {counter['n']} requests in {elapsed:.1f}s "
              f"= {counter['n'] / elapsed:.1f} qps")
    return out


def explain_mode(client: CHClient, queries: list[Query], outdir: Path,
                 run_tag: str) -> None:
    rng = random.Random(1)
    path = outdir / f"explain_{run_tag}.txt"
    with path.open("w") as fh:
        for q in queries:
            sql = q.render(rng)
            fh.write(f"{'=' * 78}\n{q.name}  [{q.cls}]\n{'=' * 78}\n")
            for label, prefix in (("EXPLAIN indexes = 1", "EXPLAIN indexes = 1"),
                                  ("EXPLAIN ESTIMATE", "EXPLAIN ESTIMATE")):
                fh.write(f"\n--- {label} ---\n")
                try:
                    fh.write(client.fetch_text(f"{prefix}\n{sql}"))
                except CHError as e:
                    fh.write(f"FAILED: {e}\n")
            fh.write("\n\n")
    print(f"wrote {path}")


# --------------------------------------------------------------------------
# server-side metrics
# --------------------------------------------------------------------------

QUERY_LOG_SQL = """
SELECT
    query_id,
    query_duration_ms                                  AS server_ms,
    read_rows, read_bytes, result_rows,
    memory_usage                                       AS memory_bytes,
    ProfileEvents['OSCPUVirtualTimeMicroseconds']      AS cpu_us,
    ProfileEvents['SelectedParts']                     AS selected_parts,
    ProfileEvents['SelectedRanges']                    AS selected_ranges,
    ProfileEvents['SelectedMarks']                     AS selected_marks,
    ProfileEvents['MarkCacheHits']                     AS mark_cache_hits,
    ProfileEvents['MarkCacheMisses']                   AS mark_cache_misses,
    ProfileEvents['OSReadBytes']                       AS os_read_bytes
FROM system.query_log
WHERE type = 'QueryFinish'
  AND event_date >= today() - 1
  AND query_id IN ({ids})
"""


def enrich_from_query_log(client: CHClient, records: list[dict],
                          attempts: int = 4, sleep_s: float = 3.0) -> None:
    wanted = {r["query_id"] for r in records if r["ok"]}
    found: dict[str, dict] = {}
    for attempt in range(attempts):
        missing = sorted(wanted - found.keys())
        if not missing:
            break
        try:
            client.execute("SYSTEM FLUSH LOGS", discard_result=True)
        except CHError as e:
            print(f"  ! SYSTEM FLUSH LOGS failed: {e}", file=sys.stderr)
        for i in range(0, len(missing), 400):
            batch = missing[i:i + 400]
            ids = ",".join("'" + b.replace("'", "") + "'" for b in batch)
            try:
                for row in client.fetch_json_each_row(QUERY_LOG_SQL.format(ids=ids)):
                    found[row["query_id"]] = row
            except CHError as e:
                print(f"  ! query_log read failed: {e}", file=sys.stderr)
                return
        if wanted - found.keys():
            time.sleep(sleep_s)
    for r in records:
        row = found.get(r["query_id"])
        if not row:
            continue
        for k, v in row.items():
            if k in FIELDNAMES and k != "query_id":
                r[k] = int(v) if isinstance(v, str) and v.isdigit() else v
    print(f"query_log: matched {len(found)}/{len(wanted)}")


# --------------------------------------------------------------------------
# output
# --------------------------------------------------------------------------

def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = max(0, min(len(s) - 1, math.ceil(p / 100 * len(s)) - 1))
    return s[k]


def write_raw(records: list[dict], path: Path) -> None:
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDNAMES, extrasaction="ignore")
        w.writeheader()
        w.writerows(records)
    print(f"wrote {path} ({len(records)} rows)")


def summarize(records: list[dict], path: Path) -> None:
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        if r["iteration"] == -1:
            continue
        groups.setdefault(
            (r["mode"], r["query_name"], r["query_class"], r["cache"],
             r["concurrency"]), []).append(r)

    rows = []
    for (mode, name, cls, cache, conc), rs in sorted(groups.items()):
        oks = [r for r in rs if r["ok"]]
        walls = [r["wall_ms"] for r in oks]
        rows.append({
            "mode": mode, "query_name": name, "query_class": cls,
            "cache": cache, "concurrency": conc,
            "n": len(rs), "errors": len(rs) - len(oks),
            "p50_ms": round(pct(walls, 50), 1),
            "p95_ms": round(pct(walls, 95), 1),
            "p99_ms": round(pct(walls, 99), 1),
            "max_ms": round(max(walls), 1) if walls else None,
            "mean_ms": round(sum(walls) / len(walls), 1) if walls else None,
            "server_p99_ms": round(pct([r["server_ms"] for r in oks
                                        if r["server_ms"] is not None], 99), 1),
            "read_rows_med": pct([r["read_rows"] for r in oks
                                  if r["read_rows"] is not None], 50),
            "read_bytes_med": pct([r["read_bytes"] for r in oks
                                   if r["read_bytes"] is not None], 50),
            "memory_p99": pct([r["memory_bytes"] for r in oks
                               if r["memory_bytes"] is not None], 99),
            "selected_parts_med": pct([r["selected_parts"] for r in oks
                                       if r["selected_parts"] is not None], 50),
            "selected_marks_med": pct([r["selected_marks"] for r in oks
                                       if r["selected_marks"] is not None], 50),
        })

    # throughput per concurrency level (across the whole mix)
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else ["mode"])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path} ({len(rows)} groups)")

    if rows:
        print("\n{:<28} {:<14} {:>5} {:>9} {:>9} {:>9}".format(
            "query", "cache/conc", "n", "p50", "p95", "p99"))
        for r in rows:
            tag = r["cache"] if r["mode"] == "latency" else f"c={r['concurrency']}"
            print("{:<28} {:<14} {:>5} {:>9.1f} {:>9.1f} {:>9.1f}".format(
                r["query_name"][:28], tag, r["n"],
                r["p50_ms"] or 0, r["p95_ms"] or 0, r["p99_ms"] or 0))


def write_throughput(records: list[dict], path: Path) -> None:
    """Aggregate QPS + latency across the whole mix, per concurrency level."""
    by_level: dict[int, list[dict]] = {}
    for r in records:
        if r["mode"] != "concurrency" or r["iteration"] == -1:
            continue
        by_level.setdefault(r["concurrency"], []).append(r)
    if not by_level:
        return
    rows = []
    for level, rs in sorted(by_level.items()):
        oks = [r for r in rs if r["ok"]]
        walls = [r["wall_ms"] for r in oks]
        total_s = sum(walls) / 1000 / level if level else 0
        rows.append({
            "concurrency": level,
            "requests": len(rs),
            "errors": len(rs) - len(oks),
            "qps": round(len(oks) / total_s, 2) if total_s else 0,
            "p50_ms": round(pct(walls, 50), 1),
            "p95_ms": round(pct(walls, 95), 1),
            "p99_ms": round(pct(walls, 99), 1),
        })
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {path}")


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="ClickHouse isolated benchmark")
    ap.add_argument("-c", "--config", default="queries.yaml", type=Path)
    ap.add_argument("-m", "--mode", choices=["latency", "concurrency", "explain"],
                    default="latency")
    ap.add_argument("--cache", default="cold,warm",
                    help="latency mode: comma list of cold,warm")
    ap.add_argument("--levels", default="1,2,5,10,25,50",
                    help="concurrency mode: comma list of worker counts")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="concurrency mode: seconds per level")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--only", default=None,
                    help="comma list of query names or classes to run")
    ap.add_argument("--tag", default=None)
    ap.add_argument("-o", "--outdir", default=Path("results"), type=Path)
    args = ap.parse_args()

    run_tag = args.tag or datetime.now().strftime("%Y%m%d-%H%M%S")
    args.outdir.mkdir(parents=True, exist_ok=True)

    client, cfg, queries = load_config(args.config)
    if args.only:
        keep = {s.strip() for s in args.only.split(",")}
        queries = [q for q in queries if q.name in keep or q.cls in keep]
    if not queries:
        print("no queries selected", file=sys.stderr)
        return 1

    try:
        v = client.fetch_text("SELECT version()").strip()
        print(f"connected: ClickHouse {v}  db={client.database}  tag={run_tag}\n")
    except Exception as e:                                   # noqa: BLE001
        print(f"cannot connect: {e}", file=sys.stderr)
        return 1

    if args.mode == "explain":
        explain_mode(client, queries, args.outdir, run_tag)
        return 0

    if args.mode == "latency":
        caches = [c.strip() for c in args.cache.split(",") if c.strip()]
        records = latency_mode(client, cfg, queries, run_tag, caches, args.warmup)
    else:
        levels = [int(x) for x in args.levels.split(",")]
        records = concurrency_mode(client, queries, run_tag, levels,
                                   args.duration, args.warmup)

    print("\nfetching server-side metrics from system.query_log ...")
    enrich_from_query_log(client, records)

    write_raw(records, args.outdir / f"raw_{run_tag}.csv")
    summarize(records, args.outdir / f"summary_{run_tag}.csv")
    write_throughput(records, args.outdir / f"throughput_{run_tag}.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
