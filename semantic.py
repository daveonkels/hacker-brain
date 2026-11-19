"""Local Nomic embeddings, resumable indexing, and exact vector retrieval."""
import argparse
import array
import contextlib
import ctypes
import ctypes.util
import hashlib
import heapq
import json
import math
import os
import pathlib
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request

MODEL = 'nomic-embed-text:latest'
API = os.environ.get('HACKER_BRAIN_OLLAMA_URL', 'http://127.0.0.1:11434').rstrip('/') + '/api/'
VERSION = 'nomic-comment-passages-v1'
STOP_WORDS = set('a an the i we you they it this that these those how what which why when where who are is was were be been being do does did have has had can could should would will may might people someone anyone others using use used uses daily their your our my in on at of for to from with and or as by about into actually practical experiences experience examples example workflows workflow tell me'.split())


def lexical_support(db,query):
    """Support exact concepts without requiring every word in the question."""
    tokens=list(dict.fromkeys(t.lower() for t in re.findall(r'\w+',query) if len(t)>1 and t.lower() not in STOP_WORDS))
    if not tokens:
        return {}
    expression=' OR '.join('"'+t+'"' for t in tokens[:20])
    try:
        rows=db.execute('SELECT rowid FROM search WHERE search MATCH ? ORDER BY bm25(search,1.0,0.15,0.1) LIMIT 300', (expression,)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r[0]:0.06/(1+rank/20) for rank,r in enumerate(rows)}


class SemanticUnavailable(RuntimeError):
    pass


