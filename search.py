"""Local SQLite search and context API. No third-party dependencies."""
import argparse
import collections
import contextlib
import datetime
from html.parser import HTMLParser
import json
import pathlib
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from context import load_items
from semantic import Engine, SemanticUnavailable

ROOT = pathlib.Path(__file__).resolve().parent


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1
        if tag in ('p', 'br', 'pre', 'li', 'div'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)
        if tag in ('p', 'pre', 'li', 'div'):
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain(source):
    parser = PlainText()
    parser.feed(source or '')
    return re.sub(r'\n{3,}', '\n\n', ''.join(parser.parts)).strip()


def normalize(text):
    return re.sub(r'\besp\s*32\b', 'esp32', text, flags=re.I)


def experience_signals(text):
    patterns = [
        ('Firsthand wording', r"\b(?:I|we)(?:(?:['’]ve| have|['’]m| am|['’]re| are)(?: been)?)?\s+(?:use|used|using|run|running|ran|built|tried|tested|switched|found|deployed|work|worked|working|pay|paid)\b|\bbeen (?:using|running|working)\b|\bmy (?:setup|workflow|experience|main model)\b|\b(?:I|we)(?:['’]ve| have)? had (?:good luck|success|problems|issues)\b"),
        ('Outcome or limitation', r'\b(?:worked|failed|broke|fixed|solved|saved|faster|slower|reliable|unreliable|limitation|problem|happy|fallback|instead|although|but)\b'),
        ('Concrete detail', r'\$\s*\d|\b\d+\s*(?:years?|months?|days?|hours?|minutes?|ms|gb|tb|users?|requests?)\b'),
    ]
    return [label for label, pattern in patterns if re.search(pattern, text, re.I)]


def build(folder):
    sample = load_items(folder / 'items.ndjson')
    items = {**load_items(folder / 'context.ndjson'), **sample}
    destination = folder / 'search.sqlite3'
    temp = folder / 'search.build.sqlite3'
    temp.unlink(missing_ok=True)
    db = sqlite3.connect(temp)
    db.executescript('''
        CREATE TABLE items(id INTEGER PRIMARY KEY, type TEXT, author TEXT, created INTEGER,
          parent INTEGER, root INTEGER, title TEXT, body TEXT, url TEXT, sample INTEGER,
          deleted INTEGER, dead INTEGER, experience INTEGER, signals TEXT);
        CREATE INDEX parent_idx ON items(parent);
        CREATE INDEX root_idx ON items(root);
        CREATE VIRTUAL TABLE search USING fts5(body, title, author, tokenize='porter unicode61');
        CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT);
    ''')
    roots = {}

    def find_root(n):
        path, seen = [], set()
        while n in items and n not in roots:
            if n in seen:
                raise ValueError('Cycle in ancestor chain: %s' % n)
            seen.add(n)
            path.append(n)
            parent = items[n].get('parent')
            if not parent:
                break
            n = parent
        root = roots.get(n, n) if n in items else None
        for child in path:
            roots[child] = root
        return root

    for n, item in items.items():
        root_id = find_root(n)
        root = items.get(root_id, {})
        title = plain(root.get('title') or item.get('title') or 'Untitled discussion')
        body = plain(item.get('text', ''))
        signals = experience_signals(body)
        score = (3 if 'Firsthand wording' in signals else 0) + len(signals)
        db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (n, item.get('type'), item.get('by', ''), item.get('time'), item.get('parent'),
                    root_id, title, body, item.get('url', ''), int(n in sample),
                    int(bool(item.get('deleted'))), int(bool(item.get('dead'))), score, json.dumps(signals)))
        db.execute('INSERT INTO search(rowid,body,title,author) VALUES(?,?,?,?)',
                   (n, normalize(body), normalize(title), item.get('by', '')))
    summary = json.loads((folder / 'summary.json').read_text())
    context = json.loads((folder / 'context-summary.json').read_text())
    stats = {'sample': summary, 'context': context, 'indexed_items': len(items),
             'searchable_comments': db.execute("SELECT count(*) FROM items WHERE type='comment' AND body!='' AND deleted=0 AND dead=0").fetchone()[0],
             'discussions': len({r for r in roots.values() if r}),
             'firsthand_comments': db.execute("SELECT count(*) FROM items WHERE type='comment' AND experience>=4 AND body!='' AND deleted=0 AND dead=0").fetchone()[0]}
    db.execute('INSERT INTO metadata VALUES(?,?)', ('stats', json.dumps(stats)))
    db.commit()
    db.execute("INSERT INTO search(search) VALUES('optimize')")
    db.commit()
    db.close()
    temp.replace(destination)
    return stats


def connect(path):
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    return db


def serialize(row):
    value = dict(row)
    value['signals'] = json.loads(value['signals'])
    value['hn_url'] = 'https://news.ycombinator.com/item?id=%s' % value['id']
    return value


def terms(query):
    # User input becomes literal FTS tokens/phrases, never raw query operators.
    chunks = [phrase or word for phrase, word in re.findall(r'"([^"\n]+)"|([^\s"]+)', query)]
    return [normalize(chunk).strip() for chunk in chunks if re.search(r'\w', chunk)]


