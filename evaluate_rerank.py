"""Controlled same-pool and expanded-pool comparison against the saved baseline."""
import argparse
import contextlib
import json
import pathlib
import time
import search
import semantic
import rerank


def ranks(ids, gold):
    return {str(n):ids.index(n)+1 if n in ids else None for n in gold}


def evaluate(folder, limit=None, provider='local'):
    cases=json.loads((folder/'semantic-evaluation-cases.json').read_text())
    baseline=json.loads((folder/'semantic-evaluation.json').read_text())
    engine=semantic.Engine(folder, reranker=provider)
    if provider == 'decisions':
        from decisions import DecisionsReranker
        reviewer=DecisionsReranker(engine)
    else:
        reviewer=rerank.Reranker(engine)
    results=[]
    with contextlib.closing(search.connect(folder/'search.sqlite3')) as db:
        for index,case in enumerate(cases[:limit]):
            print(json.dumps({'event':'query','query':case['query']}),flush=True)
            selected,assessments,info=reviewer.run(db,{'q':case['query'],'experience':'1'},
                progress=lambda done,total:print(json.dumps({'event':'progress','done':done,'total':total}),flush=True))
            ids=[r[3]['id'] for r in selected]
            same=[n for n in ids if n in info['baseline_ids']]
            gold=case['relevant_comment_ids']
            controls=[]
            for n in case.get('known_cloud_examples',[]):
                row=db.execute('SELECT * FROM items WHERE id=?',(n,)).fetchone()
                if row:controls.append((0,0,(0,len(row['body'])),row))
            negative_controls,_=reviewer.assess(db,case['query'],controls,info['digest'])
            result={'query':case['query'],'reranking':info,
                    'negative_control_assessments':negative_controls,
                    'negative_controls_injected_into_ranking':False,
                    'baseline_ranks':baseline['queries'][index]['known_example_ranks'],
                    'same_pool_ranks':ranks(same,gold),'expanded_pool_ranks':ranks(ids,gold),
                    'baseline_top20':baseline['queries'][index]['known_examples_in_top20'],
                    'same_pool_top20':sum(n in same[:20] for n in gold),
                    'expanded_pool_top20':sum(n in ids[:20] for n in gold),
                    'pool_coverage':sum(n in ids for n in gold),'gold_count':len(gold),
                    'cloud_example_ranks':ranks(ids,case.get('known_cloud_examples',[])),
                    'top20':[{'id':r[3]['id'],'url':f"https://news.ycombinator.com/item?id={r[3]['id']}",
                              'body':r[3]['body'],'title':r[3]['title'],
                              'assessment':assessments[r[3]['id']]} for r in selected[:20]],
                    'gold_assessments':{str(n):assessments.get(n) for n in gold},
                    'all_assessments':assessments}
            results.append(result)
            report={'source_sha256':engine.config['source_sha256'],'queries':results,
                    'limits':'Small preselected development set, not a held-out or unbiased precision benchmark. Same-pool ranking reuses independent per-comment ratings from the wider run. Reranking cannot find comments absent from its candidate pool. Model ratings and quoted evidence do not verify author claims.'}
            filename='decisions-evaluation.json' if provider=='decisions' else 'rerank-evaluation.json'
            (folder/filename).write_text(json.dumps(report,indent=2)+'\n')
            print(json.dumps({k:v for k,v in result.items() if k not in ['top20','all_assessments']},indent=2),flush=True)
    return report

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=pathlib.Path,required=True);p.add_argument('--limit',type=int)
    p.add_argument('--provider',choices=['local','decisions'],default='local')
    args=p.parse_args();evaluate(args.data,args.limit,args.provider)
