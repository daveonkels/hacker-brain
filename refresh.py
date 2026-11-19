"""Scheduled or manual, resumable HN refresh with atomic search-index generations."""
import collections
import concurrent.futures
import contextlib
import datetime
import fcntl
import gzip
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import uuid
import subprocess
import sys
import corpus

import collect_hn as hn
import context
import semantic


def write_items(path, items):
    with path.open('w') as stream:
        for n in sorted(items):stream.write(json.dumps(items[n],ensure_ascii=False,separators=(',',':'))+'\n')


def active_folder(root):
    root = Path(root).resolve()
    pointer = root/'current.json'
    if not pointer.exists():
        return root
    folder = (root/json.loads(pointer.read_text())['folder']).resolve()
    if root not in folder.parents or not (folder/'search.sqlite3').is_file():
        raise ValueError('Invalid active collection pointer')
    return folder


class RefreshManager:
    def __init__(self, root, on_publish=None, worker=None, isolated=False):
        self.root=Path(root).resolve()
        self.state_dir=self.root/'.refresh'
        self.state_dir.mkdir(exist_ok=True)
        self.condition=threading.Condition()
        self.readers=0
        self.publishing=False
        self.on_publish=on_publish or (lambda:None)
        self.worker=worker or self.run
        self.isolated=isolated
        self.process=None
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
            value=json.loads(self.status_path.read_text()) if self.isolated and self.thread is not None and self.status_path.exists() else dict(self.status)
        folder=active_folder(self.root)
        state=json.loads((folder/'refresh-state.json').read_text()) if (folder/'refresh-state.json').exists() else {}
        value['schedule']='hourly' if os.environ.get('HACKER_BRAIN_REFRESH_SCHEDULE')=='hourly' else 'manual'
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
                self.report(state='running',phase='starting',message='Checking for new HN items…',done=0,total=0,error=None,cleanup_warning=None)
            except Exception:
                lock.close()
                raise
            def work():
                try:
                    if self.isolated:
                        # The refresh needs public HN and local Ollama only. Do not
                        # propagate broker credentials or proxy settings to it.
                        allowed={'PATH','HOME','LANG','LC_ALL','TMPDIR','LD_LIBRARY_PATH','PYTHONUNBUFFERED','PYTHONDONTWRITEBYTECODE','HACKER_BRAIN_OLLAMA_URL'}
                        env={k:v for k,v in os.environ.items() if k in allowed}
                        env['MALLOC_ARENA_MAX']='2'
                        self.process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--prepare',str(self.root),'--lock-fd',str(lock.fileno())],pass_fds=(lock.fileno(),),env=env)
                        if self.process.wait()!=0:raise RuntimeError('Refresh worker failed')
                        self.status=json.loads(self.status_path.read_text())
                        stage=self.state_dir/'work'
                        self.finish(stage,json.loads((stage/'plan.json').read_text()))
                    else:
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

    def prune_generations(self, current, previous):
        """Bound generated snapshot storage; never remove the seed or active/base pair."""
        retain=int(os.environ.get('HACKER_BRAIN_RETAIN_GENERATIONS','0'))
        if retain==0:
            return
        if retain<2:
            raise ValueError('Retention must keep at least two generations')
        versions=self.state_dir/'versions'
        managed=[]
        for folder in versions.iterdir():
            if folder.is_symlink() or not folder.is_dir() or not re.fullmatch(r'[0-9a-f]{32}',folder.name):
                continue
            if not (folder/'prepared.json').is_file() or not (folder/'plan.json').is_file():
                continue
            plan=json.loads((folder/'plan.json').read_text())
            if plan.get('id')==folder.name:
                managed.append(folder)
        keep={current.resolve(),previous.resolve(),active_folder(self.root)}
        # Keep any staged base as well, including if called independently for maintenance.
        staged=self.state_dir/'work'/'plan.json'
        if staged.exists():
            keep.add(Path(json.loads(staged.read_text())['base']).resolve())
        for folder in sorted(managed,key=lambda p:p.stat().st_mtime,reverse=True):
            if len(keep.intersection(managed))>=retain:
                break
            keep.add(folder)
        for folder in managed:
            if folder not in keep:
                shutil.rmtree(folder)

    def run(self, prepare_only=False):
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
                  'low':max(1,base_state['max_id']-199),'pending':base_state['pending_ids']}
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
            with contextlib.closing(corpus.open_store(base,stage)) as source:
                new_count=0
                start=datetime.datetime.fromisoformat(baseline['window']['start_inclusive']).timestamp()
                def apply(item):
                    nonlocal new_count
                    n=item['id'];old=source.execute('SELECT sample FROM source WHERE id=?',(n,)).fetchone()
                    if old and old[0]:corpus.put(source,item,1)
                    elif isinstance(item.get('time'),int) and start<=item['time']<plan['end']:
                        corpus.put(source,item,1);new_count+=1
                    elif not isinstance(item.get('time'),int) or item['time']>=plan['end']:pending.add(n)
                    else:corpus.put(source,item,0)
                with (stage/'unattributed.ndjson').open('w') as unattributed:
                    for path in sorted(chunks.glob('*.ndjson')):
                        for item in corpus.records(path):
                            apply(item)
                            if item['id'] in pending:unattributed.write(json.dumps(item)+'\n')
                    for n in plan['pending']:
                        if n>=plan['low']:continue
                        item=hn.fetch('item/%s'%n)
                        if item is None:pending.add(n)
                        elif item.get('id')!=n:raise ValueError('Retry ID mismatch')
                        else:
                            apply(item)
                            if n in pending:unattributed.write(json.dumps(item)+'\n')
                source.commit()
                self.report(phase='context',message='Connecting parent conversations…',done=0,total=0)
                report=corpus.backfill(source,stage)
                if report['failed_ids']:raise RuntimeError('Parent context fetch failed')
                corpus.export(source,stage,baseline,plan,sorted(pending),new_count,report)
                hn.save_json(stage/'refresh-state.json',{'max_id':plan['max_id'],'pending_ids':sorted(pending),'fetched_at':hn.utc(plan['end'])})
                shutil.copy2(base/'run.json',stage/'run.json')
                self.report(phase='indexing',message='Updating changed keyword records…')
                search.update(stage,base,source)
            self.report(phase='indexing',message='Reusing unchanged semantic passages…')
            semantic.seed_unchanged(base,stage)
            hn.save_json(stage/'prepared.json',{'ready':True})
        self.report(phase='embedding',message='Indexing new and changed comments…',done=0,total=0)
        semantic.build(stage,batch_size=4,progress=lambda done,total:self.report(done=done,total=total))
        for name in ('search.sqlite3','semantic.sqlite3'):
            with sqlite3.connect(stage/name) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':
                    raise RuntimeError('Index integrity check failed')
        if prepare_only:
            self.report(phase='ready',message='Indexes verified; ready to publish.',done=0,total=0)
            return
        self.finish(stage,plan)

    def finish(self,stage,plan):
        base=Path(plan['base'])
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
        try:
            self.prune_generations(destination,base)
        except (OSError,ValueError,KeyError):
            # Publication already succeeded; cleanup cannot turn it into a failed refresh.
            self.report(cleanup_warning='Older index snapshots could not be removed; check available storage.')


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='Isolated incremental refresh preparation')
    parser.add_argument('--prepare',type=Path,required=True)
    parser.add_argument('--lock-fd',type=int,required=True)
    args=parser.parse_args()
    import resource
    threading.stack_size(512*1024)
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS,(768*1024*1024,768*1024*1024))
    os.fstat(args.lock_fd)  # Parent's inherited flock stays held for this worker.
    manager=RefreshManager(args.prepare)
    manager.report(state='running',phase='starting',message='Preparing the incremental refresh…',error=None,cleanup_warning=None)
    manager.run(prepare_only=True)
