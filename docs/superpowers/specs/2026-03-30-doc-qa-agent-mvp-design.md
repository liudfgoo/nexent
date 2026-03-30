# Document Q&A Agent MVP — Design Specification

**Date:** 2026-03-30
**Status:** Approved

## 1. Overview

A standalone Python script that demonstrates the Nexent SDK's agent capabilities by building a **document-based Q&A assistant**. The agent answers user questions by retrieving relevant chunks from local documents and synthesizing answers using a chat model.

**Goal:** Show how `OpenAIModel` (chat) and `OpenAICompatibleEmbedding` (embedding) are used together in a RAG pipeline — no Docker, no Elasticsearch, runs on Windows.

---

## 2. SDK Components Used

| Purpose | SDK Component | Role |
|---|---|---|
| Chunking | `DataProcessCore` + `UnstructuredProcessor` | Reads `.txt/.md/.pdf` → splits into text chunks |
| Embedding | `OpenAICompatibleEmbedding` | Converts text → vector (same instance for indexing & query) |
| Vector Store | **In-memory numpy** (custom, no Docker) | Cosine similarity search, no external process needed |
| Chat Model | `OpenAIModel` (smolagents) | ReAct reasoning and answer generation |
| Agent | `NexentAgent` + `CoreAgent` | Tool-augmented ReAct loop |
| Streaming | `MessageObserver` (existing) | Streams tokens and process events to console |
| Tool | `KnowledgeBaseSearchTool` | Wraps embedding + vector store search into a smolagents Tool |

---

## 3. Data Flow

### Startup — Indexing Phase
```
Local .txt/.md files in ./docs/
     ↓
DataProcessCore.file_process()   [SDK, chunking with UnstructuredProcessor]
     ↓  List[Dict{content, filename, ...}]
OpenAICompatibleEmbedding.get_embeddings()  [SDK, batch embed]
     ↓  List[List[float]] vectors
InMemoryVectorStore.add_chunks()   [numpy, cosine similarity]
```

### Query — Retrieval-Augmented Generation
```
User query string
     ↓
NexentAgent (CoreAgent + OpenAIModel)
     ├→ decides to call KnowledgeBaseSearchTool
     ↓
KnowledgeBaseSearchTool.forward(query)
     ├→ OpenAICompatibleEmbedding (query) → query_vector  [same instance]
     ├→ InMemoryVectorStore.semantic_search() → top-K chunks
     ↓  JSON results with content, score, source
NexentAgent synthesizes answer
     ↓
MessageObserver streams to console
```

---

## 4. Architecture

### 4.1 Components

**`InMemoryVectorStore`**
- Stores chunks as dicts and vectors as numpy arrays
- `add_chunks(chunks, embedding_model)` — batch embed and store
- `semantic_search(query_vector, top_k)` — cosine similarity, returns top-K
- `hybrid_search(query_text, embedding_model, top_k)` — keyword + semantic blend

**`DocumentLoader`**
- Reads files from `DOCS_DIR`
- Calls `DataProcessCore.file_process()` for each file
- Returns list of chunk dicts

**`KnowledgeBaseSearchTool`** — already in SDK, reused as-is
- Accepts `embedding_model` + `vdb_core` + `index_names` at init
- `forward(query)` → performs hybrid/semantic/accurate search, returns JSON

**`doc_qa_agent.py`** (main script)
1. Load and index documents
2. Build `NexentAgent` with `KnowledgeBaseSearchTool`
3. Interactive REPL loop: user types question → agent answers → stream to console

### 4.2 Configuration (top of script)

```python
# ── Chat Model (OpenAI-compatible) ──
CHAT_MODEL_CONFIG = ModelConfig(
    cite_name="openai",
    model_name="gpt-4o-mini",
    api_key=os.getenv("OPENAI_API_KEY"),
    url="https://api.openai.com/v1",
    temperature=0.1,
)

# ── Embedding Model (OpenAI-compatible) ──
EMBEDDING_CONFIG = {
    "model_name": "text-embedding-3-small",
    "base_url": "https://api.openai.com/v1",
    "api_key": os.getenv("OPENAI_API_KEY"),
    "embedding_dim": 1536,
}

# ── Documents ──
DOCS_DIR = "./docs"
INDEX_NAME = "doc_qa_index"
```

---

## 5. File Layout

```
sdk/examples/doc_qa_agent/
├── doc_qa_agent.py       # Single script — all logic
└── README.md             # How to run (pip install, env vars, sample docs)
```

**No new directories in `sdk/nexent/`** — example code lives under `sdk/examples/`.

---

## 6. Error Handling

- **No API key:** Exit with clear message pointing to env var
- **Embedding API failure:** Log error, skip failed chunks, continue
- **Empty index:** Agent gets "no results found" from tool, responds accordingly
- **File read error:** Skip file, log warning, continue with other files

---

## 7. Dependencies (additional beyond SDK)

- `numpy` — for in-memory vector store (already likely installed)
- `scikit-learn` or pure numpy cosine — for similarity scoring
- `unstructured` — SDK already depends on it for `DataProcessCore`

No new heavy dependencies. All other requirements are already in SDK's `pyproject.toml`.

---

## 8. Out of Scope

- Authentication / multi-tenancy
- Persistence (vector store resets on restart)
- Web UI / HTTP API
- Multiple indices or embedding models
- Production-grade chunking strategies (beyond SDK's built-in)
- Docker / Elasticsearch

---

## 9. Acceptance Criteria

1. Script runs with `python doc_qa_agent.py` after `pip install -e sdk/`
2. Documents in `./docs/` are chunked using `DataProcessCore`
3. Same `OpenAICompatibleEmbedding` instance is used for both indexing and query embedding
4. Agent correctly retrieves relevant chunks and synthesizes an answer
5. Answer is streamed to console via `MessageObserver`
6. No Docker or Elasticsearch required — works on Windows without any external services
7. Clear setup instructions in `README.md`
