const $ = (selector) => document.querySelector(selector);
const state = { token: '', projects: [], project: null, health: null, tab: 'overview', note: null, cycle: null, node: null, busy: false, version: '', generation: 0 };
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const date = (value) => new Date(value).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
const phaseLabel = { idle: 'Ready to explore', planning: 'Planning', researching: 'Researching', briefing: 'Writing briefing', paused: 'Paused', error: 'Needs attention', complete: 'Stopped' };
let toastTimer;

function toast(message) {
  $('#toast').textContent = message;
  $('#toast').classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 6000);
}

async function api(path, body) {
  const response = await fetch('/api' + path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-R3N-Token': state.token }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'The request could not be completed.');
  return data;
}

function inline(text) {
  // Only emit known markup; provider HTML never reaches innerHTML as raw HTML.
  const pattern = /`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|\*\*([^*]+)\*\*/g;
  let result = '', offset = 0;
  for (const match of text.matchAll(pattern)) {
    result += esc(text.slice(offset, match.index));
    if (match[1]) result += `<code>${esc(match[1])}</code>`;
    else if (match[2]) result += `<a href="${esc(match[3])}" target="_blank" rel="noopener noreferrer">${esc(match[2])}</a>`;
    else result += `<strong>${esc(match[4])}</strong>`;
    offset = match.index + match[0].length;
  }
  return result + esc(text.slice(offset));
}

function markdown(text) {
  const lines = String(text || '').split('\n');
  let html = '', paragraph = [], list = null, code = null;
  function flush() {
    if (paragraph.length) { html += `<p>${inline(paragraph.join(' '))}</p>`; paragraph = []; }
    if (list) { html += `</${list}>`; list = null; }
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('```')) {
      flush();
      if (code !== null) { html += `<pre><code>${esc(code.join('\n'))}</code></pre>`; code = null; }
      else code = [];
      continue;
    }
    if (code !== null) { code.push(line); continue; }
    const heading = line.match(/^(#{1,4})\s+(.+)/);
    const item = line.match(/^\s*(?:([-*])|\d+\.)\s+(.+)/);
    if (heading) { flush(); html += `<h${heading[1].length}>${inline(heading[2])}</h${heading[1].length}>`; }
    else if (item) {
      const kind = item[1] ? 'ul' : 'ol';
      if (list !== kind) { flush(); list = kind; html += `<${kind}>`; }
      html += `<li>${inline(item[2])}</li>`;
    } else if (line.startsWith('> ')) { flush(); html += `<blockquote>${inline(line.slice(2))}</blockquote>`; }
    else if (line.includes('|') && i + 1 < lines.length && /^\s*\|?\s*:?-{3,}/.test(lines[i + 1])) {
      flush();
      const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map(s => s.trim());
      html += `<table><thead><tr>${cells(line).map(c => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && lines[i].includes('|')) { html += `<tr>${cells(lines[i]).map(c => `<td>${inline(c)}</td>`).join('')}</tr>`; i++; }
      i--;
      html += '</tbody></table>';
    } else if (!line.trim() || /^---+$/.test(line.trim())) flush();
    else { if (list) flush(); paragraph.push(line); }
  }
  flush();
  if (code !== null) html += `<pre><code>${esc(code.join('\n'))}</code></pre>`;
  return html;
}

let sidebarVersion = '';
function renderSidebar() {
  const nextVersion = JSON.stringify([state.projects, state.project?.id]);
  if (nextVersion === sidebarVersion) return;
  sidebarVersion = nextVersion;
  $('#project-count').textContent = state.projects.length;
  $('#projects').innerHTML = state.projects.length ? state.projects.map(p => `<button class="project-link ${state.project?.id === p.id ? 'selected' : ''}" data-project="${p.id}"><span class="symbol">◌</span><span><span class="project-title">${esc(p.question)}</span><small>${p.running ? '● Exploring' : `${p.answers} notes`} · ${date(p.created_at)}</small></span></button>`).join('') : '<div class="nav-empty">Every investigation starts<br>with a little curiosity.</div>';
}

async function health() {
  state.health = await api('/health');
  const h = state.health;
  const ready = h.brave_configured && h.ollama_connected && h.model_available;
  $('#connection-status').innerHTML = `<span class="status-dot ${ready ? '' : 'warn'}"></span>${ready ? 'Research engine ready' : 'Connection setup needed'}`;
  $('#connection-details').innerHTML = `<div class="connection-row"><div><h3>Brave Answers</h3><span class="badge ${h.brave_configured ? '' : 'gold'}">${h.brave_configured ? 'Key configured' : 'Key needed'}</span></div><p>Web research with source citations. Add an Answers-enabled key to <code>.env</code> in this project, then restart the app. A configured key is not a verified subscription.</p><pre>BRAVE_SEARCH_API_KEY=your_key_here</pre></div><div class="connection-row"><div><h3>Local Qwen · Ollama</h3><span class="badge ${h.model_available ? '' : 'gold'}">${h.model_available ? 'Model available' : h.ollama_connected ? 'Model not found' : 'Not connected'}</span></div><p><code>${esc(h.model)}</code><br>Server: ${esc(h.ollama_url)}</p><p>${h.model_available ? 'Planning, question generation, and briefing run on your machine.' : 'Start Ollama and make the exact model tag available, or change OLLAMA_MODEL in .env.'}</p></div><p class="helper">Only your research question and the root question are sent to Brave. Saved notes and briefings are sent to your configured Ollama server. No cloud model fallback.</p>`;
}

function renderHome() {
  $('#breadcrumb').textContent = 'New investigation';
  $('#main').innerHTML = `<section class="hero"><span class="eyebrow"><span class="status-dot"></span> FOLLOW THE QUESTION</span><h1>A question is only<br><em>the beginning.</em></h1><p class="hero-intro">Start with something you want to understand. Let your research unfold, one question, connection, and discovery at a time.</p><form id="create-form" class="question-composer"><label for="root-question">WHAT ARE YOU CURIOUS ABOUT?</label><textarea id="root-question" name="question" placeholder="What would you like to understand more deeply?" required minlength="3" maxlength="1000" rows="2"></textarea><div class="composer-bottom"><p class="helper"><span>↻</span> Keeps exploring until you press pause.</p><button class="button" type="submit">Begin investigation <span>↗</span></button></div></form><p class="starter-label">A FEW PLACES TO START</p><div class="starters"><button class="starter" data-starter="How can cities become more resilient to extreme heat?">Climate-resilient cities ↗</button><button class="starter" data-starter="What are the practical limits of running AI models locally?">The future of local AI ↗</button><button class="starter" data-starter="How does the human brain form and retain long-term memories?">The science of memory ↗</button></div><section class="flow-section"><div class="flow-heading"><h2>A research loop that goes a little deeper.</h2><span>YOU SET THE DIRECTION</span></div><div class="flow-cards"><article class="flow-card"><span class="number">01</span><span class="flow-icon">⌘</span><h3>Plan the next question</h3><p>Local Qwen finds the gaps and turns curiosity into a research plan.</p></article><article class="flow-card"><span class="number">02</span><span class="flow-icon">⌕</span><h3>Follow the evidence</h3><p>Brave researches each question. Answers and sources become your notes.</p></article><article class="flow-card"><span class="number">03</span><span class="flow-icon">⠿</span><h3>Connect the discoveries</h3><p>A briefing and knowledge graph reveal what to explore next.</p></article></div><div class="loop-note">↻ &nbsp; Plan. Research. Brief. Keep going.</div></section><div id="home-setup"></div></section>`;
  updateHomeSetup();
}

function updateHomeSetup() {
  const target = $('#home-setup');
  if (!target || !state.health) return;
  const h = state.health;
  target.innerHTML = (!h.brave_configured || !h.model_available) ? '<div class="setup-nudge"><span>◈</span><span>Your notebook is ready. Connect your research engine to begin.</span><button class="text-button" data-action="connections">View setup ↗</button></div>' : '';
}

function stats(p) {
  const answers = p.questions.filter(q => q.answer);
  const sources = new Set(answers.flatMap(q => q.answer.sources.map(s => s.url)));
  const relations = p.graph.edges.filter(e => e.confidence);
  return `<div class="stats">${[[answers.length,'research notes'],[p.cycles.filter(c => c.brief).length,'completed cycles'],[sources.size,'sources collected'],[relations.length,'concept connections']].map(([n,label]) => `<div class="stat"><strong>${n.toString().padStart(2,'0')}</strong><span>${label}</span></div>`).join('')}</div>`;
}

function renderProject(p, preserveInput = false) {
  const knowledgeFocus = document.activeElement?.id === 'knowledge-question';
  const knowledgeSelection = knowledgeFocus ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const addValue = preserveInput ? $('#followup-question')?.value : '';
  const activeInput = document.activeElement?.id === 'followup-question';
  const selection = activeInput ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  $('#breadcrumb').textContent = 'Investigation';
  const n = p.cycles.at(-1)?.number || 1;
  const phase = p.stop_requested ? 'Pausing after current call' : phaseLabel[p.phase] || p.phase;
  $('#main').innerHTML = `<div class="investigation-head"><div><span class="eyebrow">INVESTIGATION ${String(state.projects.findIndex(x => x.id === p.id) + 1).padStart(2,'0')}</span><h1>${esc(p.question)}</h1><div class="subline"><span class="status-dot ${p.running ? 'live' : 'neutral'}"></span><span>${esc(phase)}</span><span>·</span><span>Started ${date(p.created_at)}</span></div></div><div class="head-actions"><button class="button ${p.running ? 'pause' : ''}" data-action="${p.running ? 'pause' : 'start'}" ${p.stop_requested || state.busy ? 'disabled' : ''}>${p.running ? 'Ⅱ &nbsp; Pause' : '▷ &nbsp; Start'}</button><a class="button secondary" href="/api/projects/${p.id}/export">↓ Export</a><button class="button secondary icon-button" data-action="settings" aria-label="Research preferences" title="Research preferences" ${p.running ? 'disabled' : ''}>⚙</button></div></div>${p.error ? `<div class="error-banner">${esc(p.error)}</div>` : ''}${p.stop_requested ? '<div class="status-banner">Pausing after the current provider call. Its result will be saved. No new calls will be started.</div>' : ''}${p.phase === 'complete' ? `<div class="status-banner">${esc(p.stop_reason || 'Research stopped. Check the answer budget or add a new question to continue.')}</div>` : ''}${stats(p)}<div class="research-layout"><section><div class="stage-bar">${[['planning','⌘','Plan'],['researching','⌕','Research'],['briefing','⠿','Brief']].map(([id,icon,label]) => `<div class="stage ${p.phase === id && p.running ? 'active' : ''}"><span class="stage-icon">${icon}</span>${label}<span>${p.phase === id && p.running ? 'IN PROGRESS' : '→'}</span></div>`).join('')}</div><div class="tabs" role="tablist">${[['overview','Overview'],['ask','Ask your research'],['notes','Notebook'],['graph','Knowledge graph'],['activity','Activity']].map(([id,label]) => `<button class="tab ${state.tab === id ? 'active' : ''}" role="tab" aria-selected="${state.tab === id}" data-tab="${id}">${label}</button>`).join('')}</div><div id="tab-content"></div></section><aside><div class="panel queue-panel"><div class="queue-head"><h2>Research queue</h2><span class="badge">${p.questions.filter(q => ['pending','researching'].includes(q.status)).length} questions</span></div><p class="queue-note">Your questions come first. Qwen builds on the discoveries that follow.</p><div id="queue-items">${queue(p)}</div><form class="add-question" id="add-form"><label for="followup-question">STEER THE INVESTIGATION</label><textarea id="followup-question" name="question" placeholder="What else should we look into?" required minlength="3" maxlength="1000">${esc(addValue || '')}</textarea><button class="button secondary small" type="submit">＋ Add your question</button></form></div><p class="run-note"><strong>↻ Continuous exploration</strong><br>${p.settings.questions_per_cycle} questions per cycle · ${p.settings.answer_budget ? `${p.settings.answer_budget} answer budget` : 'No answer limit'}<br>Brave requests use your API credits. Pause whenever you need a moment to think.</p></aside></div>`;
  renderTab();
  if (activeInput && selection) { $('#followup-question').focus(); $('#followup-question').setSelectionRange(...selection); }
  if (knowledgeFocus && knowledgeSelection && $('#knowledge-question')) { $('#knowledge-question').focus(); $('#knowledge-question').setSelectionRange(...knowledgeSelection); }
}

function queue(p) {
  const pending = p.questions.filter(q => ['pending','researching'].includes(q.status));
  return pending.length ? pending.slice(0,12).map(q => `<div class="queue-item ${q.status === 'researching' ? 'current' : ''}"><span class="queue-icon">${q.status === 'researching' ? '◉' : '○'}</span><div><p>${esc(q.text)}</p><small>${q.origin === 'qwen' ? 'Qwen' : q.origin === 'seed' ? 'Starting question' : 'You'} · ${q.status === 'researching' ? 'Researching now' : q.cycle ? `Cycle ${q.cycle}` : 'Next cycle'}</small>${!p.running && q.status === 'pending' ? `<button class="skip" data-skip="${q.id}">Skip question</button>` : ''}</div></div>`).join('') + (pending.length > 12 ? `<p class="queue-note">+ ${pending.length - 12} more queued</p>` : '') : `<div class="queue-empty">${p.running ? 'Gathering the next thread to follow…' : 'A little space for your next question.'}</div>`;
}

function empty(title, message, symbol='◌') { return `<div class="panel empty-panel"><span class="empty-symbol">${symbol}</span><h2>${esc(title)}</h2><p>${esc(message)}</p></div>`; }
function refs(ids) { return ids.map(id => `<button class="note-ref" data-note="${esc(id)}">↗ Note ${esc(id.slice(0,4))}</button>`).join(''); }

function renderTab() {
  const p = state.project;
  const target = $('#tab-content');
  if (!p || !target) return;
  if (state.tab === 'overview') {
    const cycles = p.cycles.filter(c => c.brief);
    const c = cycles.find(c => c.number === state.cycle) || cycles.at(-1);
    const plan = p.cycles.at(-1);
    let html = '';
    if (plan?.plan && !plan.brief) html += `<article class="panel"><div class="panel-top"><h2>The plan</h2><span class="badge">CYCLE ${plan.number}</span></div><div class="prose">${markdown(plan.plan)}</div></article>`;
    if (c) {
      const b = c.brief;
      html += `<article class="panel"><div class="panel-top"><h2>What we know so far</h2><select class="cycle-select" id="cycle-select" aria-label="Briefing cycle">${cycles.map(x => `<option value="${x.number}" ${x.number === c.number ? 'selected' : ''}>Cycle ${x.number} briefing</option>`).join('')}</select></div><div class="prose">${markdown(b.summary)}</div><h3 class="small-heading">FINDINGS & EVIDENCE</h3><ul class="findings">${b.findings.map(f => `<li class="finding">${esc(f.claim)}<div class="finding-meta"><span class="badge ${f.confidence === 'low' ? 'gold' : 'gray'}">${esc(f.confidence)} confidence</span>${refs(f.evidence)}</div></li>`).join('')}</ul>${b.gaps.length ? `<h3 class="small-heading">STILL TO UNDERSTAND</h3><ul class="gap-list">${b.gaps.map(g => `<li>${esc(g)}</li>`).join('')}</ul>` : ''}${b.contradictions.length ? `<h3 class="small-heading">CONFLICTING EVIDENCE</h3><ul class="gap-list">${b.contradictions.map(g => `<li>${esc(g)}</li>`).join('')}</ul>` : ''}<h3 class="small-heading">WHERE WE GO NEXT</h3><div class="prose">${markdown(b.next_direction)}</div></article>`;
    } else if (!html) html = empty(p.running ? 'Following the first thread.' : 'Ready when you are.', p.running ? 'Qwen is shaping the research plan. Your first findings will appear here as the investigation unfolds.' : 'Press Start to begin the research loop. Add a question at any time to give it a new direction.');
    target.innerHTML = html;
  } else if (state.tab === 'notes') {
    const notes = p.questions.filter(q => q.answer).reverse();
    const q = notes.find(q => q.id === state.note) || notes[0];
    if (!q) { target.innerHTML = empty('A notebook in the making.', 'Every Brave answer is saved here with its sources, and as a Markdown file on your machine.', '▤'); return; }
    target.innerHTML = `<div class="notes-list">${notes.map(n => `<button class="note-item ${n.id === q.id ? 'active' : ''}" data-note="${n.id}"><span>▤</span><span class="note-info">${esc(n.text)}<small>Cycle ${n.cycle} · ${n.answer.sources.length} sources · ${date(n.answer.fetched_at)}</small></span><span>↗</span></button>`).join('')}</div><article class="panel"><span class="eyebrow">BRAVE ANSWERS · CYCLE ${q.cycle}</span><h2 class="note-title">${esc(q.text)}</h2><div class="prose">${markdown(q.answer.markdown)}</div>${q.answer.warnings?.map(w => `<p class="helper">${esc(w)}</p>`).join('') || ''}<h3 class="small-heading">SOURCES</h3><ul class="source-list">${q.answer.sources.map(s => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)} ↗</a>${s.snippet ? `<p>${esc(s.snippet)}</p>` : ''}</li>`).join('')}</ul></article>`;
  } else if (state.tab === 'ask') renderKnowledge(target, p);
  else if (state.tab === 'graph') renderGraph(target, p);
  else target.innerHTML = `<div class="panel"><div class="panel-top"><h2>The research trail</h2><span class="badge">LIVE JOURNAL</span></div>${p.events.length ? [...p.events].reverse().map(e => `<div class="event"><time title="${esc(e.at)}">${new Date(e.at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',hour12:false})}</time><p>${esc(e.message)}</p></div>`).join('') : '<p class="helper">Your investigation’s activity will appear here.</p>'}</div>`;
}

function renderGraph(target, p) {
  const graph = p.graph;
  if (graph.nodes.length < 3) { target.innerHTML = empty('Connections take shape here.', 'Questions, sources, and concepts form a living map as your research grows. Select any node to trace its evidence.', '⠿'); return; }
  // Show a bounded, recent neighborhood for large investigations; exports retain all nodes.
  const root = graph.nodes.find(n => n.type === 'investigation');
  const nodes = [root, ...graph.nodes.filter(n => n !== root).slice(-119)];
  const ids = new Set(nodes.map(n => n.id));
  const edges = graph.edges.filter(e => ids.has(e.source) && ids.has(e.target));
  const colors = { investigation:'#355c3c', question:'#94a981', concept:'#b9a777', source:'#a6babb' };
  const positions = new Map([[root.id, { x: 350, y: 250 }]]);
  const others = nodes.filter(n => n !== root);
  // Deterministic radial placement, then a small force relaxation for readable clusters.
  others.forEach((n, i) => { const angle = i * 2.39996; const radius = 60 + 145 * Math.sqrt((i+1)/others.length); positions.set(n.id, {x:350+Math.cos(angle)*radius*1.35, y:250+Math.sin(angle)*radius}); });
  for (let tick=0; tick<85; tick++) {
    const delta = new Map(nodes.map(n => [n.id,{x:0,y:0}]));
    for (let i=0;i<nodes.length;i++) for (let j=i+1;j<nodes.length;j++) {
      const a=positions.get(nodes[i].id), b=positions.get(nodes[j].id), dx=a.x-b.x, dy=a.y-b.y, d=Math.max(15,Math.hypot(dx,dy)), f=1400/(d*d);
      delta.get(nodes[i].id).x += dx/d*f; delta.get(nodes[i].id).y += dy/d*f;
      delta.get(nodes[j].id).x -= dx/d*f; delta.get(nodes[j].id).y -= dy/d*f;
    }
    for (const e of edges) {
      const a=positions.get(e.source), b=positions.get(e.target), dx=b.x-a.x, dy=b.y-a.y, d=Math.max(1,Math.hypot(dx,dy)), f=(d-92)*.012;
      delta.get(e.source).x+=dx/d*f; delta.get(e.source).y+=dy/d*f;
      delta.get(e.target).x-=dx/d*f; delta.get(e.target).y-=dy/d*f;
    }
    for (const n of others) { const v=positions.get(n.id), d=delta.get(n.id); v.x=Math.min(625,Math.max(75,v.x+d.x+(350-v.x)*.002)); v.y=Math.min(440,Math.max(45,v.y+d.y+(250-v.y)*.002)); }
  }
  const selected = nodes.find(n=>n.id===state.node);
  const related = selected ? graph.edges.filter(e=>e.source===selected.id || e.target===selected.id) : [];
  const evidence = [...new Set(related.flatMap(e=>e.evidence))];
  target.innerHTML = `<div class="graph-toolbar">${Object.entries(colors).map(([type,color])=>`<span class="legend"><i style="background:${color}"></i>${type[0].toUpperCase()+type.slice(1)}</span>`).join('')}<span>${graph.nodes.length} nodes · ${graph.edges.length} links</span></div><div class="graph-wrap"><svg viewBox="0 0 700 500" role="group" aria-label="Research knowledge graph. Select a node to inspect its connections.">${edges.map(e=>{const a=positions.get(e.source),b=positions.get(e.target);return `<line x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" class="graph-edge ${selected && (e.source===selected.id||e.target===selected.id)?'highlight':''}"/>`;}).join('')}${nodes.map(n=>{const v=positions.get(n.id);return `<g class="graph-node ${state.node===n.id?'selected':''}" data-node="${n.id}" tabindex="0" role="button" aria-label="${esc(n.label)}" transform="translate(${v.x},${v.y})"><title>${esc(n.label)}</title><circle r="${n.type==='investigation'?13:n.type==='concept'?8:6}" fill="${colors[n.type]}"/><text text-anchor="middle" y="${n.type==='investigation'?29:23}">${esc(n.label.length>29?n.label.slice(0,27)+'…':n.label)}</text></g>`;}).join('')}</svg><div class="graph-detail">${selected ? `<span class="eyebrow">${esc(selected.type)}</span><strong>${esc(selected.label)}</strong>${selected.url ? `<a href="${esc(selected.url)}" target="_blank" rel="noopener noreferrer">Open source ↗</a><br>` : ''}${related.filter(e=>e.confidence).map(e=>`<div>${esc(graph.nodes.find(n=>n.id===e.source)?.label)} → ${esc(e.label)} → ${esc(graph.nodes.find(n=>n.id===e.target)?.label)} <span class="badge gray">${esc(e.confidence)}</span></div>`).join('')}${refs(evidence)}${selected.type==='question' && p.questions.find(q=>q.id===selected.id)?.answer?refs([selected.id]):''}` : 'Select a node to explore its connections and supporting research notes.'}</div></div><p class="graph-hint">Concept connections are Qwen’s interpretations of saved answers. ${graph.nodes.length>120?'Showing the 119 most recent nodes and root. Export includes the full graph.':'The full graph is saved as JSON and Mermaid alongside your notes.'}</p>`;
}

async function refresh(force = false) {
  const generation = state.generation;
  const id = state.project?.id;
  const projects = await api('/projects');
  if (generation !== state.generation) return;
  state.projects = projects;
  renderSidebar();
  if (id) {
    const p = await api(`/projects/${id}`);
    if (generation !== state.generation) return;
    const changed = p.updated_at !== state.version;
    state.project = p;
    state.version = p.updated_at;
    if (changed || force) renderProject(p, true);
    if (state.tab === 'ask') await refreshKnowledge(id);
  }
}

async function selectProject(id) {
  const generation = ++state.generation;
  const p = await api(`/projects/${id}`);
  if (generation !== state.generation) return;
  state.project = p; state.tab = 'overview'; state.note = null; state.cycle = null; state.node = null; state.version = p.updated_at;
  location.hash = id;
  renderSidebar(); renderProject(p);
}

async function runAction(action) {
  if (state.busy || !state.project) return;
  state.busy = true;
  try { await api(`/projects/${state.project.id}/${action}`, {}); await refresh(true); }
  finally { state.busy = false; if (state.project) renderProject(state.project, true); }
}

document.addEventListener('click', async event => {
  const target = event.target.closest('button, a, [data-node]');
  if (!target) return;
  try {
    if (target.dataset.project) await selectProject(target.dataset.project);
    else if (target.id === 'new-project') { ++state.generation; state.project = null; state.version = ''; history.replaceState(null, '', '/'); renderSidebar(); renderHome(); $('#root-question').focus(); }
    else if (target.dataset.starter) { $('#root-question').value = target.dataset.starter; $('#root-question').focus(); }
    else if (['connection-status','open-connections'].includes(target.id) || target.dataset.action === 'connections') { $('#connections').showModal(); await health(); }
    else if (target.id === 'refresh-health') { target.disabled = true; try { await health(); updateHomeSetup(); } finally { target.disabled = false; } }
    else if (target.dataset.action === 'settings') {
      const form = $('#settings-form');
      form.elements.questions_per_cycle.value = state.project.settings.questions_per_cycle;
      form.elements.answer_budget.value = state.project.settings.answer_budget;
      $('#settings').showModal();
    } else if (target.id === 'cancel-settings') $('#settings').close();
    else if (['start','pause'].includes(target.dataset.action)) await runAction(target.dataset.action);
    else if (target.dataset.tab) { state.tab = target.dataset.tab; renderProject(state.project, true); if (state.tab === 'ask') await refreshKnowledge(state.project.id); }
    else if (target.dataset.note) { state.note = target.dataset.note; state.tab = 'notes'; renderProject(state.project, true); }
    else if (target.dataset.node) { state.node = target.dataset.node; renderTab(); }
    else if (target.dataset.skip) { await api(`/projects/${state.project.id}/skip`,{id:target.dataset.skip}); await refresh(); }
  } catch (error) { toast(error.message); }
});

document.addEventListener('submit', async event => {
  if (!['create-form','add-form','settings-form'].includes(event.target.id)) return;
  event.preventDefault();
  const form = event.target, button = form.querySelector('button[type="submit"]') || form.querySelector('.button:not([type="button"])');
  if (button) button.disabled = true;
  try {
    if (form.id === 'create-form') {
      const p = await api('/projects',{question:form.elements.question.value,questions_per_cycle:3,answer_budget:0});
      await refresh(); await selectProject(p.id);
      try { await runAction('start'); } catch (error) { toast(error.message); $('#connections').showModal(); }
    } else if (form.id === 'add-form') {
      await api(`/projects/${state.project.id}/questions`,{question:form.elements.question.value});
      form.reset(); await refresh(); toast('Question added. It gets priority in the next plan.');
    } else {
      await api(`/projects/${state.project.id}/settings`,{questions_per_cycle:Number(form.elements.questions_per_cycle.value),answer_budget:Number(form.elements.answer_budget.value)});
      $('#settings').close(); await refresh(); toast('Research preferences saved.');
    }
  } catch (error) { toast(error.message); }
  finally { if (button) button.disabled = false; }
});

document.addEventListener('change', event => {
  if (event.target.id === 'cycle-select') { state.cycle = Number(event.target.value); renderTab(); }
});
document.addEventListener('keydown', event => {
  if (event.target.closest('[data-node]') && ['Enter',' '].includes(event.key)) { event.preventDefault(); state.node=event.target.closest('[data-node]').dataset.node; renderTab(); }
  if (event.key.toLowerCase() === 'n' && !event.metaKey && !event.ctrlKey && !['INPUT','TEXTAREA','SELECT'].includes(event.target.tagName) && !document.querySelector('dialog[open]')) $('#new-project').click();
});
window.addEventListener('hashchange', () => {
  const id=location.hash.slice(1);
  if (/^[a-f0-9]{12}$/.test(id) && state.project?.id!==id) selectProject(id).catch(e=>toast(e.message));
});

async function init() {
  renderHome();
  try {
    state.token = (await api('/session')).token;
    await refresh();
    const id = location.hash.slice(1);
    if (/^[a-f0-9]{12}$/.test(id)) await selectProject(id);
    await health(); updateHomeSetup();
  } catch (error) { toast('Could not connect to the app. ' + error.message); }
  async function poll() {
    try { if (!document.hidden && !state.busy) await refresh(); }
    catch { $('#connection-status').innerHTML = '<span class="status-dot warn"></span>App disconnected'; }
    setTimeout(poll, 2000);
  }
  setTimeout(poll,2000);
}
init();
