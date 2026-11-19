"""Regression coverage for streaming history and the real process boundary."""
import contextlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import patch
import test_refresh as fixtures
import refresh
import search
import semantic


class IncrementalTests(fixtures.RefreshTests):
    # Inherit the existing end-to-end history, deletion and resume cases too.
    def test_incremental_matches_clean_rebuild_after_ancestor_change(self):
        self.manager.run()
        self.records[1]['title']='A changed discussion title'
        self.records[301]['parent']=2
        self.records[302]['text']='I now use a different archive.'
        self.manager.run()
        folder=refresh.active_folder(self.root)
        before=self.rows(folder/'search.sqlite3')
        rebuilt=self.root/'full-rebuild';rebuilt.mkdir()
        for name in ['items.ndjson','context.ndjson','summary.json','context-summary.json']:
            shutil.copy2(folder/name,rebuilt/name)
        search.build(rebuilt)
        self.assertEqual(before,self.rows(rebuilt/'search.sqlite3'))
        with sqlite3.connect(folder/'corpus.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source').fetchone()[0],len(before))

    def rows(self,path):
        with contextlib.closing(sqlite3.connect(path)) as db:return db.execute('SELECT * FROM items ORDER BY id').fetchall()

    def test_refresh_never_reads_whole_exports_or_indexes(self):
        read_text=Path.read_text;read_bytes=Path.read_bytes
        def text(path,*args,**kwargs):
            self.assertNotEqual(path.suffix,'.ndjson','Whole export read')
            return read_text(path,*args,**kwargs)
        def binary(path,*args,**kwargs):
            self.assertNotIn(path.suffix,['.ndjson','.sqlite3'],'Whole source/index read')
            return read_bytes(path,*args,**kwargs)
        with patch.object(Path,'read_text',text),patch.object(Path,'read_bytes',binary):
            self.manager.run();self.manager.run()

    def test_isolated_worker_preserves_live_reader_and_single_refresh(self):
        # Real child interpreter and file protocol; fake only external HN/model I/O.
        wrapper=self.root/'worker.py'
        wrapper.write_text('''import sys,json,time
sys.path.insert(0,'''+repr(str(Path(refresh.__file__).parent))+''')
import refresh,semantic,collect_hn
from pathlib import Path
records=json.loads((Path(sys.argv[2])/'fixture.json').read_text())
collect_hn.fetch=lambda route:303 if route=='maxitem' else records.get(route.split('/')[-1])
refresh.time.time=lambda:300
semantic.model_digest=lambda:'fixed-model'
semantic.embed=lambda texts,prefix:[semantic.unit([1,2,3]) for t in texts]
time.sleep(0.5)
import runpy
runpy.run_module('refresh',run_name='__main__')
''')
        (self.root/'fixture.json').write_text(json.dumps(self.records))
        manager=refresh.RefreshManager(self.root,isolated=True)
        original=subprocess.Popen
        def spawn(command,**kwargs):
            command=list(command);command[1]=str(wrapper)
            return original(command,**kwargs)
        with patch.object(refresh.subprocess,'Popen',side_effect=spawn):
            with manager.read() as folder:
                self.assertTrue(manager.start());self.assertFalse(manager.start())
                deadline=time.monotonic()+8
                while manager.process is None and time.monotonic()<deadline:time.sleep(0.01)
                self.assertNotEqual(manager.process.pid,os.getpid())
                start=time.monotonic();manager.get_status();self.assertLess(time.monotonic()-start,0.5)
                with search.connect(folder/'search.sqlite3') as db:
                    self.assertEqual(search.search_items(db,{'q':'local'})['total'],1)
                with manager.condition:
                    self.assertTrue(manager.condition.wait_for(lambda:manager.publishing,timeout=8))
                self.assertEqual(refresh.active_folder(self.root),self.root)
            manager.thread.join(8)
        self.assertFalse(manager.thread.is_alive());self.assertEqual(manager.get_status()['state'],'complete')
        self.assertEqual(set(refresh.context.load_items(refresh.active_folder(self.root)/'items.ndjson')),{300,301,302})

    def test_isolated_worker_failure_preserves_history(self):
        manager=refresh.RefreshManager(self.root,isolated=True)
        original=subprocess.Popen
        def spawn(command,**kwargs):return original([sys.executable,'-c','raise SystemExit(3)'],**kwargs)
        with patch.object(refresh.subprocess,'Popen',side_effect=spawn):
            self.assertTrue(manager.start());manager.thread.join(5)
        self.assertEqual(manager.get_status()['state'],'failed')
        self.assertEqual(refresh.active_folder(self.root),self.root)
