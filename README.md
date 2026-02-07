# 🧠 Code Intelligence

A local-first code intelligence system that turns your codebase into a searchable, queryable knowledge base — accessible from your terminal or WhatsApp via [OpenClaw]([https://github.com/nichochar/openclaw](https://github.com/openclaw/openclaw?tab=MIT-1-ov-file)).

Built with tree-sitter AST parsing, SQLite indexing, and natural language query support.

## What it does

Point it at any directory. In under a second, it parses every Python file into an AST, extracts every symbol (class, function, method, import, call), and stores them in a fast SQLite index. Then query it with natural language.

```bash
# Index your codebase
python scripts/indexer.py ~/Dev --verbose
# Indexed 173 files in 0.67s — 1545 symbols, 1170 imports, 12321 references

# Ask questions
python scripts/search.py "who calls forward"
python scripts/search.py "describe RealNVPFlow"
python scripts/search.py "overview"
python scripts/search.py "unused"
python scripts/search.py "deps phi4_experiments"
```

Or from WhatsApp (via OpenClaw):

```
> who calls forward
📞 forward (in sample_and_logq)
   📍 SymmetricMC/RBflows/phi4_experiments.py:271
   └ phi, logdet = self.forward(z)

> describe RealNVPFlow
📦 RealNVPFlow — class
📍 SymmetricMC/RBflows/u1_flows_experiments.py:142
🔧 Methods: __init__, forward, inverse, log_q, sample_and_logq
```

## Features

### Phase 1: Smart Index
- Tree-sitter AST parsing for Python (Julia planned)
- SQLite database with symbols, imports, and references tables
- Incremental-ready (tracks file mtimes)
- Sub-second indexing for ~200 files

### Phase 2: Natural Language Queries
| Query | What it does |
|---|---|
| `"class Flow"` | Find classes matching "Flow" |
| `"function train"` | Find functions matching "train" |
| `"who calls forward"` | Find all call sites of `forward()` |
| `"describe RealNVPFlow"` | Show class definition, docstring, all methods |
| `"deps phi4_experiments"` | List all imports in a file |
| `"overview"` | Codebase stats: files, symbols, top dirs, most-used imports |
| `"unused"` | Detect potentially dead functions (defined but never called) |
| `"import torch"` | Find all torch imports across codebase |

### Phase 3: Background Job System
Run long tasks from WhatsApp, get notified when done.

```bash
python scripts/jobs.py submit "python train.py --epochs 100" -l "training run"
# ⚡ Job #1 submitted (PID 12345)

python scripts/jobs.py status 1
# ✅ Job #1 — done

python scripts/jobs.py result 1
# Shows full output

python scripts/jobs.py notify
# Job #1 finished.
# Training complete, loss: 0.0023
```

### Phase 4: Safety & Scope
- **Path allowlisting** — only index/search/run in approved directories
- **Command blocking** — regex patterns block `rm -rf`, `curl | sh`, `sudo`, etc.
- **Audit log** — every action timestamped and logged to SQLite

```bash
python scripts/guard.py check-cmd "rm -rf /"
# 🚫 Blocked command: contains 'rm -rf /'

python scripts/guard.py audit
# ✅ [2026-02-07T14:48:47] check-cmd: python train.py
# 🚫 [2026-02-07T14:48:47] check-cmd: rm -rf /
```

## Setup

### Requirements
- Python 3.11+ (tested on 3.14)
- tree-sitter, tree-sitter-python

### Install

```bash
git clone https://github.com/YOUR_USERNAME/code-intel.git
cd code-intel
pip install tree-sitter tree-sitter-python
```

### Index your code

```bash
python scripts/indexer.py ~/your/code/directory --verbose
```

### Search

```bash
python scripts/search.py "your query here" -v
```

### OpenClaw Integration

Symlink into OpenClaw's skills directory:

```bash
ln -s /path/to/code-intel ~/.openclaw/skills/code-intel
```

Then query from WhatsApp: `find class MyModel`, `who calls train`, `overview`, etc.

## Project Structure

```
code-intel/
├── scripts/
│   ├── indexer.py     # AST parser + SQLite indexer
│   ├── search.py      # NL query engine
│   ├── jobs.py        # Background job system
│   ├── guard.py       # Safety guards + audit
│   └── db/
│       └── code.db    # Symbol index (auto-generated)
├── jobs/              # Job artifacts (auto-generated)
├── safety.json        # Safety config (auto-generated)
├── SKILL.md           # OpenClaw skill definition
└── README.md
```

## Roadmap

### v2.1 — Language Support
- [ ] Julia tree-sitter parsing (for lattice QFT codebases)
- [ ] JavaScript/TypeScript support
- [ ] C/C++ support (header analysis)
- [ ] Rust support

### v2.2 — Dependency Graph
- [ ] Full call graph construction (who calls whom, N levels deep)
- [ ] `graph RealNVPFlow` → ASCII or Mermaid dependency tree
- [ ] Import dependency graph between files/modules
- [ ] Circular dependency detection

### v2.3 — Code Quality
- [ ] Complexity scoring (cyclomatic complexity per function)
- [ ] Duplicate/near-duplicate function detection
- [ ] Convention checker (naming, docstring coverage)
- [ ] `health` command — overall codebase health report

### v2.4 — Semantic Search
- [ ] Embed symbols using a small LM (e.g., CodeBERT)
- [ ] Vector similarity search ("find functions that do matrix multiplication")
- [ ] Semantic clustering of related functions
- [ ] "Explain this file" using LLM + indexed context

### v2.5 — Live Watching
- [ ] `fswatch`-based auto re-indexing on file changes
- [ ] Diff-aware incremental indexing (only re-parse changed files)
- [ ] WhatsApp notifications on index changes ("3 new functions added")

### v2.6 — Multi-Repo
- [ ] Index multiple repos with namespace isolation
- [ ] Cross-repo reference tracking
- [ ] `switch repo SymmetricMC` context switching
- [ ] Repo comparison ("what symbols exist in A but not B")

### v2.7 — Git Integration
- [ ] `blame RealNVPFlow` → who wrote it, when, which commit
- [ ] `history forward` → how a function evolved over commits
- [ ] `diff HEAD~5` → what symbols changed in last 5 commits
- [ ] Dead code detection informed by git (never-called AND not modified recently)

### v2.8 — Documentation Generation
- [ ] Auto-generate module docstrings from indexed symbols
- [ ] API reference markdown from class descriptions
- [ ] Architecture diagram generation (Mermaid)
- [ ] `document SymmetricMC/RBflows` → full module docs

## License

MIT
