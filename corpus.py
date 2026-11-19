"""Disk-backed, streaming source history for incremental refreshes."""
import collections
import contextlib
import datetime
import gzip
import json
import shutil
import sqlite3
from pathlib import Path
import collect_hn as hn


def records(path):
    if path.exists():
        with path.open() as stream:
            for line in stream:
                if line.strip():
                    yield json.loads(line)


def copy_database(source, destination):
    destination.unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(source)) as src, contextlib.closing(sqlite3.connect(destination)) as dst:
        src.backup(dst, pages=256)


def open_store(base, stage):
    path=stage/'corpus.sqlite3'
    if (base/'corpus.sqlite3').exists():
        copy_database(base/'corpus.sqlite3',path)
    else:
        path.unlink(missing_ok=True)
    db=sqlite3.connect(path)
    db.execute('PRAGMA cache_size=-8192')
    db.execute('PRAGMA temp_store=FILE')
    db.executescript('''CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY, parent INTEGER, sample INTEGER NOT NULL, data TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS source_parent ON source(parent);
        DROP TABLE IF EXISTS changed;
        CREATE TABLE changed(id INTEGER PRIMARY KEY);''')
    if not (base/'corpus.sqlite3').exists():
        for name,sample in [('context.ndjson',0),('items.ndjson',1)]:
            for item in records(base/name):
                db.execute('INSERT OR REPLACE INTO source VALUES(?,?,?,?)',(item['id'],item.get('parent'),sample,json.dumps(item,ensure_ascii=False,separators=(',',':'))))
        db.commit()
    return db


def put(db,item,sample):
    encoded=json.dumps(item,ensure_ascii=False,separators=(',',':'))
    old=db.execute('SELECT sample,data FROM source WHERE id=?',(item['id'],)).fetchone()
    if old and old[0]==sample and json.loads(old[1])==item:
        return False
    db.execute('INSERT OR REPLACE INTO source VALUES(?,?,?,?)',(item['id'],item.get('parent'),sample,encoded))
    db.execute('INSERT OR IGNORE INTO changed VALUES(?)',(item['id'],))
    return True


def backfill(db,folder):
    unavailable=set();failed={};fetched=0
    # SQL anti-join is bounded; only a batch of missing ancestors is held in RAM.
    db.execute('CREATE TEMP TABLE unavailable(id INTEGER PRIMARY KEY)')
    while True:
        missing=db.execute('''SELECT DISTINCT s.parent FROM source s LEFT JOIN source p ON p.id=s.parent
            LEFT JOIN unavailable u ON u.id=s.parent WHERE s.parent IS NOT NULL AND p.id IS NULL AND u.id IS NULL LIMIT 128''').fetchall()
        if not missing:break
        for (n,) in missing:
            try:
                item=hn.fetch('item/%s'%n)
                if item is not None and item.get('id')!=n:raise ValueError('Ancestor ID mismatch')
                if item is None:
                    unavailable.add(n);db.execute('INSERT OR IGNORE INTO unavailable VALUES(?)',(n,))
                else:put(db,item,0);fetched+=1
            except Exception as exc:
                failed[str(n)]=type(exc).__name__;db.execute('INSERT OR IGNORE INTO unavailable VALUES(?)',(n,))
        db.commit()
    report={'sample_items':db.execute('SELECT count(*) FROM source WHERE sample=1').fetchone()[0],
        'context_items':db.execute('SELECT count(*) FROM source WHERE sample=0').fetchone()[0],
        'fetched_this_run':fetched,'reused_this_run':0,'unavailable_ids':sorted(unavailable),
        'failed_ids':failed,'unresolved_parent_ids':sorted(unavailable|{int(n) for n in failed}),'fetched_at':hn.utc(hn.time.time())}
    return report


def export(db,folder,baseline,plan,pending,new_count,context_report):
    counts=collections.Counter()
    db.execute('CREATE TEMP TABLE authors(name TEXT PRIMARY KEY)')
    for sample,name in [(1,'items.ndjson'),(0,'context.ndjson')]:
        with (folder/name).open('w') as out:
            for (data,) in db.execute('SELECT data FROM source WHERE sample=? ORDER BY id',(sample,)):
                out.write(data+'\n')
                if sample:
                    item=json.loads(data);counts[item.get('type','unknown')]+=1
                    counts['deleted']+=bool(item.get('deleted'));counts['dead']+=bool(item.get('dead'))
                    if item.get('by'):db.execute('INSERT OR IGNORE INTO authors VALUES(?)',(item['by'],))
    with (folder/'items.ndjson').open('rb') as src, (folder/'items.ndjson.gz').open('wb') as out:
        with gzip.GzipFile(fileobj=out,mode='wb',mtime=0) as dst:shutil.copyfileobj(src,dst,1024*1024)
    start=baseline['window']['start_inclusive']
    result={'window':{'start_inclusive':start,'end_exclusive':hn.utc(plan['end']),'hours':(plan['end']-datetime.datetime.fromisoformat(start).timestamp())/3600},
        'items':db.execute('SELECT count(*) FROM source WHERE sample=1').fetchone()[0],'counts':dict(counts),
        'unique_authors':db.execute('SELECT count(*) FROM authors').fetchone()[0],
        'storage_bytes':{'compact_jsonl':(folder/'items.ndjson').stat().st_size,'gzip_jsonl':(folder/'items.ndjson.gz').stat().st_size},
        'refresh':{'through_id':plan['max_id'],'fetched_at':hn.utc(plan['end']),'pending_ids':pending,'new_items':new_count},
        'notes':['Cumulative history preserved. Refresh rechecks recent overlap and pending IDs, not every historical edit.']}
    context_report['context_bytes']=(folder/'context.ndjson').stat().st_size
    hn.save_json(folder/'context-summary.json',context_report)
    hn.save_json(folder/'summary.json',result)
    db.commit()
    return result
