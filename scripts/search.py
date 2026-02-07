#!/opt/miniconda3/envs/openclaw/bin/python3
"""
Code Intelligence Search v2
Natural language query interface with usage tracking, describe, deps, and overview.

New in v2:
  - "where do I call X" / "who calls X" → usage search via references table
  - "describe X" / "what does X do" → class/function + docstring + members
  - "deps FILE" / "what imports does FILE use" → per-file import map
  - "overview" / "architecture" → codebase summary
  - "unused" / "dead code" → cross-reference definitions vs calls
  - "callers of X" → reverse call graph
  - All original search modes still work
"""

import argparse
import sqlite3
import sys
import re
from pathlib import Path
from dataclasses import dataclass, field

def get_db_path() -> Path:
    return Path(__file__).parent / "db" / "code.db"

# ─── Data classes ────────────────────────────────────────────────

@dataclass
class SearchResult:
    path: str
    name: str
    kind: str
    line_start: int
    line_end: int
    parent: str | None
    signature: str
    docstring: str | None
    snippet: str
    score: int
    match_reason: str

@dataclass
class RefResult:
    path: str
    name: str
    kind: str
    line: int
    context: str
    caller: str | None

@dataclass
class DescribeResult:
    name: str
    kind: str
    path: str
    line_start: int
    signature: str
    docstring: str | None
    snippet: str
    methods: list[dict] = field(default_factory=list)

# ─── Query parser ────────────────────────────────────────────────

NL_PATTERNS = [
    # Usage / callers
    (r'^(?:where|when) do I (?:call|use|invoke)\s+(.+)', 'usage'),
    (r'^(?:who|what) calls?\s+(.+)', 'usage'),
    (r'^(?:find |show )?(?:all )?(?:usages?|references?|calls?) (?:of|to|for)\s+(.+)', 'usage'),
    (r'^callers? (?:of|for)\s+(.+)', 'usage'),
    # Describe
    (r'^(?:describe|explain|what does|what is|tell me about|show me)\s+(.+?)(?:\s+do)?$', 'describe'),
    # Deps / imports for a file
    (r'^(?:what )?(?:imports?|deps|dependencies) (?:does|of|for|in)\s+(.+?)(?:\s+use)?$', 'deps'),
    (r'^deps?\s+(.+)', 'deps'),
    # Overview
    (r'^(?:overview|architecture|summary|codebase|stats?)$', 'overview'),
    (r'^(?:show|give) (?:me )?(?:an? )?(?:overview|architecture|summary)', 'overview'),
    # Unused / dead code
    (r'^(?:find |show )?(?:unused|dead)(?: code| functions?| methods?)?', 'unused'),
    # File listing
    (r'^(?:show |list )?(?:all )?files?\s+(?:in|under|from)\s+(.+)', 'files'),
    # Explicit kind queries (keep from v1)
    (r'^(?:find |search )?(?:all )?(?:class(?:es)?)\s+(.+)', 'class'),
    (r'^(?:find |search )?(?:all )?(?:function|func|def)s?\s+(.+)', 'function'),
    (r'^(?:find |search )?(?:all )?methods?\s+(.+)', 'method'),
    (r'^(?:find |search )?(?:all )?imports?\s+(.+)', 'import'),
]

