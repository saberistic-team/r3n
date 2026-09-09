import io
import json
import mimetypes
import re
import secrets
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .config import Config, ROOT
from .engine import Engine
from .knowledge import Knowledge
from .providers import Providers
from .store import Store, graph_for, normalized, question


def settings(data):
    result = {'questions_per_cycle': data.get('questions_per_cycle', 3), 'answer_budget': data.get('answer_budget', 0)}
    for key, low, high in [('questions_per_cycle', 1, 10), ('answer_budget', 0, 10000)]:
        value = result[key]
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f'{key} must be an integer between {low} and {high}.')
    return result


def input_text(data, key='question'):
    value = data.get(key)
    if not isinstance(value, str) or not 3 <= len(value.strip()) <= 1000:
        raise ValueError('Enter a question between 3 and 1,000 characters.')
    return value.strip()


def make_server(config, store=None, providers=None, knowledge_autostart=True):
    store = store or Store(config.data_dir)
    providers = providers or Providers(config)
    engine = Engine(store, providers)
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def respond(self, body, status=200, kind='application/json; charset=utf-8', filename=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            if filename:
                self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def guard(self, write=False):
            # Loopback binding + Host/Origin checks protect the key and billable APIs
            # from arbitrary websites, DNS rebinding, and cross-origin form posts.
            host = self.headers.get('Host', '')
            if host not in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}:
                self.respond({'error': 'Unrecognized host.'}, 403)
                return False
            origin = self.headers.get('Origin')
            if origin and origin != f'http://{host}':
                self.respond({'error': 'Cross-origin requests are not allowed.'}, 403)
                return False
            if write and not secrets.compare_digest(self.headers.get('X-R3N-Token', ''), token):
                self.respond({'error': 'Reload this page to refresh your session.'}, 403)
                return False
            return True

        def do_GET(self):
            if not self.guard():
                return
            try:
                path = urlparse(self.path).path
                if path == '/api/session':
                    return self.respond({'token': token})
                if path == '/api/health':
                    return self.respond(providers.health())
                if path == '/api/projects':
                    projects = sorted(store.list(), key=lambda p: p['created_at'], reverse=True)
                    return self.respond([{'id': p['id'], 'question': p['question'], 'phase': p['phase'],
                                          'running': p['running'], 'created_at': p['created_at'],
                                          'answers': sum(bool(q.get('answer')) for q in p['questions'])} for p in projects])
                knowledge_match = re.fullmatch(r'/api/projects/([a-f0-9]{12})/knowledge', path)
                if knowledge_match:
                    store.get(knowledge_match[1])
                    return self.respond({'index': knowledge.index.status(knowledge_match[1]),
                                         'answers': knowledge.index.history(knowledge_match[1])})
                answer_match = re.fullmatch(r'/api/knowledge/answers/([a-f0-9]{12})', path)
                if answer_match:
                    return self.respond(knowledge.index.get_answer(answer_match[1]))
                match = re.fullmatch(r'/api/projects/([a-f0-9]{12})(?:/(graph|export))?', path)
                if match:
                    p = store.get(match[1])
                    if match[2] == 'graph':
                        return self.respond(graph_for(p))
                    if match[2] == 'export':
                        knowledge.index.export_graph(p['id'])
                        buffer = io.BytesIO()
                        with store.lock:
                            store.export(store.get(p['id']))
                            folder = store.directory / p['id']
                            with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
                                for file in sorted(folder.rglob('*')):
                                    if file.is_file() and file.suffix != '.tmp':
                                        archive.write(file, str(file.relative_to(folder)))
                        return self.respond(buffer.getvalue(), kind='application/zip', filename=f'r3n-{p["id"]}.zip')
                    return self.respond({**p, 'graph': graph_for(p)})
                static = {'/': 'index.html', '/app.js': 'app.js', '/knowledge.js': 'knowledge.js', '/style.css': 'style.css'}
                if path in static:
                    file = ROOT / 'web' / static[path]
                    return self.respond(file.read_bytes(), kind=mimetypes.guess_type(file)[0] + '; charset=utf-8')
                self.respond({'error': 'Not found'}, 404)
            except KeyError:
                self.respond({'error': 'Investigation not found'}, 404)

        def do_POST(self):
            if not self.guard(write=True):
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 20000:
                    raise ValueError('Invalid request size.')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('Expected a JSON object.')
                path = urlparse(self.path).path
                if path == '/api/projects':
                    return self.respond(store.create(input_text(data), settings(data)), 201)
                match = re.fullmatch(r'/api/projects/([a-f0-9]{12})/(start|pause|questions|settings|skip|ask|reindex)', path)
                if not match:
                    return self.respond({'error': 'Not found'}, 404)
                id_, action = match.groups()
                if action == 'ask':
                    return self.respond(knowledge.ask(id_, input_text(data), data.get('scope', [id_])), 202)
                elif action == 'reindex':
                    store.get(id_)
                    knowledge.index.retry(id_)
                    knowledge.notify(id_)
                    return self.respond({'scheduled': True})
                elif action == 'start':
                    engine.start(id_)
                elif action == 'pause':
                    engine.pause(id_)
                elif action == 'questions':
                    text = input_text(data)
                    def add(p):
                        if normalized(text) in {normalized(q['text']) for q in p['questions'] if q['status'] != 'skipped'}:
                            raise ValueError('This question is already in the investigation.')
                        p['questions'].append(question(text))
                        Engine.event(p, 'Researcher added a question: ' + text)
                    store.mutate(id_, add)
                elif action == 'settings':
                    def change(p):
                        if p['running']:
                            raise ValueError('Pause before changing cycle settings.')
                        p['settings'] = settings(data)
                    store.mutate(id_, change)
                elif action == 'skip':
                    def skip(p):
                        if p['running']:
                            raise ValueError('Pause before skipping questions.')
                        q = next((q for q in p['questions'] if q['id'] == data.get('id')), None)
                        if not q or q['status'] != 'pending':
                            raise ValueError('Only pending questions can be skipped.')
                        q['status'] = 'skipped'
                    store.mutate(id_, skip)
                return self.respond(store.get(id_))
            except KeyError:
                self.respond({'error': 'Investigation not found'}, 404)
            except (ValueError, TypeError) as exc:
                self.respond({'error': str(exc)}, 400)
            except Exception:
                import traceback
                traceback.print_exc()
                self.respond({'error': 'The request could not be saved. Check the terminal.'}, 500)

    class Server(ThreadingHTTPServer):
        def server_close(self):
            if hasattr(self, 'knowledge'):
                self.knowledge.close()
            super().server_close()

    server = Server(('127.0.0.1', config.port), Handler)
    knowledge = Knowledge(store, providers, autostart=knowledge_autostart)
    server.knowledge = knowledge
    server.engine = engine
    return server


def main():
    config = Config()
    server = make_server(config)
    print(f'R3N is ready at http://localhost:{server.server_port}', flush=True)
    print(f'Research folder: {config.data_dir}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        for p in server.engine.store.list():
            if p['running']:
                server.engine.pause(p['id'])
        print('\nStopping. Interrupted investigations can be resumed on the next launch.', flush=True)
    finally:
        server.server_close()
