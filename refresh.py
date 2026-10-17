"""Manual, resumable HN refresh with atomic search-index generations."""
import collections
import concurrent.futures
import contextlib
import datetime
import fcntl
import gzip
import json
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid

import collect_hn as hn
import context
import semantic


def write_items(path, items):
    path.write_text(''.join(json.dumps(items[n], ensure_ascii=False, separators=(',', ':'))+'\n' for n in sorted(items)))


def active_folder(root):
    root = Path(root).resolve()
    pointer = root/'current.json'
    if not pointer.exists():
        return root
    folder = (root/json.loads(pointer.read_text())['folder']).resolve()
    if root not in folder.parents or not (folder/'search.sqlite3').is_file():
        raise ValueError('Invalid active collection pointer')
    return folder


def summary(folder, items, baseline, plan, pending):
    counts = collections.Counter()
    for item in items.values():
        counts[item.get('type','unknown')] += 1
        counts['deleted'] += bool(item.get('deleted'))
        counts['dead'] += bool(item.get('dead'))
    start = baseline['window']['start_inclusive']
    hours = (plan['end']-datetime.datetime.fromisoformat(start).timestamp())/3600
    raw = (folder/'items.ndjson').read_bytes()
    (folder/'items.ndjson.gz').write_bytes(gzip.compress(raw, mtime=0))
    return {'window':{'start_inclusive':start,'end_exclusive':hn.utc(plan['end']),'hours':hours},
        'items':len(items),'counts':dict(counts),
        'unique_authors':len({i['by'] for i in items.values() if i.get('by')}),
        'storage_bytes':{'compact_jsonl':len(raw),'gzip_jsonl':(folder/'items.ndjson.gz').stat().st_size},
        'refresh':{'through_id':plan['max_id'],'fetched_at':hn.utc(plan['end']),
                   'pending_ids':pending,'new_items':len(set(items)-set(plan['original_ids']))},
        'notes':['Cumulative collection; new HN IDs plus the most recent 200 IDs are fetched on manual refresh.',
                 'Older edits and deletions outside that overlap are not exhaustively synchronized.',
                 'Null or timestamp-less IDs are retained for retry. HN is not an atomic historical snapshot.']}