def request(route, data=None, timeout=120):
    req = urllib.request.Request(API + route,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SemanticUnavailable('Local semantic search is unavailable. Start Ollama with the indexed Nomic model, or switch to Keywords.') from exc


def model_digest():
    models = request('tags')['models']
    for model in models:
        if model['name'] == MODEL:
            return model['digest']
    raise SemanticUnavailable('The indexed Nomic model is not installed. Switch to Keywords.')


def unit(vector):
    if not vector or not all(math.isfinite(x) for x in vector):
        raise SemanticUnavailable('The local model returned an invalid embedding.')
    norm = math.sqrt(sum(x*x for x in vector))
    if norm == 0:
        raise SemanticUnavailable('The local model returned an empty embedding.')
    return array.array('f', (x/norm for x in vector))


def embed(texts, prefix):
    data = request('embed', {'model': MODEL, 'input': [prefix + t for t in texts],
                            'truncate': False, 'keep_alive': '30m'})
    vectors = data.get('embeddings', [])
    if len(vectors) != len(texts):
        raise SemanticUnavailable('The local model returned an incomplete embedding batch.')
    return [unit(v) for v in vectors]


def passages(body, size=1800, overlap=240):
    # Character ranges cover the full body, including its final qualification.
    start = 0
    while start < len(body):
        end = min(len(body), start + size)
        if end < len(body):
            split = body.rfind(' ', start + size//2, end)
            if split > start:
                end = split
        yield start, end, body[start:end]
        if end == len(body):
            break
        start = max(start+1, end-overlap)


def diversify(ranked, limit=100):
    """Small repeat-discussion/author penalties keep the first page varied."""
    pool = list(ranked[:max(500,limit)])
    chosen, roots, authors = [], {}, {}
    while pool and len(chosen)<limit:
        def value(candidate):
            score, _, _, row = candidate
            return score - min(0.06,0.015*roots.get(row['root'],0)) - min(0.03,0.01*authors.get(row['author'],0))
        index = max(range(len(pool)),key=lambda i:(value(pool[i]),pool[i][3]['id']))
        item = pool.pop(index)
        chosen.append(item)
        row = item[3]
        roots[row['root']] = roots.get(row['root'],0)+1
        authors[row['author']] = authors.get(row['author'],0)+1
    return chosen


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def document_tasks(source, changed=None):
    """Lazy passage stream; at most one comment is materialized at a time."""
    predicate="i.type='comment' AND i.body!='' AND i.dead=0 AND i.deleted=0"
    def connection():
        db=sqlite3.connect(source);db.row_factory=sqlite3.Row
        db.execute('PRAGMA cache_size=-4096')
        if changed is not None:
            db.execute('ATTACH DATABASE ? AS changes',(str(changed),))
        return db
    if changed is not None:predicate+=' AND i.id IN (SELECT id FROM changes.refresh_changed)'
    with contextlib.closing(connection()) as db:
        count=db.execute('SELECT count(*) FROM items i WHERE '+predicate).fetchone()[0]
    def tasks():
        with contextlib.closing(connection()) as db:
            rows=db.execute("SELECT i.*,p.body AS parent_body FROM items i LEFT JOIN items p ON i.parent=p.id WHERE "+predicate+" ORDER BY i.id")
            for row in rows:
                for part,(start,end,text) in enumerate(passages(row['body'])):
                    document='Discussion: '+row['title'][:240]+'\n'
                    if len(row['body'])<240 and row['parent_body']:
                        document+='Parent context: '+row['parent_body'][:320]+'\n'
                    document+='Comment: '+text
                    yield row['id'],part,start,end,document
    return tasks(),count


def seed_unchanged(base, folder):
    """Keep unchanged vectors; invalidate only changed model inputs, using streams."""
    from corpus import copy_database
    with contextlib.closing(sqlite3.connect(base/'semantic.sqlite3')) as old:
        meta=dict(old.execute('SELECT key,value FROM meta'))
    config=json.loads(meta['config'])
    if meta.get('complete')!='true' or config!={'version':VERSION,'model':MODEL,'digest':model_digest(),'source_sha256':file_hash(base/'search.sqlite3')}:
        raise SemanticUnavailable('The existing semantic index must match the collection and installed model.')
    with contextlib.closing(sqlite3.connect(folder/'search.sqlite3')) as search:
        incremental=bool(search.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_changed'").fetchone())
    changed=folder/'search.sqlite3' if incremental else None
    copy_database(base/'semantic.sqlite3',folder/'semantic.sqlite3')
    config['source_sha256']=file_hash(folder/'search.sqlite3')
    with contextlib.closing(sqlite3.connect(folder/'semantic.sqlite3')) as db:
        old=iter(document_tasks(base/'search.sqlite3',changed)[0]);new=iter(document_tasks(folder/'search.sqlite3',changed)[0])
        before=next(old,None);after=next(new,None)
        while before is not None:
            while after is not None and after[:2]<before[:2]:after=next(new,None)
            if after!=before:db.execute('DELETE FROM vectors WHERE item_id=? AND part=?',before[:2])
            before=next(old,None)
        db.execute("INSERT OR REPLACE INTO meta VALUES('config',?)",(json.dumps(config),))
        db.execute("INSERT OR REPLACE INTO meta VALUES('complete','false')")
        db.commit()
        return db.execute('SELECT count(*) FROM vectors').fetchone()[0]


def build(folder, batch_size=64, progress=None):
    source = folder / 'search.sqlite3'
    source_hash = file_hash(source)
    config = {'version': VERSION, 'model': MODEL, 'digest': model_digest(), 'source_sha256': source_hash}
    index = sqlite3.connect(folder / 'semantic.sqlite3')
    try:
        index.executescript('''CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE IF NOT EXISTS vectors(item_id INTEGER, part INTEGER, start INTEGER, end INTEGER,
            embedding BLOB NOT NULL, PRIMARY KEY(item_id,part));''')
        previous = index.execute("SELECT value FROM meta WHERE key='config'").fetchone()
        if previous and json.loads(previous[0]) != config:
            raise SemanticUnavailable('Semantic index belongs to a different corpus/model. Use a new sample directory or explicitly rebuild its semantic index.')
        index.execute("INSERT OR IGNORE INTO meta VALUES('config',?)", (json.dumps(config),))
        index.execute("INSERT OR REPLACE INTO meta VALUES('complete','false')")
        index.commit()
        # Materialize only missing tasks on disk, not a corpus-sized Python list.
        index.execute('PRAGMA temp_store=FILE')
        index.execute('PRAGMA cache_size=-4096')
        index.execute('CREATE TEMP TABLE pending(item_id INTEGER,part INTEGER,start INTEGER,end INTEGER,document TEXT)')
        tasks,comment_count=document_tasks(source)
        total=0
        for task in tasks:
            total+=1
            if not index.execute('SELECT 1 FROM vectors WHERE item_id=? AND part=?',task[:2]).fetchone():
                index.execute('INSERT INTO pending VALUES(?,?,?,?,?)',task)
        pending_count=index.execute('SELECT count(*) FROM pending').fetchone()[0]
        index.commit()
        started=time.monotonic();done=0
        print(json.dumps({'event':'start','comments':comment_count,'passages':total,'pending':pending_count}),flush=True)
        if progress:progress(0,pending_count)
        cursor=index.execute('SELECT item_id,part,start,end,document FROM pending ORDER BY item_id,part')
        while True:
            batch=cursor.fetchmany(batch_size)
            if not batch:break
            vectors=embed([task[4] for task in batch],'search_document: ')
            dimensions=len(vectors[0])
            if any(len(v)!=dimensions for v in vectors):raise SemanticUnavailable('Inconsistent embedding dimensions.')
            index.executemany('INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)',
                [(*task[:4],vector.tobytes()) for task,vector in zip(batch,vectors)])
            index.commit();done+=len(batch)
            if progress:progress(done,pending_count)
        count=index.execute('SELECT count(*) FROM vectors').fetchone()[0]
        if count!=total:raise SemanticUnavailable('Index coverage mismatch.')
        if model_digest() != config['digest']:
            raise SemanticUnavailable('The local model changed during indexing; index remains incomplete.')
        blob = index.execute('SELECT embedding FROM vectors LIMIT 1').fetchone()
        stats = {'comments':comment_count,'passages':count,'dimensions':len(blob[0])//4 if blob else 0,
                 'model':MODEL,'seconds_this_run':round(time.monotonic()-started,1)}
        index.execute("INSERT OR REPLACE INTO meta VALUES('stats',?)", (json.dumps(stats),))
        index.execute("INSERT OR REPLACE INTO meta VALUES('complete','true')")
        index.commit()
        print(json.dumps({'event':'complete',**stats}), flush=True)
        return stats
    finally:
        index.close()


class Matrix:
    """Use an existing system CBLAS library, with a portable Python fallback."""
    def __init__(self, values, rows, dimensions):
        self.values, self.rows, self.dimensions = values, rows, dimensions
        self.pointer = (ctypes.c_float * len(values)).from_buffer(values)
        self.gemv = None
        for name in ('Accelerate', 'blas'):
            path = ctypes.util.find_library(name)
            if not path:
                continue
            try:
                self.library = ctypes.CDLL(path)
                self.gemv = self.library.cblas_sgemv
            except (OSError, AttributeError):
                continue
            float_ptr = ctypes.POINTER(ctypes.c_float)
            self.gemv.argtypes = [ctypes.c_int]*4 + [ctypes.c_float, float_ptr, ctypes.c_int,
                float_ptr, ctypes.c_int, ctypes.c_float, float_ptr, ctypes.c_int]
            self.gemv.restype = None
            break

    def dot(self, query):
        if len(query) != self.dimensions:
            raise SemanticUnavailable('Embedding model dimensions changed. Switch to Keywords.')
        if not self.gemv:
            return [sum(self.values[i*self.dimensions+j]*query[j] for j in range(self.dimensions)) for i in range(self.rows)]
        q = (ctypes.c_float * len(query)).from_buffer(query)
        output = (ctypes.c_float * self.rows)()
        self.gemv(101,111,self.rows,self.dimensions,1.0,self.pointer,self.dimensions,q,1,0.0,output,1)
        return list(output)


class Engine:
    def __init__(self, folder, reranker='local'):
        if reranker not in ('local','decisions'):
            raise ValueError('Unknown reranker')
        self.reranker = reranker
        path = folder / 'semantic.sqlite3'
        if not path.exists():
            raise SemanticUnavailable('Semantic indexing has not finished. Switch to Keywords for now.')
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            meta = dict(db.execute('SELECT key,value FROM meta'))
            if meta.get('complete') != 'true':
                raise SemanticUnavailable('Semantic indexing has not finished. Switch to Keywords for now.')
            self.config = json.loads(meta['config'])
            self.stats = json.loads(meta['stats'])
        if self.config.get('version') != VERSION:
            raise SemanticUnavailable('Semantic preparation changed. Switch to Keywords until the index is rebuilt.')
        if file_hash(folder/'search.sqlite3') != self.config['source_sha256']:
            raise SemanticUnavailable('The sample changed after semantic indexing. Switch to Keywords until it is rebuilt.')
        self.folder = folder
        self.vector_path = path
        self.lock = threading.Lock()
        self.last_model_check = 0
        self._cached_query = None
        self._cached_scores = None

    def similarities(self, query):
        # Keep one query on this engine only; class-level caches retain old
        # corpus matrices after a refresh publishes a replacement engine.
        if query == self._cached_query:
            return self._cached_scores
        vector = embed([query], 'search_query: ')[0]
        best = {}
        # Stream the exact dot products in small batches. The growing corpus
        # must not require a second, resident copy of every embedding.
        with contextlib.closing(sqlite3.connect(self.vector_path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
            rows = db.execute('SELECT item_id,start,end,embedding FROM vectors ORDER BY item_id,part')
            while batch := rows.fetchmany(2048):
                values = array.array('f')
                for _, _, _, blob in batch:
                    values.frombytes(blob)
                matrix = Matrix(values, len(batch), self.stats['dimensions'])
                for (n, start, end, _), score in zip(batch, matrix.dot(vector)):
                    if n not in best or score > best[n][0]:
                        best[n] = (score, (start, end))
        self._cached_query, self._cached_scores = query, best
        return best

    def candidates(self, db, params):
        query = params.get('q','').strip()[:500]
        with self.lock:
            if time.monotonic()-self.last_model_check > 60:
                if model_digest() != self.config['digest']:
                    self._cached_query, self._cached_scores = None, None
                    raise SemanticUnavailable('The embedding model changed. Switch to Keywords until the index is rebuilt.')
                self.last_model_check = time.monotonic()
            scores = self.similarities(query)
        filters = ["type='comment'", "body!=''", 'dead=0', 'deleted=0']
        if params.get('older') != '1': filters.append('sample=1')
        if params.get('experience') == '1': filters.append('experience>=4')
        rows = db.execute('SELECT * FROM items WHERE '+' AND '.join(filters))
        lexical = lexical_support(db,query)
        def candidates():
            for row in rows:
                if row['id'] not in scores:continue
                score,span=scores[row['id']]
                adjusted=score+(0.025 if row['experience']>=4 else 0)+lexical.get(row['id'],0)
                if len(row['body'])<100:adjusted-=0.04
                if row['body'].count('?')>=2:adjusted-=0.04
                yield adjusted,score,span,row
        return heapq.nlargest(500,candidates(),key=lambda r:(r[0],r[3]['id']))

    def search(self, db, params):
        from search import serialize
        query = params.get('q','').strip()[:500]
        offset = max(0,min(int(params.get('offset',0)),100))
        assessments, reranking = {}, None
        if params.get('mode') == 'experiences':
            from rerank import Reranker
            if self.reranker == 'decisions':
                from decisions import DecisionsReranker
                reviewer = DecisionsReranker(self)
            else:
                reviewer = Reranker(self)
            selected, assessments, reranking = reviewer.run(db, params)
            ranked = selected
            selected = selected[:100]
        else:
            ranked = self.candidates(db, params)
            selected = diversify(ranked)
        if params.get('sort') == 'recent':
            selected.sort(key=lambda r:(-(r[3]['created'] or 0),-r[3]['id']))
        items = []
        for rank, score, span, row in selected[offset:offset+20]:
            item = serialize(row)
            item['semantic_similarity'] = round(score,4)
            if assessments:
                item['assessment'] = assessments[row['id']]
            item['passage_start'], item['passage_end'] = span
            item['semantic_excerpt'] = ('…' if span[0] else '') + item['body'][span[0]:span[1]] + ('…' if span[1]<len(item['body']) else '')
            items.append(item)
        return {'query':query,'total':len(selected),'offset':offset,'limit':20,'items':items,'tokens':[],
                'mode':'experiences' if reranking else 'semantic','candidate_count':len(ranked),
                'reranking':reranking,
                'note':('Model assessments prioritize relevant firsthand actions and outcomes. Assessments are not verification.' if reranking else 'Meaning plus keyword support, favoring firsthand detail and discussion variety. Relatedness is not verification.')}


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=pathlib.Path,required=True)
    p.add_argument('--batch-size',type=int,default=64)
    args=p.parse_args()
    build(args.data,args.batch_size)