def parse_query(query_str: str) -> dict:
    """Parse natural language into structured query."""
    q = query_str.strip()
    q_lower = q.lower()

    # Try NL patterns first
    for pattern, qtype in NL_PATTERNS:
        m = re.match(pattern, q_lower)
        if m:
            groups = m.groups()
            target = groups[0].strip() if groups else ''
            # Clean up target: remove trailing punctuation, quotes
            target = target.strip('?"\'.,!')
            if qtype == 'overview':
                return {'type': 'overview', 'query': ''}
            elif qtype == 'unused':
                return {'type': 'unused', 'query': target}
            elif qtype == 'usage':
                return {'type': 'usage', 'query': target}
            elif qtype == 'describe':
                return {'type': 'describe', 'query': target}
            elif qtype == 'deps':
                return {'type': 'deps', 'query': target}
            elif qtype == 'files':
                return {'type': 'file', 'query': target}
            elif qtype in ('class', 'function', 'method'):
                return {'type': 'symbol', 'kind': qtype, 'query': target}
            elif qtype == 'import':
                return {'type': 'import', 'query': target}

    # Fall back to v1 keyword-prefix parsing
    parts = q.split()
    if not parts:
        return {'type': 'symbol', 'query': ''}
    first = parts[0].lower()
    if first in ('function', 'func', 'def'):
        return {'type': 'symbol', 'kind': 'function', 'query': ' '.join(parts[1:])}
    elif first == 'class':
        return {'type': 'symbol', 'kind': 'class', 'query': ' '.join(parts[1:])}
    elif first == 'method':
        rest = parts[1:]
        if 'in' in rest:
            idx = rest.index('in')
            return {'type': 'symbol', 'kind': 'method', 'query': ' '.join(rest[:idx]), 'parent': ' '.join(rest[idx + 1:])}
        return {'type': 'symbol', 'kind': 'method', 'query': ' '.join(rest)}
    elif first == 'import':
        return {'type': 'import', 'query': ' '.join(parts[1:])}
    elif first == 'file':
        return {'type': 'file', 'query': ' '.join(parts[1:])}
    elif first in ('usage', 'usages', 'callers', 'calls'):
        return {'type': 'usage', 'query': ' '.join(parts[1:])}
    elif first in ('describe', 'explain'):
        return {'type': 'describe', 'query': ' '.join(parts[1:])}
    elif first in ('deps', 'dependencies'):
        return {'type': 'deps', 'query': ' '.join(parts[1:])}
    elif first in ('overview', 'architecture', 'summary', 'stats'):
        return {'type': 'overview', 'query': ''}
    elif first in ('unused', 'dead'):
        return {'type': 'unused', 'query': ' '.join(parts[1:])}
    else:
        return {'type': 'symbol', 'query': q}

# ─── Search functions ────────────────────────────────────────────

def rank_results(results: list[dict], query: str, kind_filter: str | None) -> list[SearchResult]:
    scored = []
    query_lower = query.lower()
    for r in results:
        score = 0
        reasons = []
        name_lower = r['name'].lower()
        if name_lower == query_lower:
            score += 100
            reasons.append("exact match")
        elif name_lower.startswith(query_lower):
            score += 50
            reasons.append("prefix match")
        elif query_lower in name_lower:
            score += 25
            reasons.append("contains query")
        kind_scores = {'class': 40, 'function': 35, 'async_function': 35, 'method': 20, 'async_method': 20}
        score += kind_scores.get(r['kind'], 0)
        if r['docstring']:
            score += 10
        if len(r['name']) < 20:
            score += 5
        if r['docstring'] and query_lower in r['docstring'].lower():
            score += 15
            reasons.append("in docstring")
        scored.append(SearchResult(
            path=r['path'], name=r['name'], kind=r['kind'],
            line_start=r['line_start'], line_end=r['line_end'],
            parent=r['parent'], signature=r['signature'],
            docstring=r['docstring'], snippet=r['snippet'],
            score=score, match_reason=", ".join(reasons) if reasons else "partial match"
        ))
    scored.sort(key=lambda x: x.score, reverse=True)
    return scored

def search_symbols(query: str, db_path: Path, kind: str | None = None,
                   parent: str | None = None, max_results: int = 10, regex: bool = False) -> list[SearchResult]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id = f.id WHERE 1=1"
    params = []
    if regex:
        pattern = query.replace('*', '%')
        sql += " AND s.name LIKE ?"
        params.append(pattern)
    else:
        sql += " AND s.name LIKE ?"
        params.append(f"%{query}%")
    if kind:
        if kind in ('func', 'function', 'def'):
            sql += " AND s.kind IN ('function', 'async_function')"
        elif kind in ('method',):
            sql += " AND s.kind IN ('method', 'async_method')"
        elif kind in ('class',):
            sql += " AND s.kind = 'class'"
        else:
            sql += " AND s.kind LIKE ?"
            params.append(f"%{kind}%")
    if parent:
        sql += " AND s.parent LIKE ?"
        params.append(f"%{parent}%")
    sql += " LIMIT 100"
    cursor = conn.execute(sql, params)
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    ranked = rank_results(results, query, kind)
    return ranked[:max_results]

def search_imports(query: str, db_path: Path, max_results: int = 10) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT i.*, f.path FROM imports i JOIN files f ON i.file_id = f.id WHERE i.module LIKE ? OR i.name LIKE ? LIMIT ?"
    cursor = conn.execute(sql, (f"%{query}%", f"%{query}%", max_results))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

def search_files(query: str, db_path: Path, max_results: int = 10) -> list[dict]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT f.*, (SELECT COUNT(*) FROM symbols WHERE file_id = f.id) as symbol_count FROM files f WHERE f.path LIKE ? LIMIT ?"
    cursor = conn.execute(sql, (f"%{query}%", max_results))
    results = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return results

