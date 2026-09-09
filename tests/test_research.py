import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from r3n.engine import Engine, PLAN_SCHEMA
from r3n.providers import Providers, ProviderError, parse_brave, read_sse, validate_schema
from r3n.server import settings
from r3n.store import Store, graph_for, question


class FakeProviders:
    """Deterministic provider fixture. No network calls or model downloads."""
    config = SimpleNamespace(brave_key='fixture-only')

    def __init__(self):
        self.calls = []
        self.contexts = []
        self.on_answer = None
        self.on_plan = None
        self.fail_brief = False
        self.invalid_evidence = False

    def answer(self, text, root):
        self.calls.append(text)
        if self.on_answer:
            self.on_answer()
        return {'markdown': 'Trees cool streets through shade and evapotranspiration. [Source](https://example.org/trees)',
                'sources': [{'url': 'https://example.org/trees', 'title': 'Trees research'}], 'usage': {}, 'raw': 'fixture', 'warnings': []}

    def structured(self, instructions, context, schema):
        self.contexts.append(context)
        if schema == PLAN_SCHEMA:
            if self.on_plan:
                self.on_plan()
            return {'rationale': 'Investigate gaps in previous evidence.', 'questions': [
                {'question': f'How does tree cooling vary in region {context["cycle"]}-{i}?', 'reason': 'Close an evidence gap.',
                 'parent_id': context['question_index'][0]['id']}
                for i in range(context['questions_to_generate'])]}
        if self.fail_brief:
            raise ProviderError('Fixture synthesis failure')
        qid = 'unknown-id' if self.invalid_evidence else context['recent_evidence'][-1]['id']
        return {'summary': 'Trees can cool streets.', 'findings': [{'claim': 'Shade reduces heat.', 'evidence': [qid], 'confidence': 'medium'}],
                'gaps': ['Which climate conditions matter?'], 'contradictions': [], 'next_direction': 'Compare climates.',
                'relations': [{'source': 'Trees', 'target': 'Street heat', 'relation': 'reduces', 'evidence': [qid], 'confidence': 'medium'}]}


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.providers = FakeProviders()
        self.engine = Engine(self.store, self.providers)
        self.p = self.store.create('How do trees cool cities?', {'questions_per_cycle': 2, 'answer_budget': 4})
        self.id = self.p['id']

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def run_engine(self):
        self.engine.start(self.id)
        self.engine.threads[self.id].join(timeout=8)
        self.assertFalse(self.engine.threads[self.id].is_alive())
        return self.store.get(self.id)

    def test_two_cycles_feed_briefing_into_next_plan_and_export(self):
        p = self.run_engine()
        self.assertEqual(p['phase'], 'complete')
        self.assertEqual(len(self.providers.calls), 4)
        self.assertEqual(len(p['cycles']), 2)
        second_plan = [c for c in self.providers.contexts if c.get('cycle') == 2 and 'questions_to_generate' in c][0]
        self.assertEqual(second_plan['previous_briefing']['gaps'], ['Which climate conditions matter?'])
        self.assertTrue(second_plan['recent_evidence'])
        folder = Path(self.tmp.name) / self.id
        self.assertEqual(len(list((folder / 'notes').glob('*.md'))), 4)
        self.assertEqual(len(list((folder / 'briefings').glob('*.md'))), 2)
        graph = json.loads((folder / 'graph.json').read_text())
        self.assertIn('source', {n['type'] for n in graph['nodes']})
        self.assertIn('concept', {n['type'] for n in graph['nodes']})
        ids = {n['id'] for n in graph['nodes']}
        self.assertTrue(all(e['source'] in ids and e['target'] in ids for e in graph['edges']))

    def test_pause_saves_inflight_answer_and_resume_does_not_repeat(self):
        self.providers.on_answer = lambda: self.engine.pause(self.id)
        p = self.run_engine()
        self.assertEqual(p['phase'], 'paused')
        self.assertEqual(sum(bool(q['answer']) for q in p['questions']), 1)
        self.assertFalse(p['running'])
        self.providers.on_answer = None
        p = self.run_engine()
        self.assertEqual(p['phase'], 'complete')
        self.assertEqual(self.providers.calls.count(self.p['question']), 1)
        self.assertEqual(len(self.providers.calls), 4)

    def test_brief_failure_retry_keeps_paid_answers(self):
        self.providers.fail_brief = True
        p = self.run_engine()
        self.assertEqual(p['phase'], 'error')
        self.assertEqual(len(self.providers.calls), 2)
        self.providers.fail_brief = False
        p = self.run_engine()
        self.assertEqual(p['phase'], 'complete')
        self.assertEqual(len(self.providers.calls), 4)

    def test_rejects_hallucinated_evidence(self):
        self.providers.invalid_evidence = True
        p = self.run_engine()
        self.assertEqual(p['phase'], 'error')
        self.assertIn('unknown evidence', p['error'])
        self.assertIsNone(p['cycles'][-1]['brief'])
        self.assertFalse(any(n['type'] == 'concept' for n in graph_for(p)['nodes']))

    def test_human_question_added_during_planning_gets_priority(self):
        def add():
            self.store.mutate(self.id, lambda p: p['questions'].append(question('How much water do these trees need?')))
            self.providers.on_plan = None
        self.providers.on_plan = add
        p = self.run_engine()
        self.assertEqual(self.providers.calls[1], 'How much water do these trees need?')
        self.assertEqual(len([q for q in p['questions'] if q['cycle'] == 1]), 2)

    def test_pause_during_plan_makes_no_brave_calls(self):
        self.providers.on_plan = lambda: self.engine.pause(self.id)
        p = self.run_engine()
        self.assertEqual(p['phase'], 'paused')
        self.assertEqual(self.providers.calls, [])
        self.assertTrue(p['cycles'][-1]['planned'])

    def test_unlimited_mode_continues_until_paused(self):
        self.store.mutate(self.id, lambda p: p['settings'].update(answer_budget=0))
        self.providers.on_answer = lambda: self.engine.pause(self.id) if len(self.providers.calls) == 5 else None
        p = self.run_engine()
        self.assertEqual(len(p['cycles']), 3)
        self.assertEqual(len(self.providers.calls), 5)
        self.assertEqual(p['phase'], 'paused')

    def test_lowered_budget_does_not_start_more_paid_requests(self):
        self.providers.on_answer = lambda: self.engine.pause(self.id)
        self.run_engine()
        self.providers.on_answer = None
        self.store.mutate(self.id, lambda p: p['settings'].update(answer_budget=1))
        p = self.run_engine()
        self.assertEqual(len(self.providers.calls), 1)
        self.assertEqual(p['phase'], 'complete')

    def test_restart_recovers_running_job(self):
        def dirty(p):
            p.update(running=True, phase='researching')
            p['questions'][0]['status'] = 'researching'
        self.store.mutate(self.id, dirty)
        recovered = Store(Path(self.tmp.name))
        p = recovered.get(self.id)
        self.assertEqual(p['phase'], 'paused')
        self.assertEqual(p['questions'][0]['status'], 'pending')
        self.assertFalse(p['running'])
        recovered.db.close()

    def test_duplicate_start_rejected(self):
        entered, release = threading.Event(), threading.Event()
        def wait():
            entered.set()
            release.wait(5)
        self.providers.on_plan = wait
        self.engine.start(self.id)
        self.assertTrue(entered.wait(3))
        with self.assertRaises(ValueError):
            self.engine.start(self.id)
        self.engine.pause(self.id)
        release.set()
        self.engine.threads[self.id].join(5)


