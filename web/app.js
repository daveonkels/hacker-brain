const $ = s => document.querySelector(s);
let rerankerProvider = 'local';
let offset = 0, total = 0, requestId = 0, currentTokens = [], contextRequest = 0;
const number = n => n.toLocaleString();
function el(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
function date(t) { return t ? new Date(t * 1000).toLocaleString(undefined, {month:'short',day:'numeric',year:'numeric',hour:'numeric',minute:'2-digit'}) : 'Date unavailable'; }
function link(text, url, cls) { const n = el('a', cls, text); n.href = url; n.target = '_blank'; n.rel = 'noopener noreferrer'; return n; }
function textWithHighlights(node, text) {
  node.replaceChildren();
  const escaped = currentTokens.flatMap(x => x.split(/\s+/)).filter(Boolean).map(x => x.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
  if (!escaped.length) { node.textContent = text; return; }
  const regex = new RegExp('(' + escaped.join('|') + ')', 'gi'); let last = 0;
  for (const match of text.matchAll(regex)) { node.append(document.createTextNode(text.slice(last, match.index)), el('mark', '', match[0])); last = match.index + match[0].length; }
  node.append(document.createTextNode(text.slice(last)));
}
async function get(url) { const response = await fetch(url); if (!response.ok) {let message='Could not load the sample. Check that the local server is running.';try {const error=await response.json();message=error.error||message;}catch{}throw new Error(message);} return response.json(); }
function params() { return new URLSearchParams({q:$('#query').value.trim(), mode:$('#mode').value, depth:$('#depth').value, experience:$('#experience').checked?'1':'0', older:$('#older').checked?'1':'0', match:$('#match').value,sort:$('#sort').value,offset}); }
function updateFilterCount() {
  const count = Number($('#experience').checked) + Number($('#older').checked)
    + Number($('#mode').value==='experiences' && $('#depth').value==='deep')
    + Number($('#mode').value==='keyword' && $('#match').value==='any');
  $('#filter-count').textContent = count ? `${count} selected` : '';
}
function modeUI() {
  const mode=$('#mode').value;
  $('#depth-label').hidden=mode!=='experiences'||rerankerProvider!=='decisions';
  $('#match-label').hidden=mode!=='keyword'; $('#match').disabled=mode!=='keyword';
  document.querySelectorAll('[data-mode-choice]').forEach(button=>button.setAttribute('aria-pressed',String(button.dataset.modeChoice===mode)));
  $('#mode-hint').textContent=mode==='experiences' ? (rerankerProvider==='decisions'
    ? 'AI ranks relevant comments, with firsthand experience first. New questions usually take several seconds.'
    : 'Relevant comments, with firsthand experience ranked first. Local ranking can take several minutes.')
    : mode==='semantic' ? 'Find related ideas, even when the wording is different. Fast, local search.'
    : 'Find comments containing your words. Put a phrase in quotation marks to match it together.';
  updateFilterCount();
}
function commentPreview(item) {
  const evidence=item.assessment?.evidence;
  if(evidence && item.body.includes(evidence)) {
    const at=item.body.indexOf(evidence);
    let start=Math.max(0,at-100), end=Math.min(item.body.length,at+evidence.length+280);
    // Show surrounding source text; never turn a selected fragment into a summary.
    const contextStart=Math.max(0,at-220), before=item.body.slice(contextStart,at);
    const boundaries=[...before.matchAll(/(?:[.!?]\s+|\n+)/g)];
    if(boundaries.length){const last=boundaries[boundaries.length-1];start=contextStart+last.index+last[0].length;}
    else if(start) {const space=item.body.indexOf(' ',start);if(space>=0 && space<at)start=space+1;}
    if(end<item.body.length) {const space=item.body.lastIndexOf(' ',end);if(space>at+evidence.length)end=space;}
    return (start?'…':'')+item.body.slice(start,end)+(end<item.body.length?'…':'');
  }
  return item.semantic_excerpt || item.body.slice(0,700)+(item.body.length>700?'…':'');
}
function renderCard(item) {
  const card=el('article','card');card.dataset.id=item.id;
  card.setAttribute('aria-label',`Comment by ${item.author||'unknown author'}`);
  const meta=el('div','comment-meta');
  meta.append(el('span','comment-author',item.author||'Unknown author'));
  const posted=el('time','',date(item.created));if(item.created)posted.dateTime=new Date(item.created*1000).toISOString();meta.append(posted);
  if(!item.sample)meta.append(el('span','badge older','Older comment'));
  if(item.assessment) {
    const a=item.assessment, supported=a.firsthand>=2 && ['workflow','trial','problem','build'].includes(a.kind);
    const labels={workflow:'Reported workflow',trial:'Reported trial',problem:'Reported problem',build:'Author-built tool',question:'Question',opinion:'Opinion',proposal:'Proposed use',other:'Other'};
    const label=!supported && ['workflow','trial','problem','build'].includes(a.kind)?'Limited experience':labels[a.kind];
    const badge=el('span','badge'+(supported?'':' assessment-muted'),label);
    badge.title='AI-assessed contribution type, not a fact check.';badge.setAttribute('aria-label',label+', AI-assessed');meta.append(badge);
  }
  const body=el('blockquote','body comment-quote preview'), preview=commentPreview(item);
  const showText=text=>{
    const selected=item.assessment?.evidence;
    const evidence=selected?(selected.match(/^.{30,180}?[.!?](?:\s|$)/s)?.[0]?.trim()||selected):null, at=evidence?text.indexOf(evidence):-1;
    if(at>=0)body.replaceChildren(document.createTextNode(text.slice(0,at)),el('mark','evidence-mark',evidence),document.createTextNode(text.slice(at+evidence.length)));
    else textWithHighlights(body,text);
  };
  showText(preview);
  const discussion=el('div','discussion-line');discussion.append(link(item.title,'https://news.ycombinator.com/item?id='+item.root,'card-title'));
  const actions=el('div','card-actions'), context=el('button','context-button','Read conversation →');context.type='button';context.addEventListener('click',()=>showContext(item.id));
  const expand=el('button','expand','Full comment');expand.type='button';expand.setAttribute('aria-expanded','false');
  expand.addEventListener('click',()=>{const collapsed=body.classList.toggle('preview');showText(collapsed?preview:item.body);expand.textContent=collapsed?'Full comment':'Collapse comment';expand.setAttribute('aria-expanded',String(!collapsed));});
  actions.append(context,expand,link('View on HN ↗',item.hn_url,'source-link'));
  card.append(discussion,meta,body,actions);return card;
}
let searchBusy=false, readingResults=false;
function setBusy(busy) {
  searchBusy=busy;
  for(const selector of ['#submit-search','#apply-filters','#sort','[data-mode-choice]','[data-query]'])
    document.querySelectorAll(selector).forEach(control=>control.disabled=busy);
  $('#search-progress').hidden=!busy;
  $('#results').setAttribute('aria-busy',String(busy));
}
function renderResults(data) {
  total=data.total;currentTokens=data.tokens;
  $('#results').replaceChildren(...data.items.map(renderCard));
  if(!data.items.length)$('#results').append(el('div','empty',data.mode==='keyword'?'No comments match those words. Try fewer words or “Any word” in Filters.':'No comments match these filters. Try including older comments or removing the firsthand wording filter.'));
  $('#result-title').textContent=data.query?`“${data.query}”`:'Explore the comments';
  $('#result-count').textContent=`${number(total)} ${data.mode==='keyword'?'matching':data.reranking?'ranked':'related'} comments`;
  $('#search-note').textContent='Individual comments, not verified answers. Read the conversation behind a claim.';
  if(data.reranking) {
    const r=data.reranking;
    $('#search-details').textContent=`${r.provider||'Local model'} assessed ${r.assessed} candidates${r.retrieved?' from '+r.retrieved+' retrieved':''}. ${r.classification_cached===undefined?r.cached+' saved assessments':r.classification_cached+' saved comment labels'} reused. Completed in ${r.seconds.toFixed(1)} seconds. AI labels describe contributions, not their accuracy; relevance and context can be misjudged.`;
  } else $('#search-details').textContent='This search runs locally. Relatedness does not establish correctness. Read the conversation and original source before relying on a claim.';
  $('#page-label').textContent=total?`${number(offset+1)}–${number(Math.min(offset+20,total))} of ${number(total)}`:'No results';
  $('#previous').disabled=offset===0;$('#next').disabled=offset+20>=total;
}
async function search() {
  if(searchBusy)return;
  const id=++requestId,p=params();modeUI();$('#error').hidden=true;$('#rank-ready').hidden=true;
  $('#show-ranked').onclick=null;readingResults=false;
  document.body.classList.toggle('has-query',Boolean(p.get('q')));
  history.replaceState(null,'','/?'+p);
  $('#previous').disabled=true;$('#next').disabled=true;
  if(!p.get('q')) {
    $('#result-title').textContent='Where do you want to dig in?';$('#result-count').textContent='';$('#search-note').textContent='';$('#page-label').textContent='';
    const empty=el('div','empty','Start with a question worth exploring.');empty.append(el('small','','Ask about a technique, compare tools, or see what others learned. The example searches above are a good place to start.'));
    $('#results').replaceChildren(empty);return;
  }
  setBusy(true);$('#results').replaceChildren();$('#result-title').textContent=`“${p.get('q')}”`;$('#result-count').textContent='Finding relevant comments…';$('#page-label').textContent='';
  $('#search-note').textContent='';$('#search-details').textContent='Search in progress. Results retain their original text and source links.';
  let finished=false,initialShown=false;
  const started=Date.now();
  const progressTimer=p.get('mode')==='experiences'?setInterval(()=>{
    if(finished||id!==requestId)return;
    $('#result-count').textContent=initialShown?'Closest matches · ranking in progress':'Finding and ranking comments…';
    if(Date.now()-started>20000)$('#search-note').textContent=initialShown?'Ranking is taking longer than usual. The closest matches below are ready to read.':'Ranking is taking longer than usual. You can edit your next question while this finishes.';
  },2500):null;
  if(p.get('mode')==='experiences') {
    const initial=new URLSearchParams(p);initial.set('mode','semantic');
    get('/api/search?'+initial).then(data=>{
      if(finished||id!==requestId)return;
      currentTokens=data.tokens;$('#results').replaceChildren(...data.items.map(renderCard));initialShown=true;
      $('#result-count').textContent='Closest matches · ranking in progress';
      $('#search-note').textContent='You can start reading. We’re checking relevance and firsthand experience.';
      $('#page-label').textContent='Initial matches';
    }).catch(()=>{});
  }
  try {
    const data=await get('/api/search?'+p);if(id!==requestId)return;
    if(initialShown && (readingResults || window.scrollY>$('#results').offsetTop-100)) {
      $('#rank-ready').hidden=false;$('#result-count').textContent='Ranked results ready';
      $('#search-note').textContent='Showing the initial matches until you choose to switch.';
      $('#show-ranked').onclick=()=>{if(id!==requestId)return;$('#rank-ready').hidden=true;renderResults(data);$('#result-title').scrollIntoView({block:'start',behavior:'smooth'});};
    } else renderResults(data);
  } catch(error) {
    if(id!==requestId)return;
    $('#error').textContent=initialShown?'Ranking didn’t finish. You can read the closest matches below, or press Search to try again.':error.message;
    $('#error').hidden=false;$('#result-count').textContent=initialShown?'Closest matches · ranking unavailable':'Search unavailable';
    $('#search-note').textContent='';$('#search-details').textContent=error.message;
  } finally {finished=true;if(progressTimer)clearInterval(progressTimer);if(id===requestId)setBusy(false);}
}
function contextItem(item, selected=false) {
  const node=el('div','context-item'+(selected?' selected':''));
  node.append(el('div','meta',`${item.author || 'Unknown author'} · ${date(item.created)}${!item.sample?' · Older context':''}`));
  if(item.deleted || item.dead) node.append(el('div','unavailable',item.deleted?'[Deleted comment]':'[Dead comment hidden]'));
  else { const body=el('div','body');textWithHighlights(body,item.body || item.title);node.append(body); }
  node.append(link('Original ↗',item.hn_url,'meta'));return node;
}
async function showContext(id) {
  const version=++contextRequest, dialog=$('#context-dialog'), content=$('#context-content');
  content.replaceChildren(el('p','','Loading conversation…')); if(!dialog.open)dialog.showModal();
  try { const data=await get('/api/item?id='+id);if(version!==contextRequest)return;content.replaceChildren();
    content.append(el('div','context-title',data.item.title),el('div','scope-note',data.scope));
    const root=data.ancestors.find(i=>!i.parent);
    if(root?.url && /^https?:\/\//i.test(root.url)) content.append(link('Read the linked article ↗',root.url));
    if(data.ancestors.length){ content.append(el('div','context-label','What led to this comment'));data.ancestors.forEach(i=>content.append(contextItem(i))); }
    content.append(el('div','context-label','Matching comment'),contextItem(data.item,true));
    if(data.followups.length){content.append(el('div','context-label','Later replies by the same author'));data.followups.forEach(f=>{f.path.forEach(p=>content.append(contextItem(p)));content.append(contextItem(f.item,true));});}
    content.append(el('div','context-label',`Direct replies in this collection · ${data.replies.length}`));
    if(!data.replies.length)content.append(el('p','scope-note','No direct replies in this collection. The live thread may contain more.'));
    data.replies.forEach(i=>{const n=contextItem(i);const b=el('button','context-button','Explore this reply →');b.addEventListener('click',()=>showContext(i.id));n.append(b);content.append(n);});
    if(data.unresolved_parent)content.append(el('p','scope-note','An older parent could not be retrieved.'));
    dialog.scrollTop=0;
  } catch(error){content.replaceChildren(el('p','',error.message));}
}
$('#close-context').addEventListener('click',()=>$('#context-dialog').close());
$('#results').addEventListener('pointerdown',()=>{readingResults=true;});
$('#results').addEventListener('click',()=>{readingResults=true;});
$('#results').addEventListener('focusin',()=>{readingResults=true;});
$('#search-form').addEventListener('submit',event=>{event.preventDefault();if(searchBusy)return;offset=0;search();});
$('#apply-filters').addEventListener('click',()=>{offset=0;$('#filter-panel').open=false;search();});
$('#reset-filters').addEventListener('click',()=>{$('#experience').checked=false;$('#older').checked=false;$('#depth').value='fast';$('#match').value='all';updateFilterCount();});
for(const name of ['experience','older','match','depth'])$('#'+name).addEventListener('change',updateFilterCount);
$('#sort').addEventListener('change',()=>{offset=0;search();});
document.querySelectorAll('[data-mode-choice]').forEach(button=>button.addEventListener('click',()=>{$('#mode').value=button.dataset.modeChoice;offset=0;search();}));
document.querySelectorAll('[data-query]').forEach(button=>button.addEventListener('click',()=>{$('#query').value=button.dataset.query;$('#mode').value=button.dataset.mode||'semantic';$('#experience').checked=true;offset=0;search();}));
$('#previous').addEventListener('click',()=>{offset=Math.max(0,offset-20);search();$('#search-form').scrollIntoView({behavior:'smooth'});});
$('#next').addEventListener('click',()=>{offset+=20;search();$('#search-form').scrollIntoView({behavior:'smooth'});});
document.addEventListener('keydown',event=>{if(event.key==='/' && !$('#context-dialog').open && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) && !document.activeElement.isContentEditable){event.preventDefault();$('#query').focus();}});
async function loadStats(){
  try {
    const data=await get('/api/stats');rerankerProvider=data.reranker||'local';
    const sampleWindow=data.sample.window;
    const dateFormat=new Intl.DateTimeFormat('en-US',{month:'short',day:'numeric',year:'numeric',timeZone:'UTC'});
    const windowLabel=dateFormat.formatRange(new Date(sampleWindow.start_inclusive),new Date(sampleWindow.end_exclusive));
    $('#stats').textContent=`${number(data.sample.counts.comment)} comments · ${number(data.discussions)} discussions · ${windowLabel}`;
    $('#context-stats').textContent=`${number(data.context.context_items)} older items were collected to connect parent conversations. Deleted and dead comments are hidden from search.`;
    $('#privacy-note').textContent=rerankerProvider==='decisions'?'Insights mode sends your question and candidate comments to OpenAI through Agent Vault. Meaning and Keywords search locally.':'All search modes run locally. Insights mode uses a local model to assess comments.';
    $('#window').textContent=windowLabel.toUpperCase();
    $('#coverage').textContent=`Manually refreshed collection, with older conversation context.`;
  } catch(error){$('#stats').textContent='Collection information unavailable';}
}

let refreshTimer, lastRefreshCompletion;
async function pollRefresh(){
  clearTimeout(refreshTimer);
  const button=$('#refresh-hn');
  try {
    const status=await get('/api/refresh-status');
    const active=status.state==='running';
    button.disabled=active;
    button.textContent=active?'Refreshing…':['failed','interrupted'].includes(status.state)?'↻ Retry refresh':'↻ Refresh HN';
    $('#collected-through').textContent='Collected through '+new Date(status.through).toLocaleString();
    let message=status.state==='idle'?'Fetch new comments and stories when you need them.':status.message;
    if(active && status.total)message+=` ${number(status.done)} / ${number(status.total)} ${status.phase==='embedding'?'passages':'items'}`;
    if(status.state==='complete'){
      message=`${number(status.new_items)} new items added. Run your search again to explore the update.`;
      if(lastRefreshCompletion!==status.completed_at){lastRefreshCompletion=status.completed_at;await loadStats();}
    }
    $('#refresh-status').textContent=message;
    if(active)refreshTimer=setTimeout(pollRefresh,2000);
  } catch(error){
    button.disabled=false;
    $('#refresh-status').textContent='Could not check refresh progress. Reconnecting…';
    refreshTimer=setTimeout(pollRefresh,5000);
  }
}
$('#refresh-hn').addEventListener('click',async()=>{
  $('#refresh-hn').disabled=true;
  $('#refresh-status').textContent='Starting refresh…';
  try {
    const response=await fetch('/api/refresh',{method:'POST',headers:{'Content-Type':'application/json','X-Hacker-Brain-Refresh':'1'},body:'{}'});
    if(!response.ok)throw new Error('Unable to start refresh. Try again.');
    await pollRefresh();
  } catch(error){$('#refresh-hn').disabled=false;$('#refresh-status').textContent=error.message;}
});
async function init(){
  const p=new URLSearchParams(location.search);$('#query').value=p.get('q')||'';$('#experience').checked=p.get('experience')==='1';$('#older').checked=p.get('older')==='1';$('#match').value=p.get('match')==='any'?'any':'all';$('#sort').value=p.get('sort')==='recent'?'recent':'relevance';offset=Math.max(0,Number(p.get('offset'))||0);
  $('#depth').value=p.get('depth')==='deep'?'deep':'fast';
  $('#mode').value=['keyword','semantic','experiences'].includes(p.get('mode'))?p.get('mode'):'experiences';modeUI();
  await loadStats();
  pollRefresh();
  search();
}
init();