# ─── NEW: Phase 2 search functions ──────────────────────────────

def search_usages(query: str, db_path: Path, max_results: int = 15) -> list[RefResult]:
    """Find where a symbol is called/used across the codebase."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Check if references_ table exists (backwards compat with v1 index)
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'references_' not in tables:
        conn.close()
        return []

    sql = """
        SELECT r.*, f.path FROM references_ r
        JOIN files f ON r.file_id = f.id
        WHERE r.name LIKE ?
        ORDER BY r.name = ? DESC, r.kind, f.path, r.line
        LIMIT ?
    """
    cursor = conn.execute(sql, (f"%{query}%", query, max_results))
    results = []
    for row in cursor.fetchall():
        results.append(RefResult(
            path=row['path'], name=row['name'], kind=row['kind'],
            line=row['line'], context=row['context'], caller=row['caller']
        ))
    conn.close()
    return results

def describe_symbol(query: str, db_path: Path) -> list[DescribeResult]:
    """Get detailed info about a symbol: definition + methods (for classes)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Find the symbol itself
    sql = """
        SELECT s.*, f.path FROM symbols s
        JOIN files f ON s.file_id = f.id
        WHERE s.name LIKE ?
        ORDER BY s.name = ? DESC, s.kind = 'class' DESC
        LIMIT 5
    """
    cursor = conn.execute(sql, (f"%{query}%", query))
    results = []
    for row in cursor.fetchall():
        d = DescribeResult(
            name=row['name'], kind=row['kind'], path=row['path'],
            line_start=row['line_start'], signature=row['signature'] or '',
            docstring=row['docstring'], snippet=row['snippet']
        )
        # If it's a class, also fetch its methods
        if row['kind'] == 'class':
            methods_sql = """
                SELECT s.name, s.kind, s.signature, s.line_start, s.docstring
                FROM symbols s WHERE s.file_id = ? AND s.parent = ?
                ORDER BY s.line_start
            """
            for m in conn.execute(methods_sql, (row['file_id'], row['name'])).fetchall():
                d.methods.append(dict(m))
        results.append(d)
    conn.close()
    return results

def get_file_deps(query: str, db_path: Path) -> tuple[str | None, list[dict]]:
    """Get all imports for a specific file."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # Find the file
    file_row = conn.execute("SELECT * FROM files WHERE path LIKE ? LIMIT 1", (f"%{query}%",)).fetchone()
    if not file_row:
        conn.close()
        return None, []

    sql = "SELECT * FROM imports WHERE file_id = ? ORDER BY line"
    imports = [dict(r) for r in conn.execute(sql, (file_row['id'],)).fetchall()]
    conn.close()
    return file_row['path'], imports

def get_overview(db_path: Path) -> dict:
    """Get codebase overview stats."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    stats = {}
    stats['total_files'] = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    stats['total_symbols'] = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    stats['total_imports'] = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]

    # Check for references table
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'references_' in tables:
        stats['total_refs'] = conn.execute("SELECT COUNT(*) FROM references_").fetchone()[0]
    else:
        stats['total_refs'] = 0

    # By kind
    stats['by_kind'] = {}
    for row in conn.execute("SELECT kind, COUNT(*) as cnt FROM symbols GROUP BY kind ORDER BY cnt DESC"):
        stats['by_kind'][row['kind']] = row['cnt']

    # Top-level dirs (first path component)
    stats['top_dirs'] = {}
    for row in conn.execute("SELECT path FROM files"):
        parts = row['path'].split('/')
        top = parts[0] if len(parts) > 1 else '.'
        stats['top_dirs'][top] = stats['top_dirs'].get(top, 0) + 1

    # Most used imports
    stats['top_imports'] = []
    for row in conn.execute("""
        SELECT module, COUNT(*) as cnt FROM imports
        GROUP BY module ORDER BY cnt DESC LIMIT 10
    """):
        stats['top_imports'].append((row['module'], row['cnt']))

    # Largest files (by symbol count)
    stats['largest_files'] = []
    for row in conn.execute("""
        SELECT f.path, COUNT(s.id) as sym_count
        FROM files f JOIN symbols s ON s.file_id = f.id
        GROUP BY f.id ORDER BY sym_count DESC LIMIT 10
    """):
        stats['largest_files'].append((row['path'], row['sym_count']))

    conn.close()
    return stats

