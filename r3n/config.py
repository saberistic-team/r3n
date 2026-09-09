import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=ROOT / '.env'):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip('\"\''))


class Config:
    def __init__(self):
        load_env()
        self.data_dir = Path(os.getenv('R3N_DATA_DIR', str(ROOT / 'data'))).expanduser().resolve()
        self.brave_key = os.getenv('BRAVE_SEARCH_API_KEY', '')
        self.brave_url = 'https://api.search.brave.com/res/v1/chat/completions'
        self.ollama_url = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
        self.model = os.getenv('OLLAMA_MODEL', 'qwen3.8:27b-mlx')
        self.embedding_model = os.getenv('OLLAMA_EMBEDDING_MODEL', 'qwen3-embedding:0.6b')
        self.port = int(os.getenv('PORT', '4317'))
