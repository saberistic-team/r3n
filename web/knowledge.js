// Knowledge Q&A keeps its own view state so research polling never loses a draft.
const knowledgeViews = new Map();

function knowledgeView(id = state.project?.id) {
  if (!knowledgeViews.has(id)) knowledgeViews.set(id, { draft:'', scope:[id], data:null, version:'', selected:null, evidence:null, sending:false, error:null, graph:false });
  return knowledgeViews.get(id);
}

async function refreshKnowledge(id) {
  const view = knowledgeView(id);
  const sequence = (view.fetchSequence || 0) + 1;
  view.fetchSequence = sequence;
  const data = await api(`/projects/${id}/knowledge`);
  if (sequence !== view.fetchSequence) return;
  const version = JSON.stringify(data);
  if (version === view.version) return;
  view.data = data; view.version = version;
  if (state.project?.id === id && state.tab === 'ask') renderKnowledge($('#tab-content'), state.project);
}

function passageButtons(evidence, answer) {
  return evidence.filter((e,i,all)=>all.findIndex(x=>x.passage_id===e.passage_id)===i).map(e => {
    const index = answer.retrieval.passages.findIndex(p=>p.id===e.passage_id);
    return `<button class="note-ref" data-qa-evidence="${esc(e.passage_id)}" data-qa-answer="${answer.id}">↗ Passage ${index+1}</button>`;
  }).join('');
}

function renderKnowledge(target, project) {
  const view = knowledgeView(project.id);
  const focus = document.activeElement?.id === 'knowledge-question';
  const selection = focus ? [document.activeElement.selectionStart,document.activeElement.selectionEnd] : null;
  const scopeOpen = $('#knowledge-scope')?.open;
  const status = view.data?.index;
  const answers = view.data?.answers || [];
  const selected = answers.find(a=>a.id===view.selected) || answers[0];
  const active = answers.some(a=>['queued','retrieving','answering'].includes(a.status));
  target.innerHTML = `<section class="knowledge-intro"><span class="eyebrow">YOUR COLLECTED KNOWLEDGE</span><h2>Ask what you’ve learned.</h2><p>Find answers across your saved research, with the passages and connections behind them.</p></section>
    <div class="knowledge-health"><div><span class="status-dot ${status?.pending ? 'live' : status?.errors.length ? 'warn' : ''}"></span><span>${status ? `${status.notes} notes · ${status.passages} passages · ${status.relations} relationships` : 'Loading knowledge index…'}</span></div><span>${status ? `${status.embedded}/${status.passages} searchable by meaning` : ''}</span></div>
    ${status?.pending ? `<p class="helper">Indexing in the background: ${status.graph_indexed}/${status.passages} passages mapped. You can ask now; results will reflect what’s ready.</p>` : ''}
    ${status?.errors.length ? `<div class="error-banner">${status.errors.map(esc).join('<br>')}<br><button class="text-button" data-qa-reindex>Retry indexing ↻</button></div>` : ''}
    <form class="qa-composer" id="knowledge-form"><label for="knowledge-question">ASK YOUR RESEARCH</label><textarea id="knowledge-question" name="question" rows="3" minlength="3" maxlength="1000" placeholder="What does our research say about…?" required>${esc(view.draft)}</textarea><div class="qa-compose-bottom"><details id="knowledge-scope" ${scopeOpen ? 'open' : ''}><summary>Search ${view.scope.length} investigation${view.scope.length===1?'':'s'} ▾</summary><div class="scope-options">${state.projects.map(p=>`<label><input type="checkbox" data-qa-scope="${p.id}" ${view.scope.includes(p.id)?'checked':''}><span>${esc(p.question)}${p.id===project.id?' <small>Current</small>':''}</span></label>`).join('')}</div></details><button type="submit" class="button" ${view.sending || active?'disabled':''}>${view.sending || active?'Answering…':'Ask knowledge'} <span>↗</span></button></div></form>
    <p class="qa-local-note">Local Qwen · Saved research only · No Brave request</p>
    ${view.error ? `<div class="error-banner">${esc(view.error)}</div>`:''}
    ${answers.length ? `<div class="qa-history-heading"><span class="small-heading">KNOWLEDGE ANSWERS</span><select id="knowledge-history" class="cycle-select" aria-label="Saved knowledge answer">${answers.map(a=>`<option value="${a.id}" ${selected?.id===a.id?'selected':''}>${esc(a.question.length>65?a.question.slice(0,62)+'…':a.question)}</option>`).join('')}</select></div>${renderKnowledgeAnswer(selected,view)}` : '<div class="qa-empty"><span>⌕</span><p>Ask a specific question, compare findings, or look for gaps.<br>Your answers and supporting passages will stay in this notebook.</p></div>'}`;
  if (focus && selection) { $('#knowledge-question').focus(); $('#knowledge-question').setSelectionRange(...selection); }
}