def find_unused(db_path: Path, max_results: int = 20) -> list[dict]:
    """Find functions/methods that are defined but never called."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if 'references_' not in tables:
        conn.close()
        return []

    # Get all function/method definitions
    sql = """
        SELECT s.name, s.kind, s.parent, s.line_start, f.path
        FROM symbols s JOIN files f ON s.file_id = f.id
        WHERE s.kind IN ('function', 'async_function', 'method', 'async_method')
        AND s.name NOT LIKE '\\_\\_%' ESCAPE '\\'
        AND s.name NOT IN ('main', 'setup', 'teardown', 'setUp', 'tearDown')
    """
    definitions = [dict(r) for r in conn.execute(sql).fetchall()]

    # Get all referenced names
    ref_names = set()
    for row in conn.execute("SELECT DISTINCT name FROM references_"):
        ref_names.add(row['name'])

    unused = []
    for d in definitions:
        if d['name'] not in ref_names:
            unused.append(d)
        if len(unused) >= max_results:
            break

    conn.close()
    return unused

# ─── Formatters ──────────────────────────────────────────────────

def format_result(r: SearchResult, index: int, verbose: bool = False) -> str:
    lines = []
    kind_emoji = {'class': '📦', 'function': '🔧', 'async_function': '⚡', 'method': '  →', 'async_method': '  ⚡→'}
    emoji = kind_emoji.get(r.kind, '•')
    location = f"{r.path}:{r.line_start}"
    if r.parent:
        location += f" ({r.parent})"
    lines.append(f"#{index + 1} {emoji} {r.name} — {r.kind}")
    lines.append(f"   📍 {location}")
    if r.match_reason:
        lines.append(f"   💡 {r.match_reason}")
    if r.docstring:
        doc = r.docstring[:100].replace('\n', ' ')
        if len(r.docstring) > 100:
            doc += "..."
        lines.append(f"   📝 {doc}")
    if verbose and r.snippet:
        lines.append("   ┌─────")
        for line in r.snippet.split('\n')[:5]:
            lines.append(f"   │ {line}")
        lines.append("   └─────")
    return '\n'.join(lines)

def format_import(imp: dict, index: int) -> str:
    if imp['name']:
        text = f"from {imp['module']} import {imp['name']}"
        if imp['alias']:
            text += f" as {imp['alias']}"
    else:
        text = f"import {imp['module']}"
        if imp['alias']:
            text += f" as {imp['alias']}"
    return f"#{index + 1} {imp['path']}:{imp['line']}\n   {text}"

def format_ref(r: RefResult, index: int) -> str:
    kind_label = {'call': '📞', 'method_call': '📞.', 'qualified_call': '📞::'}
    emoji = kind_label.get(r.kind, '📞')
    caller_str = f" (in {r.caller})" if r.caller else ""
    lines = [
        f"#{index + 1} {emoji} {r.name}{caller_str}",
        f"   📍 {r.path}:{r.line}",
        f"   └ {r.context}",
    ]
    return '\n'.join(lines)

def format_describe(d: DescribeResult) -> str:
    lines = []
    kind_emoji = {'class': '📦', 'function': '🔧', 'async_function': '⚡', 'method': '→', 'async_method': '⚡→'}
    emoji = kind_emoji.get(d.kind, '•')
    lines.append(f"{emoji} {d.name} — {d.kind}")
    lines.append(f"📍 {d.path}:{d.line_start}")
    if d.signature:
        lines.append(f"📝 Signature: {d.name}{d.signature}")
    if d.docstring:
        lines.append(f"\n{d.docstring}")
    if d.methods:
        lines.append(f"\n🔧 Methods ({len(d.methods)}):")
        for m in d.methods:
            sig = m.get('signature', '()')
            doc_preview = ""
            if m.get('docstring'):
                doc_preview = f" — {m['docstring'][:60].replace(chr(10), ' ')}"
            lines.append(f"  → {m['name']}{sig}{doc_preview}")
    if d.snippet:
        lines.append("\n┌─────")
        for line in d.snippet.split('\n')[:8]:
            lines.append(f"│ {line}")
        lines.append("└─────")
    return '\n'.join(lines)

def format_overview(stats: dict) -> str:
    lines = [
        "═══ Codebase Overview ═══",
        f"📁 Files: {stats['total_files']}",
        f"🔤 Symbols: {stats['total_symbols']}",
        f"📦 Imports: {stats['total_imports']}",
        f"📞 References: {stats['total_refs']}",
        "",
        "By kind:",
    ]
    for k, v in stats['by_kind'].items():
        lines.append(f"  {k}: {v}")

    lines.append("\n📂 Top directories:")
    for d, count in sorted(stats['top_dirs'].items(), key=lambda x: x[1], reverse=True)[:10]:
        lines.append(f"  {d}/ ({count} files)")

    lines.append("\n📦 Most used imports:")
    for mod, count in stats['top_imports']:
        lines.append(f"  {mod} ({count}×)")

    lines.append("\n📊 Largest files:")
    for path, count in stats['largest_files'][:5]:
        lines.append(f"  {path} ({count} symbols)")

    return '\n'.join(lines)

def format_unused(unused: list[dict]) -> str:
    if not unused:
        return "✅ No obviously unused functions found (or re-index with v2 indexer for reference tracking)."
    lines = [f"⚠️  Potentially unused ({len(unused)} found):\n"]
    for i, u in enumerate(unused):
        parent_str = f"{u['parent']}." if u['parent'] else ""
        lines.append(f"  {i+1}. {parent_str}{u['name']} [{u['kind']}]")
        lines.append(f"     📍 {u['path']}:{u['line_start']}")
    lines.append("\n(Note: may include functions called dynamically or from external code)")
    return '\n'.join(lines)

# ─── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Search indexed code (v2)")
    parser.add_argument("query", nargs="*", help="Search query")
    parser.add_argument("--db", default=None, help="Database path")
    parser.add_argument("-n", "--max", type=int, default=10, help="Max results")
    parser.add_argument("-k", "--kind", help="Filter by kind (function/class/method)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show snippets")
    parser.add_argument("--regex", action="store_true", help="Use pattern matching (* wildcards)")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else get_db_path()
    if not db_path.exists():
        print("Error: Index not found. Run indexer.py first.", file=sys.stderr)
        sys.exit(1)

    query_str = ' '.join(args.query) if args.query else ''

    if not query_str:
        # No query → show overview
        stats = get_overview(db_path)
        print(format_overview(stats))
        return

    parsed = parse_query(query_str)
    if args.kind:
        parsed['kind'] = args.kind

    # ── Route to handler ──
    if parsed['type'] == 'overview':
        stats = get_overview(db_path)
        print(format_overview(stats))

    elif parsed['type'] == 'usage':
        results = search_usages(parsed['query'], db_path, args.max)
        if not results:
            print(f"No usages of '{parsed['query']}' found.")
            print("(Hint: re-run indexer v2 to build references table)")
        else:
            print(f"Found {len(results)} usage(s) of '{parsed['query']}':\n")
            for i, r in enumerate(results):
                print(format_ref(r, i))
                print()

    elif parsed['type'] == 'describe':
        results = describe_symbol(parsed['query'], db_path)
        if not results:
            print(f"No symbol '{parsed['query']}' found.")
        else:
            for d in results:
                print(format_describe(d))
                print()

    elif parsed['type'] == 'deps':
        filepath, imports = get_file_deps(parsed['query'], db_path)
        if not filepath:
            print(f"No file matching '{parsed['query']}' found.")
        elif not imports:
            print(f"No imports in {filepath}.")
        else:
            print(f"Imports in {filepath} ({len(imports)}):\n")
            for i, imp in enumerate(imports):
                imp['path'] = filepath  # add path for formatter
                print(format_import(imp, i))

    elif parsed['type'] == 'unused':
        unused = find_unused(db_path, args.max)
        print(format_unused(unused))

    elif parsed['type'] == 'import':
        results = search_imports(parsed['query'], db_path, args.max)
        if not results:
            print("No imports found.")
        else:
            print(f"Found {len(results)} import(s):\n")
            for i, r in enumerate(results):
                print(format_import(r, i))
                print()

    elif parsed['type'] == 'file':
        results = search_files(parsed['query'], db_path, args.max)
        if not results:
            print("No files found.")
        else:
            print(f"Found {len(results)} file(s):\n")
            for i, r in enumerate(results):
                print(f"#{i + 1} {r['path']} ({r['symbol_count']} symbols)")

    else:
        # Default: symbol search
        results = search_symbols(parsed['query'], db_path, kind=parsed.get('kind'),
                                  parent=parsed.get('parent'), max_results=args.max, regex=args.regex)
        if not results:
            print("No symbols found.")
        else:
            print(f"Found {len(results)} result(s):\n")
            for i, r in enumerate(results):
                print(format_result(r, i, verbose=args.verbose))
                print()

if __name__ == "__main__":
    main()
