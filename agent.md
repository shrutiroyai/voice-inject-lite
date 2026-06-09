# Project Knowledge Graph: Voice Inject Lite

This file serves as the definitive context map for AI agents. It outlines the architectural intent, file relationships, and preserved learnings of the project.

## 1. System Topology (The Knowledge Graph)

```mermaid
graph TD
    %% Nodes
    C[client.py] -- "Core Engine"
    S[server.py] -- "UI / Config Server"
    PS[platform_support.py] -- "OS Abstraction"
    DB[speaker_db.py] -- "Embedding Storage"
    V[vocabulary.json] -- "Phonetic Hints"
    SN[snippets.json] -- "Text Expansion"
    Web[Web UI] -- "Dashboard"

    %% Edges (Relationships)
    C -->|Imports| PS
    C <-->|WebSocket| S
    S -->|Uses| DB
    C -->|Reads| V
    C -->|Reads| SN
    Web <-->|HTTP/WS| S
```

---

## 2. Intent Registry (The "Why")

| File | Primary Intent | Architectural Rationale |
| :--- | :--- | :--- |
| `client.py` | Inference Worker | Handles the heavyweight MLX threads (Whisper & LLM). Uses a Queue pattern to prevent UI/Audio blocking. |
| `server.py` | State Orchestrator | Provides the WebSocket bridge and Web UI. Decouples the configuration from the active worker thread. |
| `platform_support.py` | Hardware Layer | Isolates brittle OS calls (AppleScript, Win32 API, X11). Keeps the core logic platform-agnostic. |
| `speaker_db.py` | Persistent Memory | Specialized for storing and comparing 512-dim speaker embeddings using Cosine Similarity. |
| `install.sh` | Environment Bootstrapper | Manages the Python 3.14 virtual environment and platform-specific dependencies (mlx, transformers). |

---

## 3. Preserved Learnings (The "Agentic Soul")

This section records critical "gotchas" discovered during development to prevent future regressions.

### **A. MLX Generation (Darwin)**
- **Learning**: As of `mlx-lm` version 0.31.0+, the `generate` function no longer accepts a `temp` argument directly.
- **Fix**: You must import `make_sampler` from `mlx_lm.sample_utils` and pass it as `sampler=make_sampler(temp=0.2)`.
- **Impact**: Incorrect parameters will crash the `mlx_worker` thread silently if not caught.

### **B. AppleScript Volume Control**
- **Learning**: The command `set volume settings alert volume 0` is inconsistent and often fails with "variable settings is not defined."
- **Fix**: Use the simpler `set volume alert volume 0` and always wrap in a `try...finally` block in Python to ensure system settings are restored even on crash.

### **C. Prompt Engineering for Latency**
- **Learning**: LLMs often expand short corrections into formal letters, increasing generation time and perceived lag.
- **Strategy**: Use the "Precision Tool" prompt. Specifically, the instruction `- MATCH THE LENGTH of the original content` is critical for maintaining high responsiveness in Selection Mode.

### **D. Snippet Post-Processing**
- **Learning**: Re-compiling Regex for every snippet on every transcription is a CPU bottleneck.
- **Fix**: Implemented `get_cached_snippets()` with a 30s TTL to cache compiled patterns in memory.

---

## 4. Operational Conventions
- **Temperature**: Default to `0.2`. It provides the best balance between human tone and correction precision.
- **Confidence**: `_MIN_CONTEXT_CONFIDENCE` is set to `-0.4`. Lower values risk polluting the rolling history with hallucinations.
- **Max Tokens**: Selection mode is capped at `500` to prevent runaway generation.

---
*Last Updated: June 9, 2026*
