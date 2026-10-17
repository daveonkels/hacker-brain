import contextlib
import hashlib
import http.client
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

import collect_hn as hn
import context
import refresh
import search
import semantic


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name).resolve()
        self.records={
            1:{'id':1,'type':'story','time':50,'title':'Local workflows'},
            300:{'id':300,'type':'comment','parent':1,'time':100,'text':'I use a local model for sorting email.'},
            301:{'id':301,'type':'comment','parent':300,'time':200,'text':'I run it on my laptop.'},
            302:{'id':302,'type':'comment','parent':2,'time':250,'text':'I built a SQLite backup. '+'full text '*300},
            2:{'id':2,'type':'comment','parent':1,'time':60,'text':'An older parent.'},
            303:None,
        }
        refresh.write_items(self.root/'items.ndjson',{300:self.records[300]})
        refresh.write_items(self.root/'context.ndjson',{1:self.records[1]})
        hn.save_json(self.root/'summary.json',{'window':{'start_inclusive':hn.utc(100),'end_exclusive':hn.utc(200),'hours':100/3600},'counts':{'comment':1}})
        hn.save_json(self.root/'run.json',{'max_id':301})
        hn.save_json(self.root/'context-summary.json',{'context_items':1})
        search.build(self.root)
        self.stack=contextlib.ExitStack()
        self.stack.enter_context(patch.object(semantic,'model_digest',return_value='fixed-model'))
        self.embedding=self.stack.enter_context(patch.object(semantic,'embed',side_effect=lambda texts,prefix:[semantic.unit([1,2,3]) for t in texts]))
        semantic.build(self.root)
        self.embedding.reset_mock()
        def fetch(route):
            if route=='maxitem':return 303
            return self.records.get(int(route.split('/')[1]))
        self.fetch=self.stack.enter_context(patch.object(hn,'fetch',side_effect=fetch))
        self.stack.enter_context(patch.object(context,'fetch',side_effect=fetch))
        self.stack.enter_context(patch.object(refresh.time,'time',return_value=300))
        self.stack.enter_context(patch.object(refresh.shutil,'disk_usage',return_value=type('Usage',(),{'free':10**10})()))
        self.manager=refresh.RefreshManager(self.root)

    def tearDown(self):
        self.stack.close();self.temp.cleanup()

    def test_new_ids_boundary_ancestors_and_repeat_refresh(self):
        self.manager.run()
        folder=refresh.active_folder(self.root)
        self.assertNotEqual(folder,self.root)
        self.assertEqual(set(context.load_items(folder/'items.ndjson')),{300,301,302})
        with search.connect(folder/'search.sqlite3') as db:
            result=search.search_items(db,{'q':'SQLite'})
            self.assertEqual(result['items'][0]['id'],302)
            self.assertIn('full text '*299,result['items'][0]['body'])
            self.assertEqual(result['items'][0]['hn_url'],'https://news.ycombinator.com/item?id=302')
            self.assertEqual([i['id'] for i in search.conversation(db,302)['ancestors']],[1,2])
        state=json.loads((folder/'refresh-state.json').read_text())
        self.assertEqual(state['max_id'],303);self.assertIn(303,state['pending_ids'])
        semantic.Engine(folder)
        self.embedding.reset_mock();self.manager.run()
        self.embedding.assert_not_called()
        self.assertEqual(self.manager.status['new_items'],0)
        self.assertEqual(len(context.load_items(refresh.active_folder(self.root)/'items.ndjson')),3)

    def test_failure_preserves_cursor_and_resume_reuses_completed_embedding_batches(self):
        before=hashlib.sha256((self.root/'search.sqlite3').read_bytes()).hexdigest()
        self.embedding.side_effect=RuntimeError('temporary offline model')
        self.manager.start();self.manager.thread.join(5)
        self.assertEqual(self.manager.status['state'],'failed')
        self.assertEqual(refresh.active_folder(self.root),self.root)
        self.assertEqual(before,hashlib.sha256((self.root/'search.sqlite3').read_bytes()).hexdigest())
        self.assertTrue((self.root/'.refresh/work/prepared.json').exists())
        self.embedding.side_effect=lambda texts,prefix:[semantic.unit([1,2,3]) for t in texts]
        self.fetch.reset_mock();self.manager.start();self.manager.thread.join(5)
        self.assertEqual(self.manager.status['state'],'complete')
        self.fetch.assert_not_called()

    def test_partial_embedding_retry_does_not_repeat_completed_batches(self):
        for n in range(304,330):
            self.records[n]={'id':n,'type':'comment','parent':1,'time':260,'text':'I use a new workflow '+str(n)}
        original_fetch=self.fetch.side_effect
        self.fetch.side_effect=lambda route:329 if route=='maxitem' else original_fetch(route)
        calls=[]
        def embed(texts,prefix):
            calls.append(texts)
            if len(calls)==2:raise RuntimeError('interrupted batch')
            return [semantic.unit([1,2,3]) for t in texts]
        self.embedding.side_effect=embed
        with self.assertRaises(RuntimeError):self.manager.run()
        completed=set(calls[0]);retried=[]
        self.embedding.side_effect=lambda texts,prefix:retried.extend(texts) or [semantic.unit([1,2,3]) for t in texts]
        self.manager.run()
        self.assertTrue(retried);self.assertFalse(completed.intersection(retried))
        semantic.Engine(refresh.active_folder(self.root))

    def test_fetch_error_does_not_publish_and_retry_completes(self):
        normal=self.fetch.side_effect
        self.fetch.side_effect=lambda route: (_ for _ in ()).throw(OSError('timeout')) if route=='item/302' else normal(route)
        with self.assertRaises(RuntimeError):self.manager.run()
        self.assertEqual(refresh.active_folder(self.root),self.root)
        self.fetch.side_effect=normal;self.manager.run()
        self.assertEqual(self.manager.status['state'],'complete')

    def test_changed_parent_and_deleted_comment_reindex_correctly(self):
        # First establish the short child and then change its parent in the overlap.
        self.manager.run()
        self.records[300]['text']='I switched to a remote workflow.'
        self.records[302]={'id':302,'deleted':True}
        self.embedding.reset_mock();self.manager.run()
        documents=[text for call in self.embedding.call_args_list for text in call.args[0]]
        self.assertEqual(len(documents),2)
        self.assertTrue(any('Parent context: I switched' in t for t in documents))
        folder=refresh.active_folder(self.root)
        with search.connect(folder/'search.sqlite3') as db:
            self.assertEqual(search.search_items(db,{'q':'SQLite'})['total'],0)
        with sqlite3.connect(folder/'semantic.sqlite3') as db:
            self.assertEqual(db.execute('SELECT count(*) FROM vectors WHERE item_id=302').fetchone()[0],0)

    def test_pending_old_id_is_retried_below_overlap(self):
        hn.save_json(self.root/'refresh-state.json',{'max_id':301,'pending_ids':[99]})
        self.records[99]={'id':99,'type':'comment','parent':1,'time':110,'text':'A delayed comment'}
        self.manager.run()
        self.assertIn(99,context.load_items(refresh.active_folder(self.root)/'items.ndjson'))

    def test_changed_model_fails_without_switching(self):
        with patch.object(semantic,'model_digest',return_value='different'):
            with self.assertRaises(semantic.SemanticUnavailable):self.manager.run()
        self.assertEqual(refresh.active_folder(self.root),self.root)

    def test_single_job_restart_and_reader_publication_gate(self):
        entered=threading.Event();finish=threading.Event()
        def worker():entered.set();finish.wait(3)
        manager=refresh.RefreshManager(self.root,worker=worker)
        self.assertTrue(manager.start());self.assertTrue(entered.wait(1));self.assertFalse(manager.start())
        other=refresh.RefreshManager(self.root,worker=worker)
        self.assertEqual(other.status['state'],'interrupted');self.assertFalse(other.start())
        finish.set();manager.thread.join(2)
        stage=self.root/'.refresh/staged';stage.mkdir()
        (stage/'search.sqlite3').write_bytes((self.root/'search.sqlite3').read_bytes())
        with self.manager.read() as folder:
            thread=threading.Thread(target=self.manager.publish,args=(stage,{'id':'verified'}))
            thread.start()
            with self.manager.condition:
                self.manager.condition.wait_for(lambda:self.manager.publishing,timeout=1)
            self.assertEqual(folder,self.root);self.assertFalse((self.root/'current.json').exists())
        thread.join(2)
        self.assertFalse(thread.is_alive());self.assertEqual(refresh.active_folder(self.root).name,'verified')

    def test_http_refresh_guard_status_and_icon_routes(self):
        with patch.object(refresh.RefreshManager,'start',return_value=False) as start:
            server=search.ThreadingHTTPServer(('127.0.0.1',0),search.handler(self.root/'search.sqlite3'))
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            client=http.client.HTTPConnection('127.0.0.1',server.server_port)
            try:
                client.request('POST','/api/refresh','{}',{'Content-Type':'application/json'})
                response=client.getresponse();self.assertEqual(response.status,403);response.read();start.assert_not_called()
                headers={'Content-Type':'application/json','X-Hacker-Brain-Refresh':'1','Origin':'https://evil.example'}
                client.request('POST','/api/refresh','{}',headers)
                response=client.getresponse();self.assertEqual(response.status,403);response.read();start.assert_not_called()
                headers.pop('Origin');client.request('POST','/api/refresh','{}',headers)
                response=client.getresponse();self.assertEqual(response.status,202);response.read();start.assert_called_once()
                with patch.dict('os.environ',{'HACKER_BRAIN_PUBLIC_ORIGIN':'https://hackerbrain.example'}):
                    headers['Origin']='https://hackerbrain.example'
                    client.request('POST','/api/refresh','{}',headers)
                    response=client.getresponse();self.assertEqual(response.status,202);response.read()
                for path,kind in [('/api/refresh-status','application/json'),('/favicon.ico','image/x-icon'),('/icons/favicon.svg','image/svg+xml'),('/icons/site.webmanifest','application/manifest+json')]:
                    client.request('GET',path);response=client.getresponse();self.assertEqual(response.status,200);self.assertTrue(response.getheader('Content-Type').startswith(kind));response.read()
                client.request('GET','/api/refresh');response=client.getresponse();self.assertEqual(response.status,404);response.read()
            finally:
                client.close();server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
