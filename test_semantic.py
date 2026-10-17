import array
import hashlib
import json
import math
import pathlib
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import semantic
import search


class SemanticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = pathlib.Path(self.temp.name)
        db = sqlite3.connect(self.folder/'search.sqlite3')
        db.executescript('''CREATE TABLE items(id INTEGER PRIMARY KEY, type TEXT, author TEXT, created INTEGER,
          parent INTEGER, root INTEGER, title TEXT, body TEXT, url TEXT, sample INTEGER,
          deleted INTEGER, dead INTEGER, experience INTEGER, signals TEXT);
          CREATE VIRTUAL TABLE search USING fts5(body,title,author,tokenize='porter unicode61');''')
        rows = [
            (1,'I run an offline assistant to sort my files.',1,0,0,4),
            (2,'I use an air-gapped coding helper.',0,0,0,4),
            (3,'I enjoy gardening.',1,0,0,4),
            (4,'I run an offline assistant.',1,0,1,4),
            (5,'I run an offline assistant.',1,1,0,4),
            (6,'Perhaps people could run offline assistants.',1,0,0,0),
            (7,'opening '*400+'Important qualification at the end.',1,0,0,4),
        ]
        for n,body,sample,deleted,dead,experience in rows:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (n,'comment','alice',n,None,n,'Discussion',body,'',sample,deleted,dead,experience,'[]'))
            db.execute('INSERT INTO search(rowid,body,title,author) VALUES(?,?,?,?)',(n,body,'Discussion','alice'))
        db.commit();db.close()

    def tearDown(self):
        self.temp.cleanup()

    def build(self):
        def embeddings(texts,prefix):
            self.assertEqual(prefix,'search_document: ')
            return [semantic.unit([0,1,0] if 'gardening' in t else [1,0,0]) for t in texts]
        with patch.object(semantic,'model_digest',return_value='test-digest'), patch.object(semantic,'embed',side_effect=embeddings):
            return semantic.build(self.folder,batch_size=3)

    def test_passages_cover_every_character_and_final_qualification(self):
        text='a sentence. '*700+'Final exception: this fails offline.'
        ranges=list(semantic.passages(text))
        covered=set()
        for start,end,part in ranges:
            self.assertEqual(part,text[start:end]);covered.update(range(start,end))
        self.assertEqual(covered,set(range(len(text))))
        self.assertIn('fails offline',ranges[-1][2])

    def test_native_dot_matches_reference(self):
        matrix=semantic.Matrix(array.array('f',[1,2,3,4,5,6]),2,3)
        query=array.array('f',[0.5,0.25,0.125])
        actual=matrix.dot(query)
        for a,b in zip(actual,[1.375,4.0]):self.assertAlmostEqual(a,b,places=5)
        with self.assertRaises(semantic.SemanticUnavailable):matrix.dot(array.array('f',[1]))

    def test_discussion_diversity_without_dropping_results(self):
        rows=[(0.70,0.70,(0,10),{'id':1,'root':10,'author':'a'}),
              (0.699,0.699,(0,10),{'id':2,'root':10,'author':'a'}),
              (0.69,0.69,(0,10),{'id':3,'root':20,'author':'b'})]
        result=semantic.diversify(rows,limit=3)
        self.assertEqual([r[3]['id'] for r in result],[1,3,2])

    def test_unit_rejects_nonfinite_and_zero(self):
        for v in [[0,0],[float('nan'),1],[float('inf'),1]]:
            with self.assertRaises(semantic.SemanticUnavailable):semantic.unit(v)
        self.assertAlmostEqual(sum(x*x for x in semantic.unit([3,4])),1)

    def test_keyword_support_keeps_terms_literal(self):
        with search.connect(self.folder/'search.sqlite3') as db:
            support=semantic.lexical_support(db,'How are people using an offline assistant?')
            self.assertIn(1,support)
            self.assertNotIn(3,support)
            self.assertEqual(semantic.lexical_support(db,'how are people using'),{})

    def test_index_resume_does_not_reembed(self):
        self.build()
        with patch.object(semantic,'model_digest',return_value='test-digest'), patch.object(semantic,'embed') as embed:
            semantic.build(self.folder)
            embed.assert_not_called()

    def test_semantic_filtering_deduplication_and_original_links(self):
        self.build();engine=semantic.Engine(self.folder)
        with search.connect(self.folder/'search.sqlite3') as db, patch.object(semantic,'model_digest',return_value='test-digest'), patch.object(semantic,'embed',return_value=[array.array('f',[1,0,0])]) as embed:
            r=engine.search(db,{'q':'How are people using local models?','experience':'1'})
            ids=[i['id'] for i in r['items']]
            self.assertIn(1,ids);self.assertNotIn(2,ids);self.assertNotIn(4,ids);self.assertNotIn(5,ids);self.assertNotIn(6,ids)
            self.assertEqual(ids.count(7),1)
            self.assertEqual(next(i for i in r['items'] if i['id']==1)['hn_url'],'https://news.ycombinator.com/item?id=1')
            embed.assert_called_with(['How are people using local models?'],'search_query: ')
            older=engine.search(db,{'q':'How are people using local models?','older':'1'})
            self.assertIn(2,[i['id'] for i in older['items']])

    def test_incomplete_index_and_stale_corpus_fail_explicitly(self):
        with self.assertRaises(semantic.SemanticUnavailable):semantic.Engine(self.folder)
        self.build()
        with sqlite3.connect(self.folder/'semantic.sqlite3') as db:
            db.execute("UPDATE meta SET value='false' WHERE key='complete'")
        with self.assertRaises(semantic.SemanticUnavailable):semantic.Engine(self.folder)
        with sqlite3.connect(self.folder/'semantic.sqlite3') as db:
            db.execute("UPDATE meta SET value='true' WHERE key='complete'")
        with sqlite3.connect(self.folder/'search.sqlite3') as db:
            db.execute("UPDATE items SET body='Changed' WHERE id=1")
        with self.assertRaises(semantic.SemanticUnavailable):semantic.Engine(self.folder)

    def test_changed_model_is_not_silently_used(self):
        self.build();engine=semantic.Engine(self.folder)
        with search.connect(self.folder/'search.sqlite3') as db, patch.object(semantic,'model_digest',return_value='different-model'):
            with self.assertRaises(semantic.SemanticUnavailable):engine.search(db,{'q':'test'})


if __name__=='__main__':unittest.main()
