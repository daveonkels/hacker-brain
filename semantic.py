"""Local Nomic embeddings, resumable indexing, and exact vector retrieval."""
import argparse
import array
import contextlib
import ctypes
import ctypes.util
import functools
import hashlib
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


def document_tasks(source):
    with contextlib.closing(sqlite3.connect(source)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT i.*, p.body AS parent_body FROM items i LEFT JOIN items p ON i.parent=p.id WHERE i.type='comment' AND i.body!='' AND i.dead=0 AND i.deleted=0 ORDER BY i.id").fetchall()
    tasks = []
    for row in rows:
        for part, (start, end, text) in enumerate(passages(row['body'])):
            document = 'Discussion: ' + row['title'][:240] + '\n'
            # Short replies need some context; avoid overwhelming substantive comments.
            if len(row['body']) < 240 and row['parent_body']:
                document += 'Parent context: ' + row['parent_body'][:320] + '\n'
            document += 'Comment: ' + text
            tasks.append((row['id'], part, start, end, document))
    return tasks, len(rows)


def seed_unchanged(base, folder):
    """Copy only vectors whose exact model input and model identity still match."""
    with contextlib.closing(sqlite3.connect(base/'semantic.sqlite3')) as old:
        meta = dict(old.execute('SELECT key,value FROM meta'))
    config = json.loads(meta['config'])
    if meta.get('complete') != 'true' or config != {
        'version': VERSION, 'model': MODEL, 'digest': model_digest(),
        'source_sha256': hashlib.sha256((base/'search.sqlite3').read_bytes()).hexdigest()
    }:
        raise SemanticUnavailable('The existing semantic index must match the collection and installed model.')
    old_tasks = {t[:2]:t[2:] for t in document_tasks(base/'search.sqlite3')[0]}
    keep = [t[:2] for t in document_tasks(folder/'search.sqlite3')[0] if old_tasks.get(t[:2]) == t[2:]]
    config['source_sha256'] = hashlib.sha256((folder/'search.sqlite3').read_bytes()).hexdigest()
    # An interrupted seed is safe to recreate; prepared.json is written afterward.
    (folder/'semantic.sqlite3').unlink(missing_ok=True)
    with contextlib.closing(sqlite3.connect(folder/'semantic.sqlite3')) as db:
        db.executescript("""CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
          CREATE TABLE vectors(item_id INTEGER, part INTEGER, start INTEGER, end INTEGER,
            embedding BLOB NOT NULL, PRIMARY KEY(item_id,part));
          CREATE TEMP TABLE keep(item_id INTEGER, part INTEGER, PRIMARY KEY(item_id,part));""")
        db.executemany('INSERT INTO keep VALUES(?,?)', keep)
        db.execute('ATTACH DATABASE ? AS previous', (str(base/'semantic.sqlite3'),))
        db.execute('INSERT INTO vectors SELECT v.* FROM previous.vectors v JOIN keep k USING(item_id,part)')
        db.execute("INSERT INTO meta VALUES('config',?)", (json.dumps(config),))
        db.execute("INSERT INTO meta VALUES('complete','false')")
        db.commit()
    return len(keep)


def build(folder, batch_size=64, progress=None):
    source = folder / 'search.sqlite3'
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
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
        complete_parts = set(index.execute('SELECT item_id,part FROM vectors'))
        tasks, comment_count = document_tasks(source)
        pending = [task for task in tasks if (task[0], task[1]) not in complete_parts]
        started = time.monotonic()
        print(json.dumps({'event':'start', 'comments':comment_count, 'passages':len(tasks), 'pending':len(pending)}), flush=True)
        if progress: progress(0, len(pending))
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset:offset+batch_size]
            vectors = embed([task[4] for task in batch], 'search_document: ')
            dimensions = len(vectors[0])
            if any(len(v) != dimensions for v in vectors):
                raise SemanticUnavailable('Inconsistent embedding dimensions.')
            index.executemany('INSERT OR REPLACE INTO vectors VALUES(?,?,?,?,?)',
                [(*task[:4], vector.tobytes()) for task, vector in zip(batch, vectors)])
            index.commit()
            done = min(offset+len(batch), len(pending))
            if progress: progress(done, len(pending))
            if offset == 0 or done % (batch_size*10) == 0 or done == len(pending):
                print(json.dumps({'event':'progress','done':len(complete_parts)+done,'total':len(tasks),
                                  'seconds':round(time.monotonic()-started,1)}), flush=True)
        count = index.execute('SELECT count(*) FROM vectors').fetchone()[0]
        if count != len(tasks):
            raise SemanticUnavailable('Index coverage mismatch.')
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
            vectors = db.execute('SELECT item_id,start,end,embedding FROM vectors ORDER BY item_id,part').fetchall()
        if self.config.get('version') != VERSION:
            raise SemanticUnavailable('Semantic preparation changed. Switch to Keywords until the index is rebuilt.')
        if hashlib.sha256((folder/'search.sqlite3').read_bytes()).hexdigest() != self.config['source_sha256']:
            raise SemanticUnavailable('The sample changed after semantic indexing. Switch to Keywords until it is rebuilt.')
        self.folder = folder
        self.ids, self.ranges, values = [], [], array.array('f')
        for n, start, end, blob in vectors:
            self.ids.append(n); self.ranges.append((start,end)); values.frombytes(blob)
        self.matrix = Matrix(values,len(vectors),self.stats['dimensions'])
        self.lock = threading.Lock()
        self.last_model_check = 0

    @functools.lru_cache(maxsize=8)
    def similarities(self, query):
        vector = embed([query], 'search_query: ')[0]
        similarities = self.matrix.dot(vector)
        best = {}
        for n, span, score in zip(self.ids, self.ranges, similarities):
            if n not in best or score > best[n][0]:
                best[n] = (score, span)
        return best

    def candidates(self, db, params):
        query = params.get('q','').strip()[:500]
        with self.lock:
            if time.monotonic()-self.last_model_check > 60:
                if model_digest() != self.config['digest']:
                    self.similarities.cache_clear()
                    raise SemanticUnavailable('The embedding model changed. Switch to Keywords until the index is rebuilt.')
                self.last_model_check = time.monotonic()
            scores = self.similarities(query)
        filters = ["type='comment'", "body!=''", 'dead=0', 'deleted=0']
        if params.get('older') != '1': filters.append('sample=1')
        if params.get('experience') == '1': filters.append('experience>=4')
        rows = db.execute('SELECT * FROM items WHERE '+' AND '.join(filters)).fetchall()
        lexical = lexical_support(db,query)
        ranked = []
        for row in rows:
            if row['id'] not in scores: continue
            score, span = scores[row['id']]
            # Wording/detail bonuses are retrieval aids, never factual-confidence scores.
            adjusted = score + (0.025 if row['experience']>=4 else 0)
            adjusted += lexical.get(row['id'],0)
            if len(row['body']) < 100:
                adjusted -= 0.04
            if row['body'].count('?') >= 2:
                adjusted -= 0.04
            ranked.append((adjusted, score, span, row))
        ranked.sort(key=lambda r:(-r[0],-r[3]['id']))
        return ranked

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
