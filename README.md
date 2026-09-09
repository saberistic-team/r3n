# R3N — follow the question

A local research notebook that continuously cycles through **planning → research → briefing** until you press **Pause**. Brave Answers researches the web; your local `qwen3.8:27b-mlx` model generates follow-up questions, synthesizes findings, and extracts evidence-linked concept relationships.

The **Ask your research** tab answers questions from collected research with local semantic search, keyword search, and an evidence graph. No dedicated graph database is required.

## Start

Requires Python 3.11+ and Ollama. There are no Python packages to install and no frontend build step.

```sh
cp .env.example .env
# Set BRAVE_SEARCH_API_KEY in .env.
python3 -m r3n
```

Open [localhost:4317](http://localhost:4317). Keep the app process and Ollama running. The web page can be closed while research continues.

Your Brave key needs access to the **Answers plan**. Ollama must expose the exact model tag configured in `.env`; the default is `qwen3.8:27b-mlx`. R3N does not silently change models, download model weights, or use a cloud model fallback. Change `OLLAMA_MODEL` if your model has a different tag. This app uses Ollama’s HTTP interface; the model tag’s `-mlx` suffix does not configure its runtime backend.

For semantic knowledge search, make `qwen3-embedding:0.6b` available in Ollama, or set `OLLAMA_EMBEDDING_MODEL` to an installed embedding model. The UI reports indexing failures and can retry them. Keyword and available graph retrieval remain usable if embeddings are unavailable; answer generation still requires Qwen. Changing the configured embedding model re-embeds existing passages at startup.

## Ask your collected research

Open an investigation and select **Ask your research**. Enter a self-contained question and choose the investigations to search in the scope menu. Asking a knowledge question uses local Ollama only and works while research is paused. It never invokes Brave or automatically starts research.

- **Answers cite exact saved excerpts.** Click a passage button to see the original text with the cited excerpt highlighted, then open its complete note or the source links attached to that note.
- **Evidence map** shows the concept connections used during retrieval and the supporting passages. Graph relationships are interpretations tied to evidence, not independent sources.
- **Research this gap** explicitly adds a follow-up to the existing research queue. If research is paused, it remains paused until you press Start.
- **History** persists the last 40 answers in the UI; all answers remain in the database. Q&A is a collection of independent questions, not a conversational-memory prompt. Restate context in follow-up questions.
- **Exports** include `knowledge/<answer-id>.md` and `.json` for complete answers. JSON stores the exact retrieved passage snapshots, selected investigation scope, retrieval methods, graph connections, and answer limitations. Existing citations survive later reindexing.
- ZIP exports also include `knowledge/graph.json` with passage-level entities, aliases, relationships, original evidence quotes, and indexing coverage. The original `graph.json` remains the cycle-briefing graph.

Existing notes are indexed at startup. Newly saved answers are indexed incrementally in the background. The tab shows how many passages have embeddings and graph extraction, plus any errors. **Retry indexing** retries failed operations; complete passages are reused. Interactive questions take priority after the current model call finishes. Research inference, embedding, indexing, and Q&A share a serialized local model scheduler.

## Knowledge retrieval architecture

`knowledge.sqlite3` sits beside `research.sqlite3` and contains normalized notes, passages, FTS5 search, vectors, entities, aliases, mentions, relationships, relationship evidence, embedding cache, and Q&A history. The original research store and Markdown notes remain authoritative. No cloud vector service or graph database is needed.

1. Split each answer into overlapping passages of roughly 1,400 characters, preserving exact offsets into the original Markdown.
2. Embed passages with local Ollama `/api/embed`, caching by text and embedding model. Store normalized vectors in SQLite.
3. Qwen extracts entities and relationships from small batches. It selects immutable excerpt IDs; the app supplies their exact original quotes. Unknown IDs, undeclared relation endpoints, and unsupported quotation locations are rejected. Explicit aliases are merged only within the same investigation and entity type; contradictory relationship labels remain separate.
4. At question time, combine FTS5/BM25 keyword results with cosine-similarity results using reciprocal-rank fusion. Find entity entry points, expand at most two graph hops and 40 evidence connections, then select up to 12 diverse passages within an 18,000-character budget. All routes filter to the selected investigations.
5. Qwen generates supported claims and selects excerpt IDs. The server validates all citations and attaches exact quoted text. Empty retrieval returns an insufficient-evidence response without a model call; Qwen can also abstain or provide a partial answer when retrieved material is insufficient.

This implements local graph-assisted RAG. Community detection and whole-corpus map/reduce summaries, original webpage ingestion, automatic fact verification, and embedding-based entity merging are not included. Source links identify URLs attached to a Brave answer; they do not establish which original page supports every sentence. The citation validator checks evidence identity and exact text, not logical entailment or real-world truth. Dates represent collection time unless an underlying note states otherwise.

The first implementation loads selected passage vectors into memory and computes exact similarity in Python. This suits local notebooks; large corpora should use an indexed vector extension behind the same retrieval interface. Use a stable embedding model tag: replacing weights under the same tag requires an explicit index rebuild. To rebuild safely, stop the app, back up the whole data folder, and move `knowledge.sqlite3` and its WAL/SHM companions to a backup folder before restarting. This also resets the database-backed Q&A history; Markdown/JSON exports remain on disk. Do not rebuild a live database.

## Using the notebook

1. Enter a starting question and choose **Begin investigation**. It is saved immediately, and research starts if a Brave key is configured.
2. Qwen plans a batch of questions. The starting question and researcher-submitted questions take priority.
3. Brave Answers researches each question. Each complete answer is saved immediately as Markdown, with citations and its original provider response.
4. Qwen writes a cumulative briefing, identifies gaps and contradictions, and extracts concept relationships referencing saved note IDs.
5. The next plan receives the previous briefing, open questions, and recent evidence. This loop runs continuously.
6. **Pause** stops scheduling new work. The current provider call is allowed to finish so its result can be saved. A local Qwen call can take several minutes. **Start** resumes the saved stage; answered questions are not researched again.
7. Add questions in the right-hand queue at any time. Questions added during planning are prioritized before AI proposals; questions added during research enter the next cycle. While paused, pending questions can be skipped.

The default is **3 questions per cycle with no answer limit**. The preferences button lets you change batch size and set an optional total answer budget while paused. A budget of zero means unlimited. Brave calls consume your API credits. The budget counts successfully saved answers, not dollars or failed calls; a disconnected or failed request may still have been billed. There are no automatic provider retries. Provider failures pause the loop with an actionable error; Start retries the current stage. If Qwen returns only duplicate or empty questions, research stops instead of issuing repeated searches.

## Your files

Data is stored in `data/` by default. Set `R3N_DATA_DIR` to use another folder.

```text
data/
  research.sqlite3                 # Durable local state; SQLite WAL
  knowledge.sqlite3                # Passage/vector/graph index and Q&A history
  <investigation-id>/
    README.md                      # Linked notebook index
    investigation.json             # Full state snapshot
    plans/cycle-001.md
    notes/<question-id>.md         # Brave answer + sources
    raw/<question-id>.json         # Raw response, citations, usage metadata
    briefings/cycle-001.md
    graph.json                     # Nodes and evidence-linked edges
    graph.mmd                      # Mermaid graph
    knowledge/<answer-id>.md       # Grounded local answer with exact quotes
    knowledge/<answer-id>.json     # Evidence snapshot and retrieval trace
```

**Export** downloads this investigation as a ZIP. The SQLite database is authoritative; Markdown and JSON exports are regenerated on updates and startup. Edit research through the app; manual edits to generated files will be overwritten. Back up the entire data folder with the app stopped.

The graph connects investigations, questions, cited sources, and extracted concepts. Concept relationships carry evidence note IDs and Qwen-assigned confidence. These are interpretations of Brave’s answers, not independently verified facts. A briefing with unknown evidence IDs is rejected and can be retried without repeating completed searches. Labels are normalized for exact deduplication; there is no embedding-based entity resolution. The browser displays up to 120 nodes for readability; exports contain the complete graph.

## Architecture and limits

- Python standard-library HTTP server, SQLite, background worker per investigation; loopback only.
- Local Ollama `/api/chat` with JSON Schema structured output and `think: false` for the installed model’s JSON compatibility. Model calls are serialized to avoid overlapping local inference.
- Brave `/res/v1/chat/completions`, one user message, streaming single-search mode, rich citations enabled. R3N orchestrates its own research loop.
- Partial Brave streams are not committed as completed answers. Citation tags split across SSE chunks are reassembled; usage metadata and raw output are preserved.
- Every stage reloads state before writing, so researcher questions survive concurrent model calls. Start is reserved synchronously to prevent duplicate runs.
- Restarted processes leave interrupted investigations paused; press Start to resume. An interrupted remote request may have already consumed credits.
- Prompts use a bounded rolling briefing, recent answer excerpts, and recent question index. Full older notes remain on disk but are not all placed into every prompt. Exact text deduplication checks the full question history.
- The app is intended for a single local researcher, one server process, and moderate investigation sizes. It rewrites exports on state changes; very large notebooks will need incremental storage and graph rendering.
- Pause is cooperative, not an immediate cancellation of inference or a remote API request. Provider socket timeouts are 3 minutes for Brave and 10 minutes for Qwen.

Only the current question and root question are sent to Brave. Research notes and briefings are sent to the configured Ollama server. API keys stay on the server and are never returned to the browser. The server rejects cross-origin writes and unknown Host headers, requires a per-process session token for writes, and renders provider content without executing HTML. This is a local application, not a public hosting server; do not expose it through a public tunnel.

## Verify

```sh
python3 -m unittest discover -s tests -v
node --check web/app.js
node --check web/knowledge.js
```

Tests use deterministic fixtures and never consume Brave credits. They cover multiple research cycles, context feedback, pause/resume, human question priority, duplicate Start requests, synthesis failure recovery, restart recovery, budget enforcement, source parsing, and evidence validation.

Knowledge tests additionally cover passage offsets, existing-data migration, incremental indexing, embedding reuse and failure fallback, scope isolation, semantic retrieval without shared keywords, graph expansion, alias handling, citation rejection, insufficient-evidence answers, export snapshots, concurrent requests, and the HTTP Q&A flow.

Provider references: [Brave Answers API](https://api-dashboard.search.brave.com/api-reference/ai/answers), [Brave citation response format](https://api-dashboard.search.brave.com/app/documentation/ai-grounding/query), and [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs).