function renderKnowledgeAnswer(answer, view) {
  if (!answer) return '';
  const stage = {queued:'Waiting for local Qwen',retrieving:'Finding relevant passages and connections',answering:'Qwen is composing an answer from the evidence'};
  if (stage[answer.status]) return `<article class="panel"><div class="panel-top"><h2>${esc(answer.question)}</h2></div><div class="qa-working"><span class="status-dot live"></span>${stage[answer.status]}…</div><p class="helper">Interactive questions take priority after any model call already in progress finishes.</p></article>`;
  if (answer.status === 'error') return `<article class="panel"><h2 class="note-title">${esc(answer.question)}</h2><p class="error-banner">${esc(answer.error)}</p><button class="button secondary small" data-qa-retry="${answer.id}">Ask again</button></article>`;
  const result = answer.result;
  const cited = new Set(result.claims.flatMap(c=>c.evidence.map(e=>e.passage_id)));
  const urls = new Set(answer.retrieval.passages.filter(p=>cited.has(p.id)).flatMap(p=>p.sources.map(s=>s.url)));
  const passage = answer.retrieval.passages.find(p=>p.id===view.evidence);
  return `<article class="panel qa-answer"><div class="panel-top"><span class="eyebrow">ANSWER FROM YOUR RESEARCH</span><span class="badge ${result.status!=='answered'?'gold':''}">${result.status==='answered'?'Supported passages found':result.status==='partial'?'Partial answer':'Insufficient evidence'}</span></div><h2 class="note-title">${esc(answer.question)}</h2><p class="qa-meta">${cited.size} cited passages · ${urls.size} linked source URLs · ${answer.scope.length} investigation${answer.scope.length===1?'':'s'} · ${esc(answer.retrieval.mode)}</p>
    ${result.status==='insufficient'?'<p class="prose">The selected research does not provide enough evidence to answer this question.</p>':''}
    ${result.claims.map(c=>`<div class="qa-claim"><p>${esc(c.text)}</p><div class="finding-meta">${passageButtons(c.evidence,answer)}</div></div>`).join('')}
    ${result.caveats.length?`<h3 class="small-heading">LIMITS & UNCERTAINTY</h3><ul class="gap-list">${result.caveats.map(c=>`<li>${esc(c)}</li>`).join('')}</ul>`:''}
    ${result.gaps.length?`<h3 class="small-heading">RESEARCH GAPS</h3><div class="qa-gaps">${result.gaps.map((g,i)=>`<div><p>${esc(g)}</p><button class="button secondary small" data-qa-gap="${i}" data-qa-answer="${answer.id}">＋ Research this gap</button></div>`).join('')}</div>`:''}
    ${answer.warnings.map(w=>`<p class="helper">${esc(w)}</p>`).join('')}
    <div class="qa-evidence-footer"><span>Quotes refer to saved Brave answers; linked webpages have not been independently fetched.</span><button class="text-button" data-qa-graph>${view.graph?'Hide':'View'} evidence map ↗</button></div>
    ${view.graph?knowledgeGraph(answer):''}
    ${passage?renderPassage(passage,answer):''}</article>`;
}

