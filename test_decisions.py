import copy
import json
import pathlib
import tempfile
import types
import unittest
from unittest.mock import patch

import decisions
from semantic import SemanticUnavailable


def response(request, overrides=None):
    values = {'kind':'workflow','firsthand':'3','detail':'2','execution':'local',
              'evidence':'1','relevance':'3', **(overrides or {})}
    return {'model':decisions.MODEL,'usage':{'input_tokens':100},'answers':[
        {'type':'choice','name':q['name'],'choice':values[q['name']], 'confidence':1.,
         'probabilities':[{'value':o['value'],'probability':float(o['value']==values[q['name']])}
                          for o in q['choices']]} for q in request['questions']]}


class DB:
    def execute(self,*args): return self
    def fetchone(self): return ('My parent used a different cloud service.',)


class DecisionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = types.SimpleNamespace(folder=pathlib.Path(self.temp.name))
        self.doc = {'id':1,'title':'Models','body':'I run an offline model to sort my files.',
                    'parent_context':'My parent used a different cloud service.'}
        self.row = {**self.doc,'parent':2}
        self.candidates = [(0.5,0.5,(0,len(self.doc['body'])),self.row)]
        self.calls = []
        def transport(requests):
            self.calls.extend(requests)
            return [{'response':response(r)} for r in requests]
        self.reviewer = decisions.DecisionsReranker(self.engine,transport)

    def test_classification_reused_across_new_questions_and_restarts(self):
        first,cached=self.reviewer.assess(DB(),'local daily workflows',self.candidates)
        self.assertEqual(len(self.calls),2);self.assertEqual(cached,0)
        self.assertEqual(first[1]['evidence'],self.doc['body'])
        same,cached=self.reviewer.assess(DB(),'local daily workflows',self.candidates)
        self.assertEqual(first,same);self.assertEqual(cached,1);self.assertEqual(len(self.calls),2)
        reopened=decisions.DecisionsReranker(self.engine,self.reviewer.transport)
        reopened.assess(DB(),'different question',self.candidates)
        self.assertEqual(len(self.calls),3)
        self.assertEqual([q['name'] for q in self.calls[-1]['questions']],['relevance'])
        self.assertEqual(reopened.usage['classification_cached'],1)

    def test_source_changes_invalidate_both_caches(self):
        self.reviewer.assess(DB(),'query',self.candidates)
        changed=[(.5,.5,(0,20),{**self.row,'body':'Actually I use cloud inference.'})]
        self.reviewer.assess(DB(),'query',changed)
        self.assertEqual(len(self.calls),4)

    def test_invalid_responses_refusals_and_distributions_fail(self):
        request,_=decisions.classification_request(self.doc)
        good=response(request)
        changes=[lambda r:r['answers'].pop(),
                 lambda r:r['answers'].append(r['answers'][0]),
                 lambda r:r['answers'][0].update(type='refusal'),
                 lambda r:r['answers'][0].update(choice='invented'),
                 lambda r:r['answers'][0].update(confidence=float('nan')),
                 lambda r:r['answers'][0]['probabilities'].pop(),
                 lambda r:r['answers'][0]['probabilities'][0].update(probability=.4),
                 lambda r:r.update(model='unrelated-model')]
        for change in changes:
            bad=copy.deepcopy(good);change(bad)
            with self.assertRaises(SemanticUnavailable):decisions.validate_response(bad,request)

    def test_missing_evidence_does_not_receive_firsthand_bonus(self):
        self.reviewer.transport=lambda reqs:[{'response':response(r,{'evidence':'0'})} for r in reqs]
        ratings,_=self.reviewer.assess(DB(),'query',self.candidates)
        self.assertEqual(ratings[1]['firsthand'],1)
        self.assertEqual(ratings[1]['evidence'],'')

    def test_successful_work_survives_partial_failure(self):
        self.reviewer.transport=lambda reqs:[{'response':response(reqs[0])},{'error_status':403}]
        with self.assertRaisesRegex(SemanticUnavailable,'403'):
            self.reviewer.assess(DB(),'query',self.candidates)
        calls=[]
        def recover(requests):
            calls.extend(requests);return [{'response':response(r)} for r in requests]
        self.reviewer.transport=recover
        self.reviewer.assess(DB(),'query',self.candidates)
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0]['questions'][0]['name'],'relevance')

    def test_requests_are_bounded_and_progress_counts_decisions(self):
        candidates=[(.5,.5,(0,10),{**self.row,'id':i}) for i in range(10)]
        sizes=[];progress=[]
        def transport(requests):
            sizes.append(len(requests));return [{'response':response(r)} for r in requests]
        self.reviewer.transport=transport
        self.reviewer.assess(DB(),'query',candidates,progress=lambda d,t:progress.append((d,t)))
        self.assertEqual(sizes,[20]);self.assertEqual(progress[-1],(20,20))
        self.assertEqual(self.reviewer.usage['input_tokens'],2000)

    def test_transport_uses_vault_and_never_exposes_stderr(self):
        with patch.object(decisions.subprocess,'run',return_value=types.SimpleNamespace(
                returncode=1,stdout='',stderr='sensitive detail')) as run:
            with self.assertRaises(SemanticUnavailable) as caught:
                decisions.vault_transport([{'test':'public source'}])
            self.assertNotIn('sensitive',str(caught.exception))
            self.assertEqual(run.call_args.args[0][1],str(decisions.VAULT_RUNNER))

    def test_remote_worker_http_errors_and_fixed_destination(self):
        import base64, zlib
        class Executor:
            def __init__(self,**kwargs):self.asserted=kwargs
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def map(self,fn,items):return map(fn,items)
        payload=base64.b64encode(zlib.compress(json.dumps([{'test':'public'}]).encode())).decode()
        ok=types.SimpleNamespace(returncode=0,stdout='{"answers": []}\n200')
        with patch('sys.argv',['worker',payload]), patch('concurrent.futures.ThreadPoolExecutor',Executor), \
             patch('subprocess.run',return_value=ok) as call, patch('builtins.print') as output:
            exec(decisions.REMOTE,{})
            argv=call.call_args.args[0]
            self.assertIn(decisions.ENDPOINT,argv)
            self.assertIn('Authorization: Bearer '+decisions.TOKEN,argv)
            self.assertIn('response',json.loads(output.call_args.args[0])[0])

    def test_ranking_path_preserves_source_links_and_provider(self):
        with patch.object(self.reviewer,'pool',return_value=(self.candidates,self.candidates,[])), \
             patch.object(decisions.rerank,'digest',return_value='local-digest'):
            selected,ratings,info=self.reviewer.run(DB(),{'q':'query'})
            self.assertEqual(selected[0][3]['id'],1)
            self.assertEqual(info['provider'],'OpenAI Decisions')
            self.assertEqual(info['baseline_ids'],[1])
            self.assertAlmostEqual(info['estimated_input_cost_usd'],.00002)

    def test_shortlist_is_bounded_stable_and_does_not_generate_queries(self):
        candidates=[(.8-i*.001,.8,(0,10),{**self.row,'id':i,'root':i,'author':str(i)}) for i in range(120)]
        self.engine.config={'source_sha256':'fixture-v1'}
        calls=[]
        def candidates_for(db,params):
            calls.append(params)
            return candidates
        self.engine.candidates=candidates_for
        with patch.object(decisions.rerank,'chat',side_effect=AssertionError('No generative expansion')):
            first,_,_=self.reviewer.pool(DB(),{'q':'local models','experience':'1'})
            self.assertEqual(len(first),48)
            # A newly learned negative label must not move pagination boundaries.
            doc={**self.doc,'id':first[0][3]['id']}
            req,_=decisions.classification_request(doc)
            self.reviewer.save(self.reviewer.key([decisions.VERSION,'classification',req]),
                               response(req,{'kind':'question','firsthand':'0','evidence':'0'}))
            again,_,_=self.reviewer.pool(DB(),{'q':'local models','experience':'1'})
            self.assertEqual([r[3]['id'] for r in first],[r[3]['id'] for r in again])
            self.assertEqual(len(calls),6) # Five initial searches, one on reuse.
            broader,_,_=self.reviewer.pool(DB(),{'q':'local models','experience':'1','depth':'deep'})
            self.assertEqual(len(broader),100)
            self.reviewer.pool(DB(),{'q':'local models','experience':'1','older':'1'})
            self.assertEqual(calls[-1]['older'],'1')

    def test_transport_rejects_unbounded_work_before_execution(self):
        with patch.object(decisions.subprocess,'run') as call:
            with self.assertRaises(ValueError): decisions.vault_transport([{}]*(decisions.WAVE_SIZE+1))
            call.assert_not_called()

    def test_access_failure_stops_scheduling_additional_waves(self):
        candidates=[(.5,.5,(0,10),{**self.row,'id':i}) for i in range(30)]
        calls=[]
        def fail(requests):
            calls.append(requests)
            return [{'error_status':403} for _ in requests]
        self.reviewer.transport=fail
        with self.assertRaises(SemanticUnavailable):
            self.reviewer.assess(DB(),'query',candidates)
        self.assertEqual(len(calls),decisions.TRANSPORT_WORKERS)


if __name__ == '__main__': unittest.main()
