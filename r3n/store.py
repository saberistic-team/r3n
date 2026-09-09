import copy
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid.uuid4().hex[:12]


def normalized(text):
    return re.sub(r'[^\w]+', ' ', text.casefold()).strip()


def question(text, origin='researcher', reason='', parent=None):
    return {'id': uid(), 'text': text.strip(), 'origin': origin, 'reason': reason, 'parent': parent,
            'status': 'pending', 'cycle': None, 'created_at': now(), 'answer': None}


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def graph_for(project):
    nodes = [{'id': project['id'], 'label': project['question'], 'type': 'investigation'}]
    edges, seen = [], {project['id']}

    def node(id_, label, type_, **extra):
        if id_ not in seen:
            nodes.append({'id': id_, 'label': label, 'type': type_, **extra})
            seen.add(id_)

    for q in project['questions']:
        if q['status'] == 'skipped':
            continue
        node(q['id'], q['text'], 'question', status=q['status'], cycle=q['cycle'])
        edges.append({'source': q['parent'] if q['parent'] in {x['id'] for x in project['questions']} else project['id'],
                      'target': q['id'], 'label': 'asks', 'evidence': []})
        if q.get('answer'):
            for source in q['answer']['sources']:
                sid = 'src-' + hashlib.sha256(source['url'].encode()).hexdigest()[:12]
                node(sid, source.get('title') or source['url'], 'source', url=source['url'])
                edges.append({'source': q['id'], 'target': sid, 'label': 'cites', 'evidence': [q['id']]})
    answered = {q['id'] for q in project['questions'] if q.get('answer')}
    for cycle in project['cycles']:
        brief = cycle.get('brief')
        if not brief:
            continue
        for relation in brief.get('relations', []):
            evidence = [id_ for id_ in relation['evidence'] if id_ in answered]
            if not evidence:
                continue
            ids = []
            for label in (relation['source'], relation['target']):
                id_ = 'concept-' + hashlib.sha256(normalized(label).encode()).hexdigest()[:12]
                node(id_, label, 'concept')
                ids.append(id_)
                for qid in evidence:
                    edges.append({'source': qid, 'target': id_, 'label': 'mentions', 'evidence': [qid]})
            edges.append({'source': ids[0], 'target': ids[1], 'label': relation['relation'],
                          'evidence': evidence, 'confidence': relation['confidence'], 'cycle': cycle['number']})
    unique = {}
    for edge in edges:
        key = (edge['source'], edge['target'], edge['label'])
        if key in unique:
            unique[key]['evidence'] = sorted(set(unique[key]['evidence'] + edge['evidence']))
        else:
            unique[key] = edge
    return {'nodes': nodes, 'edges': list(unique.values())}


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.listeners = []
        self.db = sqlite3.connect(self.directory / 'research.sqlite3', check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, document TEXT NOT NULL)')
        self.db.commit()
        for project in self.list():
            def recover(p):
                if p['running']:
                    p.update(running=False, phase='paused', stop_requested=False,
                             error='The app restarted. Saved answers are intact; resume the cycle. An interrupted Brave request may already have been billed.')
                    for q in p['questions']:
                        if q['status'] == 'researching':
                            q['status'] = 'pending'
            self.mutate(project['id'], recover)

    def list(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT document FROM projects')]

    def get(self, id_):
        with self.lock:
            row = self.db.execute('SELECT document FROM projects WHERE id=?', (id_,)).fetchone()
            if not row:
                raise KeyError('Investigation not found')
            return json.loads(row[0])

    def create(self, root, settings):
        project = {'id': uid(), 'question': root, 'created_at': now(), 'updated_at': now(), 'phase': 'idle',
                   'running': False, 'stop_requested': False, 'error': None, 'settings': settings,
                   'questions': [question(root, 'seed')], 'cycles': [], 'events': []}
        with self.lock:
            self.db.execute('INSERT INTO projects VALUES (?, ?)', (project['id'], json.dumps(project)))
            self.db.commit()
            self.export(project)
            for listener in self.listeners:
                listener(project['id'])
        return project

    def mutate(self, id_, fn):
        with self.lock:
            project = self.get(id_)
            fn(project)
            project['updated_at'] = now()
            self.db.execute('UPDATE projects SET document=? WHERE id=?', (json.dumps(project), id_))
            self.db.commit()
            self.export(project)
            for listener in self.listeners:
                listener(id_)
            return copy.deepcopy(project)

    def export(self, p):
        folder = self.directory / p['id']
        atomic_write(folder / 'investigation.json', json.dumps(p, indent=2, ensure_ascii=False))
        index = [f'# {p["question"]}', '', f'Updated: {p["updated_at"]}', '', '## Research notes', '']
        for q in p['questions']:
            if not q.get('answer'):
                continue
            a = q['answer']
            name = f'notes/{q["id"]}.md'
            index.append(f'- [{q["text"]}]({name})')
            lines = [f'# {q["text"]}', '', f'- Question ID: `{q["id"]}`', f'- Cycle: {q["cycle"]}',
                     f'- Answered: {a["fetched_at"]}', '- Provider: Brave Answers', '', a['markdown'], '', '## Sources', '']
            for s in a['sources']:
                lines.append(f'- [{s.get("title") or s["url"]}]({s["url"]})')
                if s.get('snippet'):
                    lines.append('  ' + s['snippet'].replace('\n', ' '))
            if a.get('warnings'):
                lines += ['', '## Source notes', '', *a['warnings']]
            atomic_write(folder / name, '\n'.join(lines) + '\n')
            atomic_write(folder / f'raw/{q["id"]}.json', json.dumps(a, indent=2, ensure_ascii=False))
        index += ['', '## Cycle briefings', '']
        for cycle in p['cycles']:
            n = cycle['number']
            if cycle.get('plan'):
                atomic_write(folder / f'plans/cycle-{n:03d}.md', f'# Cycle {n} plan\n\n{cycle["plan"]}\n\n' +
                             '\n'.join(f'- {q["text"]}\n  - {q["reason"]}' for q in p['questions'] if q['cycle'] == n) + '\n')
            if cycle.get('brief'):
                brief = cycle['brief']
                lines = [f'# Cycle {n} briefing', '', brief['summary'], '', '## Findings', '']
                for finding in brief['findings']:
                    refs = ', '.join(f'[note {id_}](../notes/{id_}.md)' for id_ in finding['evidence'])
                    lines.append(f'- {finding["claim"]} ({finding["confidence"]}) — {refs}')
                lines += ['', '## Open questions', '', *[f'- {g}' for g in brief['gaps']], '', '## Conflicting evidence', '',
                          *[f'- {c}' for c in brief['contradictions']], '', '## Suggested next direction', '', brief['next_direction']]
                atomic_write(folder / f'briefings/cycle-{n:03d}.md', '\n'.join(lines) + '\n')
                index.append(f'- [Cycle {n}](briefings/cycle-{n:03d}.md)')
        graph = graph_for(p)
        atomic_write(folder / 'graph.json', json.dumps(graph, indent=2, ensure_ascii=False))
        # Safe opaque Mermaid IDs and JSON-quoted labels; graph.json retains full labels.
        graph_ids = {node['id']: f'n{i}' for i, node in enumerate(graph['nodes'])}
        safe = lambda s: re.sub(r'[<>"`\[\]{}|\\\n\r]', ' ', s)[:100]
        mermaid = ['graph LR'] + [f'  {graph_ids[n["id"]]}["{safe(n["label"])}"]' for n in graph['nodes']]
        mermaid += [f'  {graph_ids[e["source"]]} -->|{safe(e["label"])}| {graph_ids[e["target"]]}' for e in graph['edges']]
        atomic_write(folder / 'graph.mmd', '\n'.join(mermaid) + '\n')
        atomic_write(folder / 'README.md', '\n'.join(index) + '\n')