function renderPassage(p, answer) {
  const quotes = answer.result.claims.flatMap(c=>c.evidence).filter(e=>e.passage_id===p.id).map(e=>e.quote);
  const ranges = [];
  for (const range of quotes.map(q=>[p.text.indexOf(q),p.text.indexOf(q)+q.length]).filter(r=>r[0]>=0).sort((a,b)=>a[0]-b[0])) {
    if(ranges.length && range[0]<=ranges.at(-1)[1]) ranges.at(-1)[1]=Math.max(ranges.at(-1)[1],range[1]);
    else ranges.push(range);
  }
  let content='',offset=0;
  for (const [start,end] of ranges) { if (start<offset) continue; content+=esc(p.text.slice(offset,start))+`<mark>${esc(p.text.slice(start,end))}</mark>`; offset=end; }
  content+=esc(p.text.slice(offset));
  const project=state.projects.find(x=>x.id===p.project_id);
  return `<section class="qa-passage" id="qa-passage"><div class="panel-top"><span class="eyebrow">EXACT SAVED PASSAGE</span><button class="text-button" data-qa-close-passage aria-label="Close passage">×</button></div><h3>${esc(p.title)}</h3><p class="qa-meta">${esc(project?.question || p.project_id)} · ${date(p.fetched_at)}<br>Characters ${p.start_offset}–${p.end_offset} · Found by ${esc(p.retrieved_by.join(', '))}</p><div class="passage-text">${content}</div><button class="button secondary small" data-qa-open-note="${p.note_id}" data-qa-project="${p.project_id}">Open full research note ↗</button><details class="passage-sources"><summary>Source links attached to this note (${p.sources.length})</summary>${p.sources.map(s=>`<a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title||s.url)} ↗</a>`).join('')}</details></section>`;
}