class ProviderTests(unittest.TestCase):
    def test_sse_multiline_events(self):
        response = io.BytesIO(b': ping\n\ndata: {"a":\ndata: 1}\n\ndata: [DONE]\n\n')
        self.assertEqual(list(read_sse(response)), ['{"a":\n1}', '[DONE]'])

    def test_citations_and_usage_preserved(self):
        r = parse_brave('A result.<citation>{"url":"https://example.com","number":1,"snippet":"Evidence"}</citation><usage>{"X-Request-Total-Cost":0.1}</usage>')
        self.assertEqual(r['markdown'], 'A result.')
        self.assertEqual(r['sources'][0]['snippet'], 'Evidence')
        self.assertEqual(r['usage']['X-Request-Total-Cost'], .1)

    def test_unsafe_citation_url_omitted(self):
        r = parse_brave('Text<citation>{"url":"javascript:alert(1)"}</citation>')
        self.assertEqual(r['sources'], [])
        self.assertTrue(r['warnings'])

    def test_repeated_citation_spans_share_one_source_entry(self):
        tag = '<citation>{"url":"https://example.org/source","number":1}</citation>'
        text = 'One finding.' + tag + ' Another finding.' + tag
        r = parse_brave(text)
        self.assertEqual(len(r['sources']), 1)
        self.assertEqual(r['raw'], text)

    def test_stream_fragments_and_correct_request_contract(self):
        chunks = ['An answer.<cit', 'ation>{"url":"https://example.org"}</citation>']
        stream = ''.join('data: ' + json.dumps({'choices':[{'delta':{'content':c}}]}) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
        config = SimpleNamespace(brave_key='fake', brave_url='https://api.search.brave.com/res/v1/chat/completions')
        with patch('r3n.providers.request', return_value=io.BytesIO(stream.encode())) as transport:
            answer = Providers(config).answer('Question?', 'Root?')
        payload = transport.call_args.args[1]
        self.assertTrue(payload['stream'])
        self.assertTrue(payload['enable_citations'])
        self.assertEqual(len(payload['messages']), 1)
        self.assertEqual(answer['markdown'], 'An answer.')
        self.assertEqual(len(answer['sources']), 1)

    def test_incomplete_stream_is_not_saved_as_a_complete_answer(self):
        config = SimpleNamespace(brave_key='fake', brave_url='unused')
        stream = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        with patch('r3n.providers.request', return_value=io.BytesIO(stream)), self.assertRaises(ProviderError):
            Providers(config).answer('Question?', 'Root?')

    def test_structured_validation_and_limits(self):
        with self.assertRaises(ValueError):
            validate_schema({'rationale': 'A plan', 'questions': [{'question': 123}]}, PLAN_SCHEMA)
        with self.assertRaises(ValueError):
            settings({'questions_per_cycle': True})
        with self.assertRaises(ValueError):
            settings({'answer_budget': -1})

    def test_local_model_contract_disables_thinking_and_validates_json(self):
        config = SimpleNamespace(model='qwen3.8:27b-mlx', ollama_url='http://127.0.0.1:11434')
        content = {'rationale': 'Investigate the evidence.', 'questions': []}
        output = json.dumps({'message': {'content': json.dumps(content)}, 'done': True}).encode()
        with patch('r3n.providers.request', return_value=io.BytesIO(output)) as transport:
            result = Providers(config).structured('Plan', {}, PLAN_SCHEMA)
        payload = transport.call_args.args[1]
        self.assertFalse(payload['think'])
        self.assertEqual(payload['model'], config.model)
        self.assertEqual(payload['format'], PLAN_SCHEMA)
        self.assertEqual(result, content)


if __name__ == '__main__':
    unittest.main()
