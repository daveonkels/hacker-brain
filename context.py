"""Backfill only missing ancestor chains, retaining the fixed sample separately."""
import argparse
import concurrent.futures
import json
import pathlib
import time

from collect_hn import fetch, save_json, utc


def load_items(path):
    if not path.exists():
        return {}
    return {item['id']: item for item in (json.loads(line) for line in path.read_text().splitlines())}


def backfill(folder, workers=12):
    sample = load_items(folder / 'items.ndjson')
    context_path = folder / 'context.ndjson'
    context = load_items(context_path)
    cache = {}
    for path in (folder / 'chunks').glob('*.ndjson'):
        cache.update(load_items(path))
    known = {**sample, **context}
    unavailable, failed = set(), {}
    fetched = reused = 0
    with context_path.open('a') as out, concurrent.futures.ThreadPoolExecutor(workers) as pool:
        while True:
            missing = sorted({i['parent'] for i in known.values() if i.get('parent')
                              and i['parent'] not in known and i['parent'] not in unavailable
                              and str(i['parent']) not in failed})
            if not missing:
                break
            futures = {pool.submit(fetch, 'item/%s' % n): n for n in missing if n not in cache}
            results = [(n, cache[n]) for n in missing if n in cache]
            reused += len(results)
            for future in concurrent.futures.as_completed(futures):
                n = futures[future]
                try:
                    item = future.result()
                    if item is not None and item.get('id') != n:
                        raise ValueError('Response ID mismatch')
                    results.append((n, item))
                    fetched += 1
                except Exception as exc:
                    failed[str(n)] = str(exc)
            for n, item in results:
                if item is None:
                    unavailable.add(n)
                    continue
                known[n] = context[n] = item
                out.write(json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n')
            out.flush()
            print(json.dumps({'context_items': len(context), 'fetched_this_run': fetched,
                              'reused_this_run': reused, 'failed': len(failed)}), flush=True)
    missing = sorted({i['parent'] for i in known.values() if i.get('parent') and i['parent'] not in known})
    report = {'sample_items': len(sample), 'context_items': len(context),
              'context_bytes': context_path.stat().st_size, 'fetched_this_run': fetched,
              'reused_this_run': reused, 'unavailable_ids': sorted(unavailable),
              'failed_ids': failed, 'unresolved_parent_ids': missing, 'fetched_at': utc(time.time()),
              'scope': 'Recursive ancestors and original stories; not every historical sibling or descendant.'}
    save_json(folder / 'context-summary.json', report)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=pathlib.Path, required=True)
    args = p.parse_args()
    report = backfill(args.data)
    print(json.dumps(report), flush=True)
    if report['failed_ids'] or report['unresolved_parent_ids']:
        raise SystemExit(2)
