import threading

from .providers import ProviderError
from .store import now, normalized, question


def string(limit=2000):
    return {'type': 'string', 'maxLength': limit}


def array(items, limit=30):
    return {'type': 'array', 'items': items, 'maxItems': limit}


def obj(**properties):
    return {'type': 'object', 'properties': properties, 'required': list(properties), 'additionalProperties': False}


CONFIDENCE = {'type': 'string', 'enum': ['low', 'medium', 'high']}
PLAN_SCHEMA = obj(rationale=string(4000), questions=array(obj(question=string(1000), reason=string(1000), parent_id=string(100)), 10))
BRIEF_SCHEMA = obj(summary=string(6000), findings=array(obj(claim=string(), evidence=array(string(100)), confidence=CONFIDENCE)),
                   gaps=array(string()), contradictions=array(string()), next_direction=string(3000),
                   relations=array(obj(source=string(160), relation=string(120), target=string(160),
                                       evidence=array(string(100)), confidence=CONFIDENCE), 50))


class Paused(Exception):
    pass


class Engine:
    def __init__(self, store, providers):
        self.store, self.providers = store, providers
        self.lock = threading.Lock()
        self.threads = {}

    def start(self, id_):
        with self.lock:
            p = self.store.get(id_)
            if p['running']:
                raise ValueError('This investigation is already running.')
            if not self.providers.config.brave_key:
                raise ValueError('Add BRAVE_SEARCH_API_KEY to .env and restart before starting research.')
            # Reserve synchronously so two Start clicks cannot schedule duplicate jobs.
            self.store.mutate(id_, lambda p: p.update(running=True, stop_requested=False, error=None, stop_reason=None))
            thread = threading.Thread(target=self._worker, args=(id_,), daemon=True)
            self.threads[id_] = thread
            thread.start()

    def pause(self, id_):
        def update(p):
            if p['running']:
                p['stop_requested'] = True
                self.event(p, 'Pause requested. Finishing the current provider call and saving its result.')
        return self.store.mutate(id_, update)

    @staticmethod
    def event(p, message):
        p['events'].append({'at': now(), 'message': message})
        p['events'] = p['events'][-150:]

    def checkpoint(self, id_):
        p = self.store.get(id_)
        if p['stop_requested']:
            raise Paused()
        return p

    def phase(self, id_, phase, message):
        def update(p):
            p['phase'] = phase
            if phase == 'complete':
                p['stop_reason'] = message
            self.event(p, message)
        self.store.mutate(id_, update)

    @staticmethod
    def context(p):
        # Bounded prompt: rolling briefing + recent evidence + the complete question
        # index up to a character budget. Full originals always remain on disk.
        answered = [q for q in p['questions'] if q.get('answer')]
        notes, remaining = [], 42000
        for q in reversed(answered):
            if remaining <= 0:
                break
            excerpt = q['answer']['markdown'][:min(6000, remaining)]
            remaining -= len(excerpt)
            notes.append({'id': q['id'], 'question': q['text'], 'answer': excerpt,
                          'sources': q['answer']['sources'][:8]})
        previous = next((c['brief'] for c in reversed(p['cycles']) if c.get('brief')), None)
        index, remaining = [], 16000
        for q in reversed(p['questions']):
            if remaining < len(q['text']):
                break
            index.append({'id': q['id'], 'question': q['text'], 'status': q['status']})
            remaining -= len(q['text'])
        return {'root_question': p['question'], 'previous_briefing': previous,
                'question_index': index, 'recent_evidence': list(reversed(notes))}

    def _worker(self, id_):
        try:
            while True:
                p = self.checkpoint(id_)
                budget = p['settings']['answer_budget']
                completed = sum(bool(q.get('answer')) for q in p['questions'])
                unfinished = p['cycles'] and not p['cycles'][-1].get('brief')
                if budget and completed >= budget:
                    if unfinished and any(q.get('answer') and q['cycle'] == p['cycles'][-1]['number'] for q in p['questions']):
                        self._brief(id_)
                    self.phase(id_, 'complete', 'Answer budget reached. Increase the budget to continue.')
                    break
                if not unfinished:
                    def new_cycle(p):
                        p['cycles'].append({'number': len(p['cycles']) + 1, 'created_at': now(),
                                            'plan': None, 'planned': False, 'brief': None})
                    p = self.store.mutate(id_, new_cycle)
                cycle = p['cycles'][-1]
                if not cycle['planned']:
                    self._plan(id_)
                p = self.checkpoint(id_)
                n = p['cycles'][-1]['number']
                active = [q for q in p['questions'] if q['cycle'] == n and q['status'] != 'skipped']
                if not active:
                    self.phase(id_, 'complete', 'No new questions were generated. Add a question and press Start to explore further.')
                    # A new Start should plan a fresh cycle, not loop on an empty plan.
                    self.store.mutate(id_, lambda p: p['cycles'].pop())
                    break
                self._research(id_)
                self.checkpoint(id_)
                self._brief(id_)
                self.checkpoint(id_)
        except Paused:
            self.phase(id_, 'paused', 'Paused. Press Start to continue from saved progress.')
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ProviderError, ValueError)) else 'An unexpected error interrupted this cycle. Saved notes are intact. Check the terminal and retry.'
            if not isinstance(exc, (ProviderError, ValueError)):
                import traceback
                traceback.print_exc()
            def fail(p):
                p.update(phase='error', error=message)
                for q in p['questions']:
                    if q['status'] == 'researching':
                        q['status'] = 'pending'
                self.event(p, message)
            self.store.mutate(id_, fail)
        finally:
            self.store.mutate(id_, lambda p: p.update(running=False, stop_requested=False))

    def _plan(self, id_):
        self.checkpoint(id_)
        self.phase(id_, 'planning', 'Qwen is planning the next cycle from saved evidence and open questions.')
        p = self.checkpoint(id_)
        n = p['cycles'][-1]['number']
        size = p['settings']['questions_per_cycle']
        if p['settings']['answer_budget']:
            size = min(size, max(0, p['settings']['answer_budget'] - sum(bool(q.get('answer')) for q in p['questions'])))
        pending = sorted((q for q in p['questions'] if q['status'] == 'pending' and q['cycle'] in (None, n)),
                         key=lambda q: (q['origin'] == 'qwen', q['created_at']))[:size]
        room = size - len(pending)
        context = {**self.context(p), 'cycle': n, 'questions_to_generate': room,
                   'researcher_questions_for_this_cycle': [{'id': q['id'], 'question': q['text']} for q in pending]}
        plan = self.providers.structured(
            'You are the local research planner. Generate the requested number of specific, non-duplicate follow-up questions. '
            'Build on actual findings, uncertainties, and contradictions in previous_briefing and recent_evidence. '
            'Prioritize missing evidence and falsifiable questions over broad repetition. Respect researcher questions. '
            'When questions_to_generate is zero return an empty questions list and explain the plan for the researcher questions. '
            'For each generated question set parent_id to the question ID that motivated it, or empty string for the root. '
            'Do not answer the questions. Research continues until the researcher pauses; seek useful new directions.', context, PLAN_SCHEMA)
        def save(p):
            current = p['cycles'][-1]
            current.update(plan=plan['rationale'], planned=True)
            # Honor questions submitted while Qwen was planning, ahead of AI proposals.
            available = sorted((q for q in p['questions'] if q['status'] == 'pending' and q['cycle'] in (None, n)),
                               key=lambda q: (q['origin'] == 'qwen', q['created_at']))[:size]
            for q in available:
                q['cycle'] = n
            seen = {normalized(q['text']) for q in p['questions']}
            ids = {q['id'] for q in p['questions']}
            for item in plan['questions']:
                if len(available) >= size:
                    break
                text = item['question'].strip()
                if not text or normalized(text) in seen:
                    continue
                q = question(text, 'qwen', item['reason'], item['parent_id'] if item['parent_id'] in ids else None)
                q['cycle'] = n
                p['questions'].append(q)
                available.append(q)
                seen.add(normalized(text))
            self.event(p, f'Cycle {n} planned with {len(available)} questions.')
        self.store.mutate(id_, save)

    def _research(self, id_):
        self.phase(id_, 'researching', 'Researching this cycle with Brave Answers.')
        while True:
            p = self.checkpoint(id_)
            n = p['cycles'][-1]['number']
            q = next((q for q in p['questions'] if q['cycle'] == n and q['status'] == 'pending'), None)
            if not q:
                break
            self.store.mutate(id_, lambda p: self._mark(p, q['id'], 'researching'))
            answer = self.providers.answer(q['text'], p['question'])
            answer['fetched_at'] = now()
            def save(p):
                current = next(x for x in p['questions'] if x['id'] == q['id'])
                current.update(answer=answer, status='answered')
                self.event(p, f'Saved answer: {current["text"]}')
            self.store.mutate(id_, save)
            self.checkpoint(id_)

    @staticmethod
    def _mark(p, qid, status):
        next(q for q in p['questions'] if q['id'] == qid)['status'] = status

    def _brief(self, id_):
        self.phase(id_, 'briefing', 'Qwen is writing the briefing, identifying gaps, and connecting evidence in the graph.')
        p = self.checkpoint(id_)
        context = self.context(p)
        context['cycle'] = p['cycles'][-1]['number']
        brief = self.providers.structured(
            'You are the local research synthesizer. Write a cumulative briefing for the root question using only supplied evidence. '
            'Separate supported findings from uncertainty. Every finding and concept relation must cite one or more IDs from recent_evidence. '
            'Never invent sources or evidence IDs. Relations connect short named concepts, and must be supported by the cited answers. '
            'Extract at most 20 useful relations. Confidence describes the evidence, not your certainty of wording. '
            'Explicitly report contradictory findings and unresolved questions; do not fabricate contradictions. '
            'The summary and next_direction should preserve the most important earlier findings and guide the next research cycle. '
            'Brave answers are secondary evidence, not independent verification. Keep all prose concise.', context, BRIEF_SCHEMA)
        allowed = {note['id'] for note in context['recent_evidence']}
        for group in ('findings', 'relations'):
            for item in brief[group]:
                if not item['evidence'] or not set(item['evidence']).issubset(allowed):
                    raise ProviderError('Qwen cited an unknown evidence ID. The briefing was not saved; press Start to retry synthesis without repeating answered questions.')
        def save(p):
            p['cycles'][-1].update(brief=brief, finished_at=now())
            self.event(p, f'Cycle {p["cycles"][-1]["number"]} briefing saved. Expanding from these findings.')
        self.store.mutate(id_, save)
