import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from r3n.knowledge import ANSWER_SCHEMA, EXTRACTION_SCHEMA, ANSWER_MODEL_SCHEMA, EXTRACT_MODEL_SCHEMA, Knowledge, KnowledgeIndex, chunk_text, graph_from_relations
from r3n.providers import ProviderError
from r3n.store import Store, now, question


class KnowledgeProviders:
    config = SimpleNamespace(embedding_model='fixture-embed', model='fixture-qwen', brave_key='')

    def __init__(self):
        self.embed_calls = 0
        self.model_calls = 0
        self.bad_quote = False
        self.fail_embed = False
        self.entered = None
        self.release = None

    def embed(self, texts, priority=20):
        self.embed_calls += 1
        if self.fail_embed:
            raise ProviderError('Embedding provider unavailable')
        return [[1., 0.] if any(t in text.lower() for t in ('heat', 'cooling', 'shade')) else [0., 1.] for text in texts]

    def structured(self, instructions, context, schema, priority=10, **options):
        self.model_calls += 1
        if self.entered:
            self.entered.set()
            self.release.wait(5)
        if schema == EXTRACT_MODEL_SCHEMA:
            return {'relations': []}
        if schema != ANSWER_MODEL_SCHEMA:
            raise AssertionError('Unexpected schema')
        p = context['passages'][0]
        if 'unsupported' in context['question']:
            return {'status':'insufficient','claims':[], 'gaps':[context['question']], 'caveats':['Not supported by these passages.']}
        return {'status':'answered','claims':[{'text':'A finding from the saved research.',
                'evidence':['fabricated-evidence-id' if self.bad_quote else context['excerpts'][0]['id']]}],
                'gaps':['What evidence is missing?'], 'caveats':['Based on saved Brave answers.']}


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.providers = KnowledgeProviders()
        self.k = Knowledge(self.store, self.providers, autostart=False)
        self.p = self.store.create('Urban environments', {'questions_per_cycle':2,'answer_budget':0})
        self.other = self.store.create('Private second investigation', {'questions_per_cycle':2,'answer_budget':0})

    def tearDown(self):
        for t in self.k.active_answers.values():
            t.join(5)
        self.k.close()
        self.store.db.close()
        self.tmp.cleanup()

    def add_note(self, text, title='Study', project=None):
        project = project or self.p
        q = question(title)
        q.update(status='answered',cycle=1,answer={'markdown':text,'fetched_at':now(),
                   'sources':[{'url':'https://example.org/shared-source'}], 'raw':text,'usage':{},'warnings':[]})
        p = self.store.mutate(project['id'], lambda p:p['questions'].append(q))
        self.k.index.sync_project(p)
        return q

    def ask(self, text, scope=None):
        answer = self.k.ask(self.p['id'], text, scope or [self.p['id']])
        self.k.active_answers[self.p['id']].join(5)
        return self.k.index.get_answer(answer['id'])

    def test_chunks_preserve_exact_offsets_and_cover_long_text(self):
        text = ('A sentence about heat.\n\n' * 240) + 'Final fact.'
        chunks = list(chunk_text(text))
        self.assertGreater(len(chunks), 2)
        for start,end,body in chunks:
            self.assertEqual(text[start:end], body)
            self.assertLessEqual(len(body), 1400)
        self.assertEqual(chunks[0][0], 0)
        self.assertEqual(chunks[-1][1], len(text))
        self.assertTrue(all(a[1] >= b[0] for a,b in zip(chunks,chunks[1:])))

    def test_migration_does_not_modify_research_and_is_idempotent(self):
        q = self.add_note('Shade reduces street heat substantially.', 'Trees')
        before = self.store.get(self.p['id'])
        self.k.index.sync_project(before)
        ids = [p['id'] for p in self.k.index.passage_rows([self.p['id']])]
        self.k.index.sync_project(before)
        self.assertEqual(ids, [p['id'] for p in self.k.index.passage_rows([self.p['id']])])
        self.assertEqual(self.store.get(self.p['id']), before)
        self.assertTrue((Path(self.tmp.name)/self.p['id']/f'notes/{q["id"]}.md').exists())

    def test_keyword_search_respects_selected_investigations(self):
        self.add_note('Trees provide cooling in urban streets.', 'Trees')
        self.add_note('Secret cooling experiment with confidential findings.', 'Secret', self.other)
        r = self.k.index.retrieve('cooling', [self.p['id']])
        self.assertTrue(r['passages'])
        self.assertTrue(all(p['project_id'] == self.p['id'] for p in r['passages']))
        r = self.k.index.retrieve('cooling', [self.p['id'],self.other['id']])
        self.assertEqual({p['project_id'] for p in r['passages']}, {self.p['id'],self.other['id']})

    def test_semantic_search_finds_passage_without_shared_keywords(self):
        self.add_note('Evapotranspiration moderates ambient temperatures.', 'Physiology')
        passage = self.k.index.passage_rows([self.p['id']])[0]
        self.k.index.save_vectors([passage], [[1.,0.]])
        r = self.k.index.retrieve('shade cooling', [self.p['id']], [1.,0.])
        self.assertEqual(r['passages'][0]['retrieved_by'], ['semantic'])

    def test_incremental_index_reuses_embeddings(self):
        self.add_note('Trees reduce urban heat through shade.', 'Heat')
        self.k.index_pending(self.p['id'])
        calls = self.providers.embed_calls
        self.k.index.sync_project(self.store.get(self.p['id']))
        self.k.index_pending(self.p['id'])
        self.assertEqual(calls, self.providers.embed_calls)
        self.add_note('Water use changes by season.', 'Water')
        self.k.index_pending(self.p['id'])
        status = self.k.index.status(self.p['id'])
        self.assertEqual(status['passages'], 2)
        self.assertEqual(status['embedded'], 2)
        self.assertEqual(status['graph_indexed'], 2)

    def test_graph_expansion_retrieves_connected_evidence(self):
        self.add_note('The Alpha evaluation concerns Zephyr.', 'Alpha')
        self.add_note('Zephyr reduces latency in remote regions.', 'Regional outcomes')
        passages = self.k.index.passage_rows([self.p['id']])
        a,b = passages
        def evidence(p): return [{'passage_id':p['id'],'quote':p['text']}]
        self.k.index.save_extraction(self.p['id'], passages, {
            'entities':[{'name':'Zephyr','kind':'product','aliases':[], 'evidence':evidence(a)+evidence(b)},
                        {'name':'latency','kind':'concept','aliases':[], 'evidence':evidence(b)}],
            'relations':[{'source':'Zephyr','target':'latency','relation':'reduces','confidence':'medium','evidence':evidence(b)}]})
        r=self.k.index.retrieve('Alpha evaluation', [self.p['id']])
        related=next(p for p in r['passages'] if p['id']==b['id'])
        self.assertIn('graph', related['retrieved_by'])
        self.assertTrue(r['connections'])
        with self.assertRaises(ProviderError):
            self.k.index.save_extraction(self.p['id'], passages, {'entities':[{'name':'Bad','kind':'concept','aliases':[],
                'evidence':[{'passage_id':a['id'],'quote':'Invented text with no basis.'}]}], 'relations':[]})

    def test_graph_aliases_merge_only_within_investigation(self):
        self.add_note('World Health Organization (WHO) tracks public health.', 'Health')
        p=self.k.index.passage_rows([self.p['id']])[0]
        for name,aliases in [('World Health Organization',['WHO']),('WHO',[])]:
            self.k.index.save_extraction(self.p['id'],[p],{'entities':[{'name':name,'kind':'organization','aliases':aliases,
                     'evidence':[{'passage_id':p['id'],'quote':p['text']}]}], 'relations':[]})
        self.assertEqual(self.k.index.status(self.p['id'])['entities'],1)

    def test_graph_endpoints_are_derived_without_dangling_names(self):
        self.add_note('Zephyr reduces latency in remote regions.', 'Zephyr')
        p=self.k.index.passage_rows([self.p['id']])[0]
        output=graph_from_relations({'relations':[{
            'source':{'name':'Zephyr','kind':'product','aliases':[]},
            'target':{'name':'latency','kind':'concept','aliases':[]},
            'relation':'reduces','confidence':'medium','evidence':['E1']}]},
            {'E1':{'passage_id':p['id'],'quote':p['text']}})
        self.k.index.save_extraction(self.p['id'],[p],output)
        self.assertEqual(self.k.index.status(self.p['id'])['entities'],2)
        self.assertEqual(self.k.index.status(self.p['id'])['relations'],1)
        self.assertEqual(self.k.index.status(self.p['id'])['graph_indexed'],1)

    def test_answer_uses_only_saved_evidence_and_exports_snapshot(self):
        self.add_note('Shade lowers surface heat beneath mature trees.', 'Shade')
        self.k.index_pending(self.p['id'])
        a=self.ask('How does shade affect heat?')
        self.assertEqual(a['status'],'complete')
        self.assertEqual(a['result']['status'],'answered')
        e=a['result']['claims'][0]['evidence'][0]
        p=next(p for p in a['retrieval']['passages'] if p['id']==e['passage_id'])
        self.assertIn(e['quote'],p['text'])
        folder=Path(self.tmp.name)/self.p['id']/'knowledge'
        self.assertTrue((folder/(a['id']+'.md')).exists())
        self.assertTrue((folder/(a['id']+'.json')).exists())
        self.assertFalse(self.store.get(self.p['id'])['running'])
        self.assertEqual(len(self.store.get(self.p['id'])['questions']),2)

    def test_invalid_model_citation_is_rejected(self):
        self.add_note('Shade lowers surface heat beneath mature trees.', 'Shade')
        self.providers.bad_quote=True
        a=self.ask('What does shade do?')
        self.assertEqual(a['status'],'error')
        self.assertIn('rejected',a['error'])
        self.assertIsNone(a['result'])

    def test_no_evidence_abstains_without_model_or_brave(self):
        a=self.ask('How do wormholes work?')
        self.assertEqual(a['result']['status'],'insufficient')
        self.assertEqual(self.providers.model_calls,0)
        self.assertEqual(self.providers.embed_calls,0)

    def test_model_can_abstain_with_retrieved_but_irrelevant_passages(self):
        self.add_note('Shade reduces local heat.', 'Shade')
        a=self.ask('What unsupported conclusion follows about shade?')
        self.assertEqual(a['result']['status'],'insufficient')
        self.assertEqual(a['result']['claims'],[])

    def test_embedding_failure_keeps_keyword_search_available(self):
        self.add_note('Trees provide cooling through shade.', 'Trees')
        self.providers.fail_embed=True
        self.k.index_pending(self.p['id'])
        self.assertTrue(self.k.index.status(self.p['id'])['errors'])
        a=self.ask('How do trees provide cooling?')
        self.assertEqual(a['status'],'complete')
        self.assertEqual(a['retrieval']['mode'],'keyword + graph')
        self.assertTrue(a['warnings'])
        self.providers.fail_embed=False
        self.k.index.retry(self.p['id'])
        self.k.index_pending(self.p['id'])
        self.assertEqual(self.k.index.status(self.p['id'])['errors'],[])

    def test_duplicate_ask_is_rejected_and_invalid_scope_fails(self):
        self.add_note('Trees reduce heat.', 'Trees')
        self.providers.entered=threading.Event()
        self.providers.release=threading.Event()
        self.k.ask(self.p['id'],'Trees?', [self.p['id']])
        self.assertTrue(self.providers.entered.wait(3))
        try:
            with self.assertRaises(ValueError): self.k.ask(self.p['id'],'Trees again?', [self.p['id']])
            with self.assertRaises(ValueError): self.k.ask(self.p['id'],'Trees?', [])
            with self.assertRaises(KeyError): self.k.ask(self.p['id'],'Trees?', ['000000000000'])
        finally:
            self.providers.release.set()

    def test_changed_note_removes_stale_passages_and_retains_answer_snapshot(self):
        q=self.add_note('Trees reduce heat.', 'Trees')
        answer=self.ask('What do trees do?')
        old=answer['retrieval']['passages'][0]['id']
        def edit(p):
            next(x for x in p['questions'] if x['id']==q['id'])['answer']['markdown']='Revised evidence about water demand.'
        p=self.store.mutate(self.p['id'],edit)
        self.k.index.sync_project(p)
        self.assertNotIn(old,[p['id'] for p in self.k.index.passage_rows([self.p['id']])])
        self.assertEqual(self.k.index.get_answer(answer['id'])['retrieval']['passages'][0]['text'],'Trees reduce heat.')

    def test_restart_recovers_inflight_questions(self):
        self.k.index.put_answer({'id':'abc123','project_id':self.p['id'],'status':'answering'})
        index=KnowledgeIndex(Path(self.tmp.name),self.providers.config.embedding_model)
        self.assertEqual(index.get_answer('abc123')['status'],'error')
        index.db.close()

    def test_store_changes_schedule_background_indexing(self):
        self.add_note('New evidence about trees.', 'New note')
        self.assertIn(self.p['id'], self.k.pending)


if __name__ == '__main__':
    unittest.main()
