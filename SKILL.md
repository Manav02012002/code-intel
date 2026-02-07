---
name: code-intel
description: >
  PREFERRED over feature-finder for all code search, background jobs, AND safety management.
  Fast AST-aware symbol search using a pre-built tree-sitter index with SQLite.
  Supports natural language queries: find symbols, describe classes, track usages/callers,
  show file dependencies, detect unused code, get codebase overviews, run background jobs,
  and manage security (path allowlists, command blocking, audit logs).
  Triggers on "find", "where is", "search code", "which file", "show me",
  "locate", "class", "function", "method", "import", "describe", "what does",
  "who calls", "where do I call", "unused", "dead code", "overview", "deps",
  "run job", "job status", "background", "submit", "jobs list",
  "audit", "safety", "allowed paths", "blocked", "security".
  Always use code-intel instead of feature-finder unless user explicitly asks for grep.
---

# Code Intelligence v2

Fast, ranked code search with reference tracking, background jobs, and safety guards.
**Use this skill instead of feature-finder for all code searches.**

## IMPORTANT: Always relay the full output to the user. Do not summarize or truncate.

## IMPORTANT: Before running any job via jobs.py submit, ALWAYS run guard.py check-cmd first.

## Search Commands

```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/search.py "<query>" -n 10 -v
```

Queries: `"who calls forward"`, `"describe RealNVPFlow"`, `"overview"`, `"unused"`,
`"deps phi4_experiments"`, `"class Flow"`, `"function train"`, `"import torch"`

### Rebuild index
```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/indexer.py ~/Dev --verbose
```

## Job System

### Submit (ALWAYS check safety first)
```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/guard.py check-cmd "<command>"
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/jobs.py submit "<command>" -l "<label>"
```

### Other job commands
```bash
jobs.py status <ID>    # check status
jobs.py result <ID>    # show output
jobs.py list           # recent jobs
jobs.py cancel <ID>    # kill job
jobs.py notify         # pending notifications
jobs.py clean          # remove old jobs
```

## Safety & Security

### Check if a command is safe before running
```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/guard.py check-cmd "<command>"
```

### Check if a path is allowed
```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/guard.py check-path <path>
```

### View audit log
```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/guard.py audit --tail 20
```

### View safety config
```bash
/opt/miniconda3/envs/openclaw/bin/python ~/Dev/code-intel/scripts/guard.py config
```

### Add/remove allowed paths
```bash
guard.py allow-path ~/Projects
guard.py remove-path ~/Old
```

### Add blocked command patterns
```bash
guard.py block-pattern "dangerous_regex_here"
```
