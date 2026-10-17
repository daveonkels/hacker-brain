#!/usr/bin/env python3
"""Dependency-free, resumable HN item census over a fixed creation-time window."""
import argparse
import collections
import concurrent.futures
import datetime as dt
import gzip
import hashlib
import html
import http.client
import json
import pathlib
import re
import statistics
import threading
import time

HOST = "hacker-news.firebaseio.com"
LOCAL = threading.local()


def utc(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat()


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


def fetch(route):
    for attempt in range(5):
        try:
            conn = getattr(LOCAL, "conn", None)
            if conn is None:
                conn = LOCAL.conn = http.client.HTTPSConnection(HOST, timeout=25)
            conn.request("GET", "/v0/" + route + ".json", headers={
                "User-Agent": "HackerBrain-72h-research/0.1", "Accept": "application/json"})
            response = conn.getresponse()
            body = response.read()
            if response.status != 200:
                raise OSError("HTTP %s: %s" % (response.status, body[:100]))
            return json.loads(body)
        except (OSError, ValueError, http.client.HTTPException) as exc:
            conn = getattr(LOCAL, "conn", None)
            if conn:
                conn.close()
            LOCAL.conn = None
            if attempt == 4:
                raise RuntimeError("%s: %s" % (route, exc)) from exc
            time.sleep(min(8, 0.5 * 2 ** attempt))


def sample_time(item_id):
    # Deleted/null IDs cannot anchor a timestamp search. Probe nearby IDs.
    for offset in range(100):
        data = fetch("item/%s" % (item_id + offset))
        if data and isinstance(data.get("time"), int):
            return item_id + offset, data["time"]
    raise RuntimeError("No timestamp within 100 IDs of %s" % item_id)


def find_start(max_id, cutoff, guard):
    high = max_id
    step = 4096
    probes = []
    while True:
        probe, stamp = sample_time(max(1, max_id - step))
        probes.append({"id": probe, "time": stamp})
        if stamp < cutoff:
            low = probe
            break
        high = probe
        step *= 2
        if max_id - step < 1:
            raise RuntimeError("Could not bracket cutoff")
    while high - low > 128:
        probe, stamp = sample_time((low + high) // 2)
        probes.append({"id": probe, "time": stamp})
        if stamp < cutoff:
            low = probe
        else:
            high = probe
    return max(1, low - guard), probes


def in_window(item, start, end):
    stamp = item.get("time")
    return isinstance(stamp, int) and start <= stamp < end


def collect_chunk(pool, directory, low, high):
    stem = "%s-%s" % (low, high)
    output = directory / (stem + ".ndjson")
    manifest = directory / (stem + ".json")
    if manifest.exists() and output.exists():
        previous = json.loads(manifest.read_text())
        if not previous["failed_ids"] and hashlib.sha256(output.read_bytes()).hexdigest() == previous["sha256"]:
            return previous
    items, nulls, errors = {}, [], {}
    futures = {pool.submit(fetch, "item/%s" % n): n for n in range(low, high + 1)}
    for future in concurrent.futures.as_completed(futures):
        n = futures[future]
        try:
            item = future.result()
            if item is None:
                nulls.append(n)
            elif item.get("id") != n:
                errors[str(n)] = "Response ID mismatch"
            else:
                items[n] = item
        except Exception as exc:
            errors[str(n)] = str(exc)
    raw = b"".join((json.dumps(items[n], ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                   for n in sorted(items))
    tmp = output.with_suffix(".tmp")
    tmp.write_bytes(raw)
    tmp.replace(output)
    result = {"low": low, "high": high, "fetched": len(items), "null_ids": sorted(nulls),
              "failed_ids": errors, "sha256": hashlib.sha256(raw).hexdigest()}
    save_json(manifest, result)
    return result


def summarize(directory, config):
    counts = collections.Counter()
    sizes = []
    authors, ids, parent_ids = set(), set(), set()
    missing_time, outside, nulls, failed = [], 0, [], {}
    text_bytes = html_bytes = 0
    earliest = latest = None
    start, end = config["start_timestamp"], config["end_timestamp"]
    exported = directory / "items.ndjson"
    with exported.open("wb") as dest, (directory / "unattributed.ndjson").open("wb") as unknown:
        for path in sorted((directory / "chunks").glob("*.ndjson")):
            manifest = json.loads(path.with_suffix(".json").read_text())
            nulls.extend(manifest["null_ids"])
            failed.update(manifest["failed_ids"])
            for line in path.read_bytes().splitlines(keepends=True):
                item = json.loads(line)
                if not isinstance(item.get("time"), int):
                    missing_time.append(item["id"])
                    unknown.write(line)
                    continue
                if not in_window(item, start, end):
                    outside += 1
                    continue
                dest.write(line)
                ids.add(item["id"])
                counts[item.get("type", "unknown")] += 1
                counts["deleted"] += bool(item.get("deleted"))
                counts["dead"] += bool(item.get("dead"))
                if item.get("by"):
                    authors.add(item["by"])
                if item.get("parent"):
                    parent_ids.add(item["parent"])
                body = item.get("text", "")
                html_bytes += len(body.encode())
                plain = html.unescape(re.sub(r"<[^>]+>", " ", body))
                size = len(plain.encode())
                text_bytes += size
                if body:
                    sizes.append(size)
                earliest = min(earliest or item["time"], item["time"])
                latest = max(latest or item["time"], item["time"])
    with exported.open("rb") as src, (directory / "items.ndjson.gz").open("wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as dest:
            while True:
                block = src.read(1024 * 1024)
                if not block:
                    break
                dest.write(block)
    expected = config["max_id"] - config["scan_start_id"] + 1
    accounted = len(ids) + len(missing_time) + outside + len(nulls) + len(failed)
    result = {
        "window": {"start_inclusive": utc(start), "end_exclusive": utc(end), "hours": config["hours"]},
        "coverage": {"id_range": [config["scan_start_id"], config["max_id"]],
            "expected_ids": expected, "accounted_ids": accounted,
            "all_scanned_ids_accounted_for": expected == accounted,
            "failed_count": len(failed), "failed_ids": failed, "null_count": len(nulls), "null_ids": nulls,
            "items_without_timestamp": len(missing_time), "unattributed_ids": missing_time,
            "outside_window_count": outside,
            "boundary_method": "Timestamp-guided ID search plus older-ID guard; assumes broadly chronological ID allocation.",
            "earliest_in_window": utc(earliest) if earliest else None,
            "latest_in_window": utc(latest) if latest else None},
        "items": len(ids), "counts": dict(counts), "unique_authors": len(authors),
        "distinct_parents_not_in_window": len(parent_ids - ids),
        "storage_bytes": {"compact_jsonl": exported.stat().st_size,
            "gzip_jsonl": (directory / "items.ndjson.gz").stat().st_size,
            "html_comment_and_story_bodies": html_bytes, "plain_text_bodies": text_bytes,
            "unattributed_jsonl": (directory / "unattributed.ndjson").stat().st_size,
            "raw_scan_chunks": sum(p.stat().st_size for p in (directory / "chunks").glob("*.ndjson"))},
        "text_item_bytes": {"count": len(sizes), "median": statistics.median(sizes) if sizes else 0,
            "mean": round(statistics.mean(sizes), 1) if sizes else 0, "max": max(sizes, default=0)},
        "notes": ["Raw source HTML retained without truncation.",
            "Window is based on creation time, not edit time; older parent context is not backfilled.",
            "Dead/deleted flags overlap item types. Null/timeless items cannot be assigned to the window.",
            "Snapshot collected over time; no guarantee of every historical revision or later deletion."]}
    save_json(directory / "summary.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=72)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--guard", type=int, default=2000)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--summarize-only", action="store_true")
    args = parser.parse_args()
    directory = args.output
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "chunks").mkdir(exist_ok=True)
    config_path = directory / "run.json"
    if config_path.exists():
        config = json.loads(config_path.read_text())
    else:
        end = int(time.time())
        start = end - int(args.hours * 3600)
        maximum = fetch("maxitem")
        first, probes = find_start(maximum, start, args.guard)
        config = {"hours": args.hours, "start_timestamp": start, "end_timestamp": end,
            "scan_start_id": first, "max_id": maximum, "boundary_probes": probes,
            "older_guard_ids": args.guard, "chunk_size": args.chunk_size}
        save_json(config_path, config)
    print(json.dumps({"event": "start", **config}), flush=True)
    if not args.summarize_only:
        began = time.monotonic()
        total = config["max_id"] - config["scan_start_id"] + 1
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            for low in range(config["scan_start_id"], config["max_id"] + 1, config["chunk_size"]):
                high = min(low + config["chunk_size"] - 1, config["max_id"])
                result = collect_chunk(pool, directory / "chunks", low, high)
                done = high - config["scan_start_id"] + 1
                elapsed = time.monotonic() - began
                print(json.dumps({"event": "progress", "done": done, "total": total,
                    "percent": round(100 * done / total, 1), "elapsed_seconds": round(elapsed, 1),
                    "failed_in_chunk": len(result["failed_ids"])}), flush=True)
    report = summarize(directory, config)
    print(json.dumps({"event": "complete", "summary": report}), flush=True)
    if report["coverage"]["failed_count"] or not report["coverage"]["all_scanned_ids_accounted_for"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