def search_items(db, params):
    query = params.get('q', '').strip()[:200]
    tokens = terms(query)
    offset = max(0, min(int(params.get('offset', 0)), 100000))
    filters = ['i.deleted=0', 'i.dead=0']
    if params.get('kind', 'comment') == 'comment':
        filters += ["i.type='comment'", "i.body!=''"]
    if params.get('older') != '1':
        filters.append('i.sample=1')
    if params.get('experience') == '1':
        filters.append('i.experience>=4')
    values = []
    source = 'items i'
    rank = '0.0'
    if tokens:
        joiner = ' OR ' if params.get('match') == 'any' else ' AND '
        expression = joiner.join('"' + token.replace('"', '""') + '"' for token in tokens)
        source = 'search JOIN items i ON search.rowid=i.id'
        filters.append('search MATCH ?')
        values.append(expression)
        rank = 'bm25(search, 1.0, 0.15, 0.1)'
    where = ' AND '.join(filters)
    total = db.execute('SELECT count(*) FROM %s WHERE %s' % (source, where), values).fetchone()[0]
    order = 'i.created DESC, i.id DESC' if params.get('sort') == 'recent' else ('rank ASC, i.experience DESC, i.id DESC' if tokens else 'i.experience DESC, i.created DESC')
    rows = db.execute('SELECT i.*, %s AS rank FROM %s WHERE %s ORDER BY %s LIMIT 20 OFFSET ?' %
                      (rank, source, where, order), values + [offset]).fetchall()
    return {'query': query, 'total': total, 'offset': offset, 'limit': 20,
            'items': [serialize(row) for row in rows], 'tokens': tokens}


def conversation(db, item_id):
    row = db.execute('SELECT * FROM items WHERE id=?', (item_id,)).fetchone()
    if row is None:
        return None
    item = serialize(row)
    ancestors, seen = [], {item_id}
    parent = item['parent']
    while parent and parent not in seen:
        seen.add(parent)
        found = db.execute('SELECT * FROM items WHERE id=?', (parent,)).fetchone()
        if found is None:
            break
        ancestors.append(serialize(found))
        parent = found['parent']
    replies = db.execute('SELECT * FROM items WHERE parent=? ORDER BY created,id', (item_id,)).fetchall()
    # Find later replies by this author anywhere below the selected comment.
    descendants = db.execute('''WITH RECURSIVE descendants(id) AS (
      SELECT id FROM items WHERE parent=? UNION SELECT i.id FROM items i JOIN descendants d ON i.parent=d.id
    ) SELECT i.* FROM items i JOIN descendants d ON i.id=d.id
      WHERE i.author=? AND i.author!='' AND i.deleted=0 AND i.dead=0 ORDER BY i.created,i.id LIMIT 30''',
      (item_id, item['author'])).fetchall()
    followups = []
    for followup in descendants:
        path, visited = [], set()
        p = followup['parent']
        while p and p != item_id and p not in visited:
            visited.add(p)
            r = db.execute('SELECT * FROM items WHERE id=?', (p,)).fetchone()
            if r is None:
                break
            path.append(serialize(r))
            p = r['parent']
        followups.append({'item': serialize(followup), 'path': list(reversed(path))})
    return {'item': item, 'ancestors': list(reversed(ancestors)),
            'replies': [serialize(r) for r in replies], 'followups': followups,
            'unresolved_parent': parent if parent and parent not in seen else None,
            'scope': 'Collected replies from the 72-hour sample plus older ancestors; not the entire historical thread.'}


def handler(db_path, reranker='local'):
    semantic_engine = None
    engine_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal semantic_engine
            parsed = urlparse(self.path)
            try:
                if parsed.path.startswith('/api/'):
                    with contextlib.closing(connect(db_path)) as db:
                        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                        if parsed.path == '/api/stats':
                            result = json.loads(db.execute("SELECT value FROM metadata WHERE key='stats'").fetchone()[0])
                            result['index_bytes'] = db_path.stat().st_size
                            result['reranker'] = reranker
                        elif parsed.path == '/api/rerank-status':
                            from rerank import status
                            result = status(params.get('q',''))
                        elif parsed.path == '/api/search':
                            if params.get('mode') in ('semantic','experiences') and params.get('q','').strip():
                                with engine_lock:
                                    if semantic_engine is None:
                                        semantic_engine = Engine(db_path.parent, reranker=reranker)
                                result = semantic_engine.search(db,params)
                            else:
                                result = search_items(db, params)
                                result['mode'] = 'keyword'
                        elif parsed.path == '/api/item':
                            result = conversation(db, int(params.get('id', '0')))
                            if result is None:
                                self.send_error(404)
                                return
                        else:
                            self.send_error(404)
                            return
                    payload = json.dumps(result).encode()
                    content_type = 'application/json; charset=utf-8'
                else:
                    assets = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'),
                              '/style.css': ('style.css', 'text/css')}
                    if parsed.path not in assets:
                        self.send_error(404)
                        return
                    name, content_type = assets[parsed.path]
                    payload = (ROOT / 'web' / name).read_bytes()
                    content_type += '; charset=utf-8'
                self.send_response(200)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(payload)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(payload)
            except SemanticUnavailable as exc:
                payload = json.dumps({'error':str(exc)}).encode()
                self.send_response(503)
                self.send_header('Content-Type','application/json')
                self.send_header('Content-Length',str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            except (ValueError, sqlite3.OperationalError) as exc:
                self.send_error(400, 'Invalid search request')

        def log_message(self, fmt, *args):
            # Query text can be private; persist only the route and status.
            print('%s %s' % (urlparse(self.path).path, args[1] if len(args)>1 else ''), flush=True)
    return Handler


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data', type=pathlib.Path, required=True)
    p.add_argument('--build', action='store_true')
    p.add_argument('--serve', action='store_true')
    p.add_argument('--port', type=int, default=8765)
    p.add_argument('--reranker', choices=['local','decisions'], default='local',
                   help='Decisions uses paid OpenAI calls through approved Agent Vault access')
    args = p.parse_args()
    if args.build:
        print(json.dumps(build(args.data)), flush=True)
    if args.serve:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(args.data / 'search.sqlite3', args.reranker))
        print('Hacker Brain: http://127.0.0.1:%s' % args.port, flush=True)
        server.serve_forever()
