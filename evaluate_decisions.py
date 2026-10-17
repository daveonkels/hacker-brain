"""Measure a Decisions search and its repeat, preserving source-linked results."""
import argparse
import contextlib
import json
from pathlib import Path

import decisions
import search
import semantic


def evaluate(folder, query, output):
    engine = semantic.Engine(folder, reranker='decisions')
    reviewer = decisions.DecisionsReranker(engine)
    with contextlib.closing(search.connect(folder/'search.sqlite3')) as db:
        ranked, assessments, info = reviewer.run(db, {'q':query,'experience':'1'},
            progress=lambda d,t:print(json.dumps({'done':d,'total':t}),flush=True))
        report = {'query':query, 'reranking':info, 'ids':[r[3]['id'] for r in ranked],
            'top20':[{'id':r[3]['id'], 'body':r[3]['body'], 'title':r[3]['title'],
                      'url':f"https://news.ycombinator.com/item?id={r[3]['id']}",
                      'assessment':assessments[r[3]['id']]} for r in ranked[:20]],
            'limits':'Development measurement, not a representative popularity or accuracy estimate. Check cache counts when interpreting first-search latency.'}
        _, _, report['cached_reranking'] = reviewer.run(db, {'q':query,'experience':'1'})
    output.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({k:{a:b for a,b in report[k].items() if a not in ('baseline_ids','expansions')}
                      for k in ('reranking','cached_reranking')},indent=2),flush=True)
    return report


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',type=Path,required=True)
    p.add_argument('--query',required=True)
    p.add_argument('--output',type=Path,required=True)
    args = p.parse_args()
    evaluate(args.data,args.query,args.output)