class RefreshManager:
    def __init__(self, root, on_publish=None, worker=None):
        self.root=Path(root).resolve()
        self.state_dir=self.root/'.refresh'
        self.state_dir.mkdir(exist_ok=True)
        self.condition=threading.Condition()
        self.readers=0
        self.publishing=False
        self.on_publish=on_publish or (lambda:None)
        self.worker=worker or self.run
        self.thread=None
        self.status_path=self.state_dir/'status.json'
        self.status={'state':'idle','message':'Ready to fetch new HN items.'}
        if self.status_path.exists():
            self.status=json.loads(self.status_path.read_text())
            if self.status['state']=='running':
                self.status={'state':'interrupted','message':'Refresh was interrupted. Retry to resume; the previous collection is still available.'}

    def report(self, **values):
        with self.condition:
            self.status.update(values)
            hn.save_json(self.status_path,self.status)

    def get_status(self):
        with self.condition:
            value=dict(self.status)
        folder=active_folder(self.root)
        state=json.loads((folder/'refresh-state.json').read_text()) if (folder/'refresh-state.json').exists() else {}
        value['through']=state.get('fetched_at') or json.loads((folder/'summary.json').read_text())['window']['end_exclusive']
        return value

    @contextlib.contextmanager
    def read(self):
        with self.condition:
            while self.publishing:
                self.condition.wait()
            folder=active_folder(self.root)
            self.readers+=1
        try:
            yield folder
        finally:
            with self.condition:
                self.readers-=1
                self.condition.notify_all()

    def start(self):
        with self.condition:
            if self.thread and self.thread.is_alive():
                return False
            lock=(self.state_dir/'refresh.lock').open('a')
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                return False
            try:
                self.report(state='running',phase='starting',message='Checking for new HN items…',done=0,total=0,error=None)
            except Exception:
                lock.close()
                raise
            def work():
                try:
                    self.worker()
                except Exception as exc:
                    print(json.dumps({'event':'refresh_failed','phase':self.status.get('phase'),'type':type(exc).__name__}),flush=True)
                    # Raw provider errors may include sensitive proxy details.
                    self.report(state='failed',message='Refresh failed. Your previous collection is still available. Retry to resume.',error='Collection or indexing did not complete.')
                finally:
                    fcntl.flock(lock,fcntl.LOCK_UN)
                    lock.close()
            self.thread=threading.Thread(target=work,daemon=True)
            self.thread.start()
            return True

    def publish(self, stage, plan):
        versions=self.state_dir/'versions';versions.mkdir(exist_ok=True)
        destination=versions/plan['id']
        with self.condition:
            self.publishing=True
            try:
                while self.readers:
                    self.condition.wait()
                stage.rename(destination)
                hn.save_json(self.root/'current.json',{'folder':str(destination.relative_to(self.root))})
                self.on_publish()
            finally:
                self.publishing=False
                self.condition.notify_all()
        return destination

    def run(self):
        import search
        base=active_folder(self.root)
        stage=self.state_dir/'work';stage.mkdir(exist_ok=True)
        baseline=json.loads((base/'summary.json').read_text())
        base_state=json.loads((base/'refresh-state.json').read_text()) if (base/'refresh-state.json').exists() else {
            'max_id':json.loads((base/'run.json').read_text())['max_id'], 'pending_ids':sorted(set(baseline.get('coverage',{}).get('null_ids',[])) | set(baseline.get('coverage',{}).get('unattributed_ids',[])))}
        if not (stage/'plan.json').exists():
            free=shutil.disk_usage(self.root).free
            if free < max(1024**3,3*sum(p.stat().st_size for p in base.glob('*.sqlite3') if p.is_file())):
                raise RuntimeError('Not enough space for a safe index generation')
            maximum=hn.fetch('maxitem')
            if type(maximum) is not int or maximum<base_state['max_id']:
                raise ValueError('Invalid HN maximum ID')
            plan={'id':uuid.uuid4().hex,'base':str(base),'max_id':maximum,'end':int(time.time())+1,
                  'low':max(1,base_state['max_id']-199),'pending':base_state['pending_ids'],
                  'original_ids':list(context.load_items(base/'items.ndjson'))}
            hn.save_json(stage/'plan.json',plan)
        plan=json.loads((stage/'plan.json').read_text())
        if plan['base']!=str(base):
            raise ValueError('Staged refresh is based on a different active collection')
        if not (stage/'prepared.json').exists():
            chunks=stage/'chunks';chunks.mkdir(exist_ok=True)
            total=plan['max_id']-plan['low']+1
            pending=set()
            with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
                for low in range(plan['low'],plan['max_id']+1,500):
                    high=min(low+499,plan['max_id'])
                    self.report(phase='fetching',done=low-plan['low'],total=total,message='Fetching new comments and stories…')
                    result=hn.collect_chunk(pool,chunks,low,high)
                    if result['failed_ids']:
                        raise RuntimeError('Some HN items could not be fetched')
                    pending.update(result['null_ids'])
            new={}
            for path in sorted(chunks.glob('*.ndjson')):
                new.update(context.load_items(path))
            # Retry unavailable older IDs without revisiting the whole archive.
            for n in plan['pending']:
                if n>=plan['low']:
                    continue
                item=hn.fetch('item/%s'%n)
                if item is None:
                    pending.add(n)
                elif item.get('id')!=n:
                    raise ValueError('Retry ID mismatch')
                else:
                    new[n]=item
            items=context.load_items(base/'items.ndjson')
            older=context.load_items(base/'context.ndjson')
            start=datetime.datetime.fromisoformat(baseline['window']['start_inclusive']).timestamp()
            for n,item in new.items():
                if n in items:
                    # Deletion markers may have lost their creation timestamp.
                    items[n]=item
                elif isinstance(item.get('time'),int) and start<=item['time']<plan['end']:
                    items[n]=item
                    older.pop(n,None)
                elif not isinstance(item.get('time'),int) or item['time']>=plan['end']:
                    pending.add(n)
                else:
                    older[n]=item
            write_items(stage/'items.ndjson',items)
            write_items(stage/'context.ndjson',older)
            write_items(stage/'unattributed.ndjson',{n:i for n,i in new.items() if n in pending})
            self.report(phase='context',message='Connecting parent conversations…',done=0,total=0)
            report=context.backfill(stage)
            if report['failed_ids']:
                raise RuntimeError('Parent context fetch failed')
            stats=summary(stage,items,baseline,plan,sorted(pending))
            hn.save_json(stage/'summary.json',stats)
            hn.save_json(stage/'refresh-state.json',{'max_id':plan['max_id'],'pending_ids':sorted(pending),'fetched_at':hn.utc(plan['end'])})
            shutil.copy2(base/'run.json',stage/'run.json')
            self.report(phase='indexing',message='Updating keyword search…')
            search.build(stage)
            self.report(phase='indexing',message='Reusing unchanged semantic passages…')
            semantic.seed_unchanged(base,stage)
            hn.save_json(stage/'prepared.json',{'ready':True})
        self.report(phase='embedding',message='Indexing new and changed comments…',done=0,total=0)
        semantic.build(stage,batch_size=16,progress=lambda done,total:self.report(done=done,total=total))
        for name in ('search.sqlite3','semantic.sqlite3'):
            with sqlite3.connect(stage/name) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise RuntimeError('Index integrity check failed')
        # All generations share the live cache: no labels written during a
        # refresh are lost, and source-aware keys invalidate changed content.
        cache=stage/'rerank.sqlite3'
        if not cache.exists() and not cache.is_symlink():
            cache.symlink_to(self.root/'rerank.sqlite3')
        self.report(phase='publishing',message='Making the updated collection available…',done=0,total=0)
        destination=self.publish(stage,plan)
        result=json.loads((destination/'summary.json').read_text())['refresh']
        self.report(state='complete',phase='complete',message='Refresh complete. New data is ready to search.',
                    new_items=result['new_items'],completed_at=hn.utc(time.time()),done=0,total=0)
