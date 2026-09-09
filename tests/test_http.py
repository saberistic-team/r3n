import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from types import SimpleNamespace

from r3n.server import make_server
from r3n.store import Store
from tests.test_research import FakeProviders
from tests.test_knowledge import KnowledgeProviders
from r3n.store import now


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.store = Store(Path(cls.tmp.name))
        cls.providers = FakeProviders()
        cls.providers.health = lambda: {'brave_configured': True, 'model_available': True}
        try:
            cls.server = make_server(SimpleNamespace(port=0), cls.store, cls.providers, knowledge_autostart=False)
        except PermissionError:
            cls.store.db.close()
            cls.tmp.cleanup()
            raise unittest.SkipTest('Loopback sockets are blocked in this sandbox; run tests with local networking enabled.')
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'
        with urllib.request.urlopen(cls.url + '/api/session') as response:
            cls.token = json.load(response)['token']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(2)
        cls.store.db.close()
        cls.tmp.cleanup()

    def send(self, path, data, headers=None):
        request = urllib.request.Request(self.url + path, data=json.dumps(data).encode(),
                                         headers={'Content-Type': 'application/json', 'X-R3N-Token': self.token, **(headers or {})})
        return urllib.request.urlopen(request)

    def test_browser_api_cycle_and_zip(self):
        with self.send('/api/projects', {'question': 'How do trees cool streets?', 'answer_budget': 2, 'questions_per_cycle': 2}) as response:
            p = json.load(response)
        base = '/api/projects/' + p['id']
        with self.send(base + '/start', {}):
            pass
        self.server.engine.threads[p['id']].join(5)
        with urllib.request.urlopen(self.url + base) as response:
            p = json.load(response)
        self.assertEqual(p['phase'], 'complete')
        self.assertEqual(len(p['graph']['nodes']), 6)
        with self.send(base + '/questions', {'question': 'How do species compare?'}) as response:
            p = json.load(response)
        self.assertEqual(p['questions'][-1]['origin'], 'researcher')
        with urllib.request.urlopen(self.url + base + '/export') as response:
            content = response.read()
            self.assertEqual(response.headers['Content-Type'], 'application/zip')
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            names = archive.namelist()
            self.assertIn('graph.json', names)
            self.assertIn('briefings/cycle-001.md', names)
            self.assertEqual(len([name for name in names if name.startswith('notes/')]), 2)

    def test_cross_origin_and_missing_token_rejected(self):
        for headers in [{'Origin': 'https://untrusted.example'}, {'X-R3N-Token': ''}, {'Host': 'untrusted.example'}]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.send('/api/projects', {'question': 'A question'}, headers)
            self.assertEqual(caught.exception.code, 403)
            caught.exception.close()

    def test_invalid_inputs_and_missing_project(self):
        for payload in [[], {'question': ''}, {'question':'Valid question', 'answer_budget': -10}]:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.send('/api/projects', payload)
            self.assertEqual(caught.exception.code, 400)
            caught.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.url + '/api/projects/000000000000')
        self.assertEqual(caught.exception.code, 404)
        caught.exception.close()

    def test_knowledge_question_api_works_without_brave(self):
        with self.send('/api/projects', {'question':'How do trees affect heat?'}) as response:
            project=json.load(response)
        id_=project['id']
        answer=FakeProviders().answer('trees','heat')
        answer['fetched_at']=now()
        def save(p): p['questions'][0].update(answer=answer,status='answered',cycle=1)
        self.store.mutate(id_,save)
        k=self.server.knowledge
        original=k.providers
        k.providers=KnowledgeProviders()
        try:
            k.index.sync_project(self.store.get(id_))
            k.index_pending(id_)
            with self.send(f'/api/projects/{id_}/ask',{'question':'How do trees provide shade?', 'scope':[id_]}) as response:
                self.assertEqual(response.status,202)
                job=json.load(response)
            k.active_answers[id_].join(5)
            with urllib.request.urlopen(self.url+'/api/knowledge/answers/'+job['id']) as response:
                job=json.load(response)
            self.assertEqual(job['status'],'complete')
            self.assertTrue(job['result']['claims'][0]['evidence'])
            with urllib.request.urlopen(self.url+f'/api/projects/{id_}/knowledge') as response:
                knowledge=json.load(response)
            self.assertEqual(knowledge['index']['notes'],1)
            self.assertEqual(knowledge['answers'][0]['id'],job['id'])
            with urllib.request.urlopen(self.url+f'/api/projects/{id_}/export') as response:
                with zipfile.ZipFile(io.BytesIO(response.read())) as archive:
                    self.assertIn('knowledge/'+job['id']+'.md',archive.namelist())
            self.assertFalse(self.store.get(id_)['running'])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.send(f'/api/projects/{id_}/ask',{'question':'Anything?', 'scope':[]})
            self.assertEqual(caught.exception.code,400)
            caught.exception.close()
        finally:
            k.providers=original


if __name__ == '__main__':
    unittest.main()
