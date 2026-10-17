"""Run the preselected sample relevance checks against local semantic search."""
import argparse
import contextlib
import json
import pathlib
import time

import search
import semantic


def evaluate(folder):
    cases=json.loads((folder/'semantic-evaluation-cases.json').read_text())
    engine=semantic.Engine(folder)
    results=[]
    with contextlib.closing(search.connect(folder/'search.sqlite3')) as db:
        for case in cases:
            params={'q':case['query'],'experience':'1'}
            started=time.monotonic()
            answer=engine.search(db,params)
            elapsed=time.monotonic()-started
            # Collect all five pages to measure known-example ranks.
            all_items=list(answer['items'])
            for offset in range(20,answer['total'],20):
                all_items.extend(engine.search(db,{**params,'offset':str(offset)})['items'])
            ids=[item['id'] for item in all_items]
            ranks={str(n):ids.index(n)+1 if n in ids else None for n in case['relevant_comment_ids']}
            results.append({'query':case['query'],'firsthand_filter':True,
                'keyword_all_words_results':search.search_items(db,params)['total'],
                'semantic_results':answer['total'],'seconds':round(elapsed,3),
                'known_example_ranks':ranks,
                'known_examples_in_top20':sum(rank is not None and rank<=20 for rank in ranks.values()),
                'known_example_count':len(ranks),
                'cloud_example_ranks':{str(n):ids.index(n)+1 if n in ids else None for n in case.get('known_cloud_examples',[])},
                'top20':[{'id':item['id'],'title':item['title'],'author':item['author'],
                          'url':item['hn_url'],'body':item['body'],
                          'similarity':item['semantic_similarity']} for item in all_items[:20]],
                'notes':case['notes']})
    report={'model':engine.config,'coverage':engine.stats,'queries':results,
            'index_bytes':(folder/'semantic.sqlite3').stat().st_size,
            'limits':'Small preselected retrieval check, not corpus-wide recall, factual verification, or an unbiased precision benchmark.'}
    (folder/'semantic-evaluation.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({**report,'queries':[{**r,'top20':r['top20'][:5]} for r in results]}),flush=True)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=pathlib.Path,required=True)
    args=p.parse_args()
    evaluate(args.data)
