import copy
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

import rerank
from semantic import SemanticUnavailable


class RerankTests(unittest.TestCase):
    def setUp(self):
        self.docs=[{'id':1,'title':'Local models','body':'I run an offline assistant for sorting my files.','parent_context':'My parent used a cloud model.'}]
        self.rating={'id':1,'relevance':3,'firsthand':3,'detail':2,'kind':'workflow',
                     'evidence':'I run an offline assistant'}

    def test_validates_exact_evidence_and_complete_unique_ids(self):
        self.assertEqual(rerank.validate({'results':[self.rating]},self.docs)[1],self.rating)
        for rows in [[],[self.rating,self.rating],[{**self.rating,'id':2}]]:
            with self.assertRaises(SemanticUnavailable):rerank.validate({'results':rows},self.docs)

    def test_rejects_fabricated_or_parent_only_evidence_and_bad_scores(self):
        invalid=[{'evidence':'My parent used a cloud model.'},{'evidence':'I run a helpful offline assistant'},
                 {'evidence':''},{'relevance':True},{'relevance':4},{'detail':-1},{'kind':'proven'}]
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(SemanticUnavailable):
                rerank.validate({'results':[{**self.rating,**change}]},self.docs)

    def test_questions_and_constraint_mismatches_cannot_beat_concrete_experiences(self):
        concrete={**self.rating,'relevance':2}
        question={**self.rating,'kind':'question'}
        cloud={**self.rating,'relevance':1}
        opinion={**self.rating,'kind':'opinion'}
        for weak in [question,cloud,opinion]:
            self.assertGreater(rerank.score(concrete,.5),rerank.score(weak,.9))

    def test_cache_survives_reopen_and_keys_change_with_model_prompt_query_content(self):
        with tempfile.TemporaryDirectory() as temp:
            engine=types.SimpleNamespace(folder=pathlib.Path(temp),config={'source_sha256':'corpus1'})
            reviewer=rerank.Reranker(engine)
            parts=[rerank.VERSION,'digest1',rerank.PROMPT,'query',self.docs]
            key=reviewer.key(parts);reviewer.save(key,self.rating)
            self.assertEqual(rerank.Reranker(engine).read(key),self.rating)
            for i in range(len(parts)):
                changed=copy.deepcopy(parts);changed[i]='changed'
                self.assertIsNone(reviewer.read(reviewer.key(changed)))

    def test_failed_or_incomplete_model_output_is_explicit(self):
        for response in [{'done':True,'done_reason':'length','message':{'content':'{}'}},
                         {'done':True,'message':{'content':'not JSON'}}]:
            with patch.object(rerank,'request',return_value=response),self.assertRaises(SemanticUnavailable):
                rerank.chat('instructions',{}, {})

    def test_numbered_evidence_is_copied_from_source_and_out_of_range_rejected(self):
        rating={k:v for k,v in self.rating.items() if k!='evidence'}
        spans={1:[self.docs[0]['body']]}
        decoded=rerank.decode({**{k:v for k,v in self.rating.items() if k!='evidence'},'evidence_index':1},self.docs,spans)
        self.assertEqual(decoded[1]['evidence'],self.docs[0]['body'])
        unsupported=rerank.decode({**rating,'evidence_index':0},self.docs,spans)
        self.assertEqual(unsupported[1]['firsthand'],1)
        for index in [-1,2,True]:
            with self.assertRaises(SemanticUnavailable):
                rerank.decode({**rating,'evidence_index':index},self.docs,spans)

    def test_duplicate_work_fails_fast(self):
        reviewer=rerank.Reranker.__new__(rerank.Reranker)
        with rerank.LOCK:
            with self.assertRaisesRegex(SemanticUnavailable,'already reviewing'):
                reviewer.run(None,{'q':'question'})

    def test_cached_ratings_are_revalidated_and_do_not_call_model(self):
        class DB:
            def execute(self,*args):return self
            def fetchone(self):return None
        with tempfile.TemporaryDirectory() as temp:
            engine=types.SimpleNamespace(folder=pathlib.Path(temp),config={'source_sha256':'corpus1'})
            reviewer=rerank.Reranker(engine)
            row={**self.docs[0],'parent':None}
            with patch.object(rerank,'chat',return_value={**{k:v for k,v in self.rating.items() if k!='evidence'},'evidence_index':1}) as model:
                first,cached=reviewer.assess(DB(),'query',[(.5,.5,(0,10),row)],'digest')
                second,cached2=reviewer.assess(DB(),'query',[(.5,.5,(0,10),row)],'digest')
                self.assertEqual(first,second);self.assertEqual(cached,0);self.assertEqual(cached2,1)
                self.assertEqual(model.call_count,1)

if __name__=='__main__':unittest.main()
