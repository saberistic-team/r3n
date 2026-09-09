"""Incremental local passage index, evidence graph, and grounded knowledge Q&A.

Research JSON remains authoritative. This separate SQLite index is rebuildable;
saved Q&A carries immutable passage snapshots so citations survive reindexing.
"""
import hashlib
import json
import re
import sqlite3
import threading
from collections import defaultdict

from .engine import CONFIDENCE, array, obj, string
from .providers import ProviderError, validate_schema
from .store import atomic_write, normalized, now, uid


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def chunk_text(text, size=1400, overlap=180):
    """Exact substrings with character offsets, favoring paragraph/sentence boundaries."""
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            for separator in ('\n\n', '. ', '\n', ' '):
                boundary = text.rfind(separator, start + size // 2, end)
                if boundary != -1:
                    end = boundary + len(separator)
                    break
        if text[start:end].strip():
            yield start, end, text[start:end]
        if end == len(text):
            break
        start = max(start + 1, end - overlap)


QUOTE = obj(passage_id=string(100), quote=string(1600))
EXTRACTION_SCHEMA = obj(
    entities=array(obj(name=string(160), kind={'type': 'string', 'enum': ['person', 'organization', 'location', 'concept', 'product', 'other']},
                       aliases=array(string(160), 6), evidence=array(QUOTE, 8)), 20),
    relations=array(obj(source=string(160), target=string(160), relation=string(160),
                        confidence=CONFIDENCE, evidence=array(QUOTE, 8)), 24))
ANSWER_SCHEMA = obj(
    status={'type': 'string', 'enum': ['answered', 'partial', 'insufficient']},
    claims=array(obj(text=string(2400), evidence=array(QUOTE, 8)), 12),
    gaps=array(string(1000), 8), caveats=array(string(1000), 8))

# The model selects immutable excerpts. It never needs to regenerate their quotes.
ENTITY_MODEL = obj(name=string(160), kind={'type':'string','enum':['person','organization','location','concept','product','other']},
                   aliases=array(string(160),6))
EXTRACT_MODEL_SCHEMA = obj(relations=array(obj(source=ENTITY_MODEL,target=ENTITY_MODEL,relation=string(160),
                                               confidence=CONFIDENCE,evidence=array(string(100),8)),6))
ANSWER_MODEL_SCHEMA = obj(status=ANSWER_SCHEMA['properties']['status'],
                         claims=array(obj(text=string(2400),evidence=array(string(100),8)),12),
                         gaps=array(string(1000),8), caveats=array(string(1000),8))


def excerpts_for(passages):
    excerpts = {}
    for p in passages:
        for start,end,text in chunk_text(p['text'],size=520,overlap=70):
            if len(text.strip()) >= 8:
                excerpts['E' + str(len(excerpts) + 1)] = {'passage_id':p['id'], 'quote':text}
    return excerpts


def expand_evidence(output, groups, excerpts):
    result = json.loads(json.dumps(output))
    for group in groups:
        for item in result[group]:
            if not item['evidence'] or any(id_ not in excerpts for id_ in item['evidence']):
                raise ProviderError('Qwen referenced evidence outside the retrieved excerpts. The result was rejected; retry the question or indexing.')
            item['evidence'] = [dict(excerpts[id_]) for id_ in dict.fromkeys(item['evidence'])]
    return result


def graph_from_relations(output, excerpts):
    """Derive every graph node from typed endpoints; dangling names are impossible."""
    expanded = expand_evidence(output, ('relations',), excerpts)
    entities, relations = {}, []
    for item in expanded['relations']:
        names = []
        for endpoint in (item['source'], item['target']):
            key = normalized(endpoint['name'])
            if not key:
                raise ProviderError('Graph extraction returned an empty concept name. Retry indexing.')
            if key not in entities:
                entities[key] = {**endpoint, 'evidence':[]}
            entity = entities[key]
            entity['aliases'] = list(dict.fromkeys(entity['aliases'] + endpoint['aliases']))[:6]
            for evidence in item['evidence']:
                if evidence not in entity['evidence'] and len(entity['evidence']) < 8:
                    entity['evidence'].append(evidence)
            names.append(entity['name'])
        relations.append({**item, 'source':names[0], 'target':names[1]})
    return {'entities':list(entities.values()), 'relations':relations}

STOP_WORDS = set('a an the to of in for on at by with from and or is are was were be been being do does did '
                 'what which how why when where who can could would should will about our we you your it its '
                 'this that these those as have has had tell me based research collected know explain compare'.split())


def terms(text):
    return [t for t in re.findall(r'\w+', text.casefold()) if len(t) > 1 and t not in STOP_WORDS][:40]


def valid_quote(item, passages):
    quote = item['quote']
    # Quotes are literal notebook substrings, not purported quotes from original websites.
    return item['passage_id'] in passages and len(quote.strip()) >= 8 and quote in passages[item['passage_id']]['text']


class KnowledgeIndex:
    def __init__(self, directory, embedding_model):
        self.directory = directory
        self.embedding_model = embedding_model
        self.lock = threading.RLock()
        self.db = sqlite3.connect(directory / 'knowledge.sqlite3', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS knowledge_notes (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                content_hash TEXT NOT NULL, fetched_at TEXT, sources TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS passages (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, note_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, start_offset INTEGER, end_offset INTEGER, text TEXT NOT NULL,
                vector TEXT, vector_model TEXT, graph_done INTEGER DEFAULT 0,
                embedding_error TEXT, graph_error TEXT);
            CREATE INDEX IF NOT EXISTS passage_scope ON passages(project_id, note_id);
            CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(id UNINDEXED, title, text, tokenize='unicode61');
            CREATE TABLE IF NOT EXISTS entities (
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS aliases (
                project_id TEXT, kind TEXT, alias TEXT, entity_id TEXT,
                PRIMARY KEY(project_id,kind,alias));
            CREATE TABLE IF NOT EXISTS mentions (
                entity_id TEXT, passage_id TEXT, quote TEXT, PRIMARY KEY(entity_id,passage_id,quote));
            CREATE TABLE IF NOT EXISTS relations (
                id TEXT PRIMARY KEY, project_id TEXT, source TEXT, target TEXT, label TEXT, confidence TEXT);
            CREATE INDEX IF NOT EXISTS relation_scope ON relations(project_id, source, target);
            CREATE TABLE IF NOT EXISTS relation_evidence (
                relation_id TEXT, passage_id TEXT, quote TEXT, PRIMARY KEY(relation_id,passage_id,quote));
            CREATE TABLE IF NOT EXISTS embedding_cache (
                content_hash TEXT, model TEXT, vector TEXT, PRIMARY KEY(content_hash,model));
            CREATE TABLE IF NOT EXISTS knowledge_answers (
                id TEXT PRIMARY KEY, project_id TEXT, document TEXT NOT NULL);
        ''')
        with self.lock, self.db:
            for row in self.db.execute('SELECT id,document FROM knowledge_answers').fetchall():
                answer = json.loads(row['document'])
                if answer['status'] in ('queued', 'retrieving', 'answering'):
                    answer.update(status='error', error='The app restarted during this question. Ask it again; saved research is intact.')
                    self.db.execute('UPDATE knowledge_answers SET document=? WHERE id=?', (json.dumps(answer), answer['id']))

    def rows(self, sql, params=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, params).fetchall()]

    def sync_project(self, project):
        """Fast, transactional text indexing; no inference while holding a database lock."""
        notes = {q['id']: q for q in project['questions'] if q.get('answer')}
        with self.lock, self.db:
            existing = {r['id']: r for r in self.db.execute('SELECT * FROM knowledge_notes WHERE project_id=?', (project['id'],))}
            for note_id in set(existing) - set(notes):
                self._remove_note(note_id)
            for q in notes.values():
                a = q['answer']
                content_hash = digest(q['text'] + '\n' + a['markdown'])
                if q['id'] in existing and existing[q['id']]['content_hash'] == content_hash:
                    self.db.execute('UPDATE knowledge_notes SET sources=?, fetched_at=? WHERE id=?',
                                    (json.dumps(a['sources']), a['fetched_at'], q['id']))
                    continue
                self._remove_note(q['id'])
                self.db.execute('INSERT INTO knowledge_notes VALUES (?,?,?,?,?,?)',
                                (q['id'], project['id'], q['text'], content_hash, a['fetched_at'], json.dumps(a['sources'])))
                for ordinal, (start, end, text) in enumerate(chunk_text(a['markdown'])):
                    pid = digest(project['id'] + q['id'] + content_hash + str(start))
                    self.db.execute('INSERT INTO passages(id,project_id,note_id,ordinal,start_offset,end_offset,text) VALUES (?,?,?,?,?,?,?)',
                                    (pid, project['id'], q['id'], ordinal, start, end, text))
                    self.db.execute('INSERT INTO passage_fts VALUES (?,?,?)', (pid, q['text'], text))

    def _remove_note(self, note_id):
        for row in self.db.execute('SELECT id FROM passages WHERE note_id=?', (note_id,)).fetchall():
            for table in ('mentions', 'relation_evidence'):
                self.db.execute(f'DELETE FROM {table} WHERE passage_id=?', (row['id'],))
            self.db.execute('DELETE FROM passage_fts WHERE id=?', (row['id'],))
        self.db.execute('DELETE FROM passages WHERE note_id=?', (note_id,))
        self.db.execute('DELETE FROM knowledge_notes WHERE id=?', (note_id,))
        self.db.execute('DELETE FROM relations WHERE id NOT IN (SELECT relation_id FROM relation_evidence)')

    def passage_rows(self, scope):
        marks = ','.join('?' for _ in scope)
        rows = self.rows(f'''SELECT p.*,n.title,n.fetched_at,n.sources FROM passages p
                            JOIN knowledge_notes n ON n.id=p.note_id WHERE p.project_id IN ({marks})''', scope)
        for row in rows:
            row['sources'] = json.loads(row['sources'])
        return rows

    def retry(self, project_id):
        with self.lock, self.db:
            self.db.execute('UPDATE passages SET embedding_error=NULL, graph_error=NULL WHERE project_id=?', (project_id,))

    def status(self, project_id):
        rows = self.rows('SELECT * FROM passages WHERE project_id=?', (project_id,))
        embedded = sum(bool(r['vector']) and r['vector_model'] == self.embedding_model for r in rows)
        errors = list(dict.fromkeys(e for r in rows for e in (r['embedding_error'], r['graph_error']) if e))
        return {'passages': len(rows), 'notes': len({r['note_id'] for r in rows}), 'embedded': embedded,
                'graph_indexed': sum(r['graph_done'] for r in rows), 'embedding_model': self.embedding_model,
                'errors': errors[:5], 'pending': sum((not r['vector'] or r['vector_model'] != self.embedding_model) and not r['embedding_error']
                                                    or not r['graph_done'] and not r['graph_error'] for r in rows),
                'entities': self.rows('''SELECT COUNT(DISTINCT e.id) AS n FROM entities e JOIN mentions m ON m.entity_id=e.id
                                        JOIN passages p ON p.id=m.passage_id WHERE e.project_id=?''', (project_id,))[0]['n'],
                'relations': self.rows('SELECT COUNT(*) AS n FROM relations WHERE project_id=?', (project_id,))[0]['n']}

    def save_vectors(self, passages, vectors):
        with self.lock, self.db:
            for p, v in zip(passages, vectors):
                value = json.dumps(v)
                self.db.execute('UPDATE passages SET vector=?,vector_model=?,embedding_error=NULL WHERE id=?',
                                (value, self.embedding_model, p['id']))
                self.db.execute('INSERT OR REPLACE INTO embedding_cache VALUES (?,?,?)',
                                (digest(p['title'] + '\n' + p['text']), self.embedding_model, value))

    def save_extraction(self, project_id, passages, output):
        validate_schema(output, EXTRACTION_SCHEMA)
        by_id = {p['id']: p for p in passages}
        for item in output['entities'] + output['relations']:
            if not item['evidence'] or any(not valid_quote(e, by_id) for e in item['evidence']):
                raise ProviderError('Graph extraction returned a quotation not found in its passage. Retry indexing to try again.')
        names = {e['name'] for e in output['entities']}
        if any(r['source'] not in names or r['target'] not in names for r in output['relations']):
            raise ProviderError('Graph extraction referenced an undeclared entity. Retry indexing.')
        with self.lock, self.db:
            # Ignore results if the underlying content changed during inference.
            if any(not self.db.execute('SELECT 1 FROM passages WHERE id=?', (p['id'],)).fetchone() for p in passages):
                return
            all_text = normalized(' '.join(p['text'] for p in passages))
            entities = {}
            for entity in output['entities']:
                key = normalized(entity['name'])
                if not key:
                    raise ProviderError('Graph extraction returned an empty entity name.')
                # Merge explicit, grounded aliases only, within one investigation and entity type.
                aliases = {key} | {normalized(a) for a in entity['aliases']
                                   if normalized(a) and ' ' + normalized(a) + ' ' in ' ' + all_text + ' '}
                found = set()
                for alias in aliases:
                    row = self.db.execute('SELECT entity_id FROM aliases WHERE project_id=? AND kind=? AND alias=?',
                                          (project_id, entity['kind'], alias)).fetchone()
                    if row:
                        found.add(row[0])
                eid = next(iter(found)) if len(found) == 1 else digest(project_id + entity['kind'] + key)
                self.db.execute('INSERT OR IGNORE INTO entities VALUES (?,?,?,?)', (eid, project_id, entity['name'], entity['kind']))
                for alias in aliases:
                    self.db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?,?,?)', (project_id, entity['kind'], alias, eid))
                entities[entity['name']] = eid
                for evidence in entity['evidence']:
                    self.db.execute('INSERT OR IGNORE INTO mentions VALUES (?,?,?)', (eid, evidence['passage_id'], evidence['quote']))
            for relation in output['relations']:
                source, target = entities[relation['source']], entities[relation['target']]
                # Keep assertions of opposite polarity separate; never overwrite a conflicting claim.
                rid = digest(project_id + source + target + normalized(relation['relation']))
                self.db.execute('INSERT OR IGNORE INTO relations VALUES (?,?,?,?,?,?)',
                                (rid, project_id, source, target, relation['relation'], relation['confidence']))
                for evidence in relation['evidence']:
                    self.db.execute('INSERT OR IGNORE INTO relation_evidence VALUES (?,?,?)',
                                    (rid, evidence['passage_id'], evidence['quote']))
            for p in passages:
                self.db.execute('UPDATE passages SET graph_done=1,graph_error=NULL WHERE id=?', (p['id'],))

    def retrieve(self, question, scope, vector=None, limit=12):
        passages = self.passage_rows(scope)
        by_id = {p['id']: p for p in passages}
        scores, reasons = defaultdict(float), defaultdict(set)
        query_terms = list(dict.fromkeys(terms(question)))
        marks = ','.join('?' for _ in scope)
        if query_terms:
            expression = ' OR '.join('"' + t + '"' for t in query_terms)
            matches = self.rows(f'''SELECT f.id,bm25(passage_fts,0,1.5,1) AS score FROM passage_fts f
                JOIN passages p ON p.id=f.id WHERE passage_fts MATCH ? AND p.project_id IN ({marks})
                ORDER BY score LIMIT 40''', [expression, *scope])
            for rank, match in enumerate(matches):
                scores[match['id']] += 1 / (30 + rank)
                reasons[match['id']].add('keyword')
        if vector:
            matches = []
            for p in passages:
                if p['vector'] and p['vector_model'] == self.embedding_model:
                    v = json.loads(p['vector'])
                    if len(v) != len(vector):
                        continue
                    score = sum(a*b for a,b in zip(vector,v))
                    if score >= 0.3:
                        matches.append((score, p['id']))
            for rank, (_, pid) in enumerate(sorted(matches, reverse=True)[:40]):
                scores[pid] += 1 / (30 + rank)
                reasons[pid].add('semantic')
        # Use matched entity aliases and highly ranked passage mentions as graph entry points.
        seeds = set()
        qnorm = ' ' + normalized(question) + ' '
        for alias in self.rows(f'SELECT * FROM aliases WHERE project_id IN ({marks})', scope):
            if len(alias['alias']) >= 3 and ' ' + alias['alias'] + ' ' in qnorm:
                seeds.add(alias['entity_id'])
        top = sorted(scores, key=scores.get, reverse=True)[:4]
        for pid in top:
            seeds.update(r['entity_id'] for r in self.rows('SELECT entity_id FROM mentions WHERE passage_id=?', (pid,)))
        relations = self.rows(f'''SELECT r.*,e.passage_id,e.quote,s.name AS source_name,t.name AS target_name
            FROM relations r JOIN relation_evidence e ON e.relation_id=r.id
            JOIN entities s ON s.id=r.source JOIN entities t ON t.id=r.target
            WHERE r.project_id IN ({marks})''', scope)
        traversed, frontier, visited = [], seeds, set()
        # Two hops and a fixed evidence cap avoid unbounded expansion through hub concepts.
        for hop in range(2):
            next_frontier = set()
            for rel in relations:
                if len(traversed) >= 40:
                    break
                key = (rel['id'], rel['passage_id'])
                if key in visited or rel['passage_id'] not in by_id:
                    continue
                if rel['source'] in frontier or rel['target'] in frontier:
                    visited.add(key)
                    traversed.append(rel)
                    next_frontier.update((rel['source'], rel['target']))
                    scores[rel['passage_id']] += .7 / (30 + hop*10 + len(traversed))
                    reasons[rel['passage_id']].add('graph')
            frontier = next_frontier - seeds
            seeds.update(frontier)
        selected, counts, used_text, remaining = [], defaultdict(int), set(), 18000
        for pid in sorted(scores, key=lambda pid: (-scores[pid], pid)):
            p = by_id[pid]
            text_hash = digest(normalized(p['text']))
            if counts[p['note_id']] >= 3 or text_hash in used_text or len(p['text']) > remaining:
                continue
            counts[p['note_id']] += 1
            used_text.add(text_hash)
            remaining -= len(p['text'])
            selected.append({k:v for k,v in p.items() if k not in ('vector','vector_model','embedding_error','graph_error')}
                            | {'retrieved_by': sorted(reasons[pid]), 'score': round(scores[pid], 5)})
            if len(selected) >= limit:
                break
        ids = {p['id'] for p in selected}
        graph = [r for r in traversed if r['passage_id'] in ids]
        return {'passages': selected, 'connections': graph, 'mode': 'hybrid' if vector else 'keyword + graph',
                'scope': scope, 'searched_passages': len(passages)}

    def put_answer(self, answer):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO knowledge_answers VALUES (?,?,?)',
                            (answer['id'], answer['project_id'], json.dumps(answer)))
            if answer['status'] == 'complete':
                folder = self.directory / answer['project_id'] / 'knowledge'
                atomic_write(folder / (answer['id'] + '.json'), json.dumps(answer, indent=2, ensure_ascii=False))
                result = answer['result']
                passages = {p['id']: p for p in answer['retrieval']['passages']}
                lines = [f'# {answer["question"]}', '', f'Answered from saved research on {answer["finished_at"]}.', '',
                         'These citations refer to saved Brave answers, not independently fetched source pages.', '']
                for claim in result['claims']:
                    lines += [claim['text'], '']
                    for e in claim['evidence']:
                        p = passages[e['passage_id']]
                        lines += [f'> {e["quote"]}', '', f'Passage `{p["id"]}` in note `{p["note_id"]}`, investigation `{p["project_id"]}`.', '']
                lines += ['## Gaps', '', *['- '+g for g in result['gaps']], '', '## Caveats', '', *['- '+c for c in result['caveats']]]
                atomic_write(folder / (answer['id'] + '.md'), '\n'.join(lines) + '\n')

    def get_answer(self, id_):
        rows = self.rows('SELECT document FROM knowledge_answers WHERE id=?', (id_,))
        if not rows:
            raise KeyError('Knowledge answer not found')
        return json.loads(rows[0]['document'])

    def history(self, project_id):
        return [json.loads(r['document']) for r in self.rows('SELECT document FROM knowledge_answers WHERE project_id=? ORDER BY rowid DESC LIMIT 40', (project_id,))]

    def export_graph(self, project_id):
        with self.lock:
            passages = self.passage_rows([project_id])
            entities = self.rows('''SELECT DISTINCT e.* FROM entities e JOIN mentions m ON m.entity_id=e.id
                                    JOIN passages p ON p.id=m.passage_id WHERE e.project_id=?''', (project_id,))
            relations = self.rows('SELECT * FROM relations WHERE project_id=?', (project_id,))
            for entity in entities:
                entity['aliases'] = [r['alias'] for r in self.rows('SELECT alias FROM aliases WHERE entity_id=?', (entity['id'],))]
                entity['mentions'] = self.rows('SELECT passage_id,quote FROM mentions WHERE entity_id=?', (entity['id'],))
            for relation in relations:
                relation['evidence'] = self.rows('SELECT passage_id,quote FROM relation_evidence WHERE relation_id=?', (relation['id'],))
            document = {'version':1, 'project_id':project_id, 'exported_at':now(), 'entities':entities, 'relations':relations,
                        'passages':[{k:v for k,v in p.items() if k not in ('vector','vector_model','embedding_error','graph_error')}
                                    for p in passages], 'index_status':self.status(project_id)}
            atomic_write(self.directory / project_id / 'knowledge' / 'graph.json', json.dumps(document, indent=2, ensure_ascii=False))
            return document


class Knowledge:
    def __init__(self, store, providers, autostart=True):
        self.store, self.providers = store, providers
        self.index = KnowledgeIndex(store.directory, getattr(providers.config, 'embedding_model', 'qwen3-embedding:0.6b'))
        self.condition = threading.Condition()
        self.pending = set()
        self.stopping = False
        self.ask_lock = threading.Lock()
        self.active_answers = {}
        self.interactive_count = 0
        self.worker = None
        self.store.listeners.append(self.notify)
        for p in store.list():
            self.index.sync_project(p)
            self.notify(p['id'])
        if autostart:
            self.worker = threading.Thread(target=self._index_loop, name='knowledge-index', daemon=True)
            self.worker.start()

    def notify(self, project_id):
        # Called under Store's lock: never perform IO or acquire the index DB lock here.
        with self.condition:
            self.pending.add(project_id)
            self.condition.notify()

    def _index_loop(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.pending and self.interactive_count == 0)
                if self.stopping:
                    return
                project_id = sorted(self.pending)[0]
                self.pending.remove(project_id)
            try:
                self.index.sync_project(self.store.get(project_id))
                self.index_pending(project_id)
            except Exception:
                import traceback
                traceback.print_exc()

    def index_pending(self, project_id):
        rows = self.index.passage_rows([project_id])
        missing = [p for p in rows if (not p['vector'] or p['vector_model'] != self.index.embedding_model) and not p['embedding_error']]
        for start in range(0, len(missing), 8):
            if self._yield_index(project_id):
                return
            batch = missing[start:start+8]
            try:
                uncached = []
                for p in batch:
                    cached = self.index.rows('SELECT vector FROM embedding_cache WHERE content_hash=? AND model=?',
                                             (digest(p['title']+'\n'+p['text']), self.index.embedding_model))
                    if cached:
                        self.index.save_vectors([p], [json.loads(cached[0]['vector'])])
                    else:
                        uncached.append(p)
                if uncached:
                    vectors = self.providers.embed([p['title']+'\n'+p['text'] for p in uncached])
                    self.index.save_vectors(uncached, vectors)
            except Exception as exc:
                message = str(exc) if isinstance(exc, ProviderError) else 'Embedding failed. Check Ollama and retry indexing.'
                with self.index.lock, self.index.db:
                    for p in missing[start:]:
                        self.index.db.execute('UPDATE passages SET embedding_error=? WHERE id=?', (message, p['id']))
                break
        pending = [p for p in rows if not p['graph_done'] and not p['graph_error']]
        for start in range(0, len(pending), 4):
            if self._yield_index(project_id):
                return
            batch = pending[start:start+4]
            try:
                excerpts = excerpts_for(batch)
                output = self.providers.structured(
                    'Extract at most 6 important supported relationships from these saved research passages. '
                    'Each relationship must contain complete source and target objects, with a name, kind, and aliases, plus a precise relation label and confidence. '
                    'For EVERY relationship put one or two exact excerpt IDs in its evidence list. Select IDs from the supplied excerpts; do not write quotes or invent IDs. '
                    'Each relation and both named endpoints must be explicitly supported by its cited excerpts. '
                    'Preserve negation and conflicting assertions. Use aliases only for explicit alternative names in these passages. '
                    'Do not treat a repeated summary as independent corroboration. Empty lists are valid if no supported relationships exist.',
                    {'excerpts':[{'id':id_, 'text':e['quote']} for id_,e in excerpts.items()]}, EXTRACT_MODEL_SCHEMA,
                    priority=20, max_tokens=2200, context_size=16384)
                validate_schema(output, EXTRACT_MODEL_SCHEMA)
                output = graph_from_relations(output, excerpts)
                self.index.save_extraction(project_id, batch, output)
            except Exception as exc:
                message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else 'Graph extraction failed. Check Ollama and retry indexing.'
                with self.index.lock, self.index.db:
                    for p in batch:
                        self.index.db.execute('UPDATE passages SET graph_error=? WHERE id=?', (message, p['id']))
                # Connection errors should not trigger dozens of identical requests.
                if isinstance(exc, ProviderError) and ('reach' in str(exc) or 'HTTP' in str(exc)):
                    with self.index.lock, self.index.db:
                        for p in pending[start+4:]:
                            self.index.db.execute('UPDATE passages SET graph_error=? WHERE id=?', (message, p['id']))
                    break

    def _yield_index(self, project_id):
        with self.condition:
            if self.stopping:
                return True
            if self.interactive_count:
                self.pending.add(project_id)
                return True
        return False

    def ask(self, project_id, question, scope):
        if not isinstance(scope, list) or not scope or len(scope) > 30 or any(not isinstance(id_, str) for id_ in scope):
            raise ValueError('Select between 1 and 30 investigations to search.')
        scope = list(dict.fromkeys(scope))
        self.store.get(project_id)
        for id_ in scope:
            self.store.get(id_)
        with self.ask_lock:
            if self.active_answers.get(project_id) and self.active_answers[project_id].is_alive():
                raise ValueError('A knowledge question is already being answered for this investigation.')
            answer = {'id': uid(), 'project_id': project_id, 'question': question, 'scope': scope,
                      'status': 'queued', 'created_at': now(), 'result': None, 'error': None, 'warnings': []}
            self.index.put_answer(answer)
            with self.condition:
                self.interactive_count += 1
            # The HTTP response is a queued snapshot, not a dict concurrently mutated by the worker.
            thread = threading.Thread(target=self._answer, args=(json.loads(json.dumps(answer)),), name='knowledge-answer', daemon=True)
            self.active_answers[project_id] = thread
            thread.start()
        return answer

    def _answer(self, answer):
        try:
            answer['status'] = 'retrieving'
            self.index.put_answer(answer)
            for id_ in answer['scope']:
                self.index.sync_project(self.store.get(id_))
            vector = None
            if any(self.index.status(id_)['embedded'] for id_ in answer['scope']):
                try:
                    vector = self.providers.embed([answer['question']], priority=0)[0]
                except ProviderError:
                    answer['warnings'].append('Semantic search is unavailable. This answer uses keyword and graph retrieval.')
            else:
                answer['warnings'].append('Embeddings are still being indexed or unavailable. This answer uses keyword and graph retrieval.')
            retrieval = self.index.retrieve(answer['question'], answer['scope'], vector)
            answer['retrieval'] = retrieval
            answer['index_status'] = [dict(project_id=id_, **self.index.status(id_)) for id_ in answer['scope']]
            if any(s['pending'] or s['errors'] for s in answer['index_status']):
                answer['warnings'].append('Some passages are not fully indexed yet. You can ask again after indexing completes.')
            passages = {p['id']:p for p in retrieval['passages']}
            if not passages:
                result = {'status':'insufficient','claims':[], 'gaps':[answer['question']],
                          'caveats':['No relevant passages were retrieved from the selected investigations.']}
            else:
                answer['status'] = 'answering'
                self.index.put_answer(answer)
                excerpts = excerpts_for(passages.values())
                result = self.providers.structured(
                    'Answer the question using ONLY the retrieved saved research passages. Do not use outside knowledge. '
                    'Each factual claim MUST include one or more exact excerpt IDs in its evidence list supporting the claim. '
                    'Choose IDs from the supplied excerpts. Do not write quotes or invent IDs. The application will attach the exact original quotes. Keep claims concise and specific. '
                    'The graph helps locate evidence; relationships are not additional independent sources. '
                    'These are Brave-generated research notes, not verified original webpages. Repeated URLs or overlapping passages are one source. '
                    'Acknowledge contradictions and scope/date limits. Do not force an answer from irrelevant material. '
                    'Use status insufficient with empty claims when the question is unsupported; use partial when only part is supported. '
                    'Gaps must be useful follow-up QUESTIONS. Caveats describe limitations, not uncited factual answers.',
                    {'question':answer['question'], 'passages':[{'id':p['id'],'title':p['title'],
                                                              'fetched_at':p['fetched_at'],'project_id':p['project_id'],
                                                              'source_urls':list(dict.fromkeys(s['url'] for s in p['sources']))} for p in passages.values()],
                     'excerpts':[{'id':id_, 'passage_id':e['passage_id'], 'text':e['quote']} for id_,e in excerpts.items()],
                     'connections':[{'source':r['source_name'],'target':r['target_name'],'relation':r['label'],
                                     'evidence_passage':r['passage_id']} for r in retrieval['connections']]},
                    ANSWER_MODEL_SCHEMA, priority=0, max_tokens=3000, context_size=16384)
                validate_schema(result, ANSWER_MODEL_SCHEMA)
                result = expand_evidence(result, ('claims',), excerpts)
                validate_schema(result, ANSWER_SCHEMA)
                if result['status'] != 'insufficient' and not result['claims']:
                    raise ProviderError('Qwen returned an answer without supported claims. Ask again to retry.')
                if result['status'] == 'insufficient' and result['claims']:
                    raise ProviderError('Qwen returned inconsistent evidence status. Ask again to retry.')
                for claim in result['claims']:
                    if not claim['evidence'] or any(not valid_quote(e, passages) for e in claim['evidence']):
                        raise ProviderError('Qwen produced a citation or quote that does not match the retrieved research. The answer was rejected; ask again to retry.')
            answer.update(status='complete', result=result, finished_at=now())
        except Exception as exc:
            answer.update(status='error', error=str(exc) if isinstance(exc, (ProviderError, ValueError)) else 'Unable to answer from the knowledge index. Check the app terminal.')
            if not isinstance(exc, (ProviderError, ValueError)):
                import traceback
                traceback.print_exc()
        try:
            self.index.put_answer(answer)
        finally:
            with self.condition:
                self.interactive_count -= 1
                self.condition.notify_all()

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        if self.notify in self.store.listeners:
            self.store.listeners.remove(self.notify)
        if self.worker:
            self.worker.join(timeout=1)
        # In-flight inference may still be saving; process shutdown handles final closure.
        if (not self.worker or not self.worker.is_alive()) and not any(t.is_alive() for t in self.active_answers.values()):
            self.index.db.close()