function knowledgeGraph(answer) {
  const relations=answer.retrieval.connections.slice(0,12);
  const passages=answer.retrieval.passages;
  const concepts=new Map();
  relations.forEach(r=>{concepts.set(r.source,r.source_name);concepts.set(r.target,r.target_name);});
  const positions=new Map();
  const items=[...concepts];
  items.forEach(([id],i)=>positions.set(id,{x:75+(i%4)*145,y:55+Math.floor(i/4)*80}));
  const topHeight=Math.max(110,Math.ceil(items.length/4)*80+30);
  const height=topHeight+Math.ceil(passages.length/6)*65+30;
  passages.forEach((p,i)=>positions.set(p.id,{x:65+(i%6)*98,y:topHeight+Math.floor(i/6)*65}));
  return `<div class="qa-map"><p class="helper">Concept connections used in retrieval. Select a passage to inspect its evidence.</p><svg viewBox="0 0 600 ${height}" role="group" aria-label="Evidence subgraph">${relations.map(r=>{const a=positions.get(r.source),b=positions.get(r.target),p=positions.get(r.passage_id);return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="graph-edge highlight"/><line x1="${b.x}" y1="${b.y}" x2="${p.x}" y2="${p.y}" class="graph-edge" stroke-dasharray="4 4"/>`;}).join('')}${items.map(([id,label])=>{const p=positions.get(id);return `<g transform="translate(${p.x},${p.y})"><title>${esc(label)}</title><circle r="7" fill="#b9a777"/><text text-anchor="middle" y="23">${esc(label.length>22?label.slice(0,20)+'…':label)}</text></g>`;}).join('')}${passages.map((p,i)=>{const pos=positions.get(p.id);return `<g tabindex="0" role="button" aria-label="Open passage ${i+1}" data-qa-evidence="${p.id}" data-qa-answer="${answer.id}" transform="translate(${pos.x},${pos.y})"><rect x="-22" y="-12" width="44" height="24" rx="5" fill="#e1ead7"/><text text-anchor="middle" y="4">P${i+1}</text></g>`;}).join('')}</svg>${relations.length?`<ul class="qa-connections">${relations.map(r=>`<li>${esc(r.source_name)} <span>→ ${esc(r.label)} →</span> ${esc(r.target_name)} ${passageButtons([{passage_id:r.passage_id}],answer)}</li>`).join('')}</ul>`:'<p class="helper">This answer used passage search. No extracted graph connections were used.</p>'}</div>`;
}

document.addEventListener('input',event=>{
  if(event.target.id==='knowledge-question') knowledgeView().draft=event.target.value;
});
document.addEventListener('change',event=>{
  const view=knowledgeView();
  if(event.target.dataset.qaScope) {
    const id=event.target.dataset.qaScope;
    view.scope=event.target.checked?[...new Set([...view.scope,id])]:view.scope.filter(x=>x!==id);
    $('#knowledge-scope summary').textContent=`Search ${view.scope.length} investigations ▾`;
  }
  if(event.target.id==='knowledge-history') {view.selected=event.target.value;view.evidence=null;renderKnowledge($('#tab-content'),state.project);}
});
document.addEventListener('submit',async event=>{
  if(event.target.id!=='knowledge-form') return;
  event.preventDefault();
  const id=state.project.id,view=knowledgeView(id);
  if(view.sending) return;
  view.sending=true;view.error=null;
  renderKnowledge($('#tab-content'),state.project);
  try {
    const answer=await api(`/projects/${id}/ask`,{question:view.draft,scope:view.scope});
    view.selected=answer.id;view.evidence=null;view.draft='';
    await refreshKnowledge(id);
  } catch(error){view.error=error.message;}
  finally{view.sending=false;if(state.project?.id===id && state.tab==='ask')renderKnowledge($('#tab-content'),state.project);}
});
document.addEventListener('click',async event=>{
  const el=event.target.closest('[data-qa-evidence],[data-qa-gap],[data-qa-open-note],[data-qa-reindex],[data-qa-retry],[data-qa-graph],[data-qa-close-passage]');
  if(!el || !state.project) return;
  const id=state.project.id,view=knowledgeView(id);
  try {
    if(el.hasAttribute('data-qa-reindex')) {await api(`/projects/${id}/reindex`,{});toast('Indexing retry scheduled.');}
    else if(el.dataset.qaEvidence) {view.selected=el.dataset.qaAnswer;view.evidence=el.dataset.qaEvidence;renderKnowledge($('#tab-content'),state.project);$('#qa-passage')?.scrollIntoView({block:'nearest'});}
    else if(el.hasAttribute('data-qa-close-passage')) {view.evidence=null;renderKnowledge($('#tab-content'),state.project);}
    else if(el.hasAttribute('data-qa-graph')) {view.graph=!view.graph;renderKnowledge($('#tab-content'),state.project);}
    else if(el.hasAttribute('data-qa-gap')) {
      const answer=view.data.answers.find(a=>a.id===el.dataset.qaAnswer);
      await api(`/projects/${id}/questions`,{question:answer.result.gaps[Number(el.dataset.qaGap)]});
      toast(state.project.running?'Gap added to the next research plan.':'Gap added. Press Start when you want Brave to research it.');await refresh();
    } else if(el.dataset.qaRetry) {const answer=view.data.answers.find(a=>a.id===el.dataset.qaRetry);view.draft=answer.question;view.scope=[...answer.scope];renderKnowledge($('#tab-content'),state.project);$('#knowledge-question').focus();}
    else if(el.dataset.qaOpenNote) {
      if(el.dataset.qaProject!==id) await selectProject(el.dataset.qaProject);
      state.note=el.dataset.qaOpenNote;state.tab='notes';renderProject(state.project,true);
    }
  } catch(error){toast(error.message);}
});
document.addEventListener('keydown',event=>{
  const node=event.target.closest('g[data-qa-evidence]');
  if(node && ['Enter',' '].includes(event.key)){event.preventDefault();node.dispatchEvent(new MouseEvent('click',{bubbles:true}));}
});
