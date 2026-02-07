#!/opt/miniconda3/envs/openclaw/bin/python3
"""
Code Intelligence Indexer v2
Parses Python files using tree-sitter and stores symbols, imports, AND references in SQLite.
New in v2: tracks function/method calls and attribute accesses for usage analysis.
"""

import argparse
import sqlite3
import os
import sys
import time
from pathlib import Path
from typing import Generator

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

SKIP_DIRS = {
    '.git', 'node_modules', '.venv', 'venv', '__pycache__',
    'dist', 'build', '.eggs', '*.egg-info', '.tox', '.mypy_cache',
    '.pytest_cache', 'site-packages'
}

PY_LANGUAGE = Language(tspython.language())

def get_db_path() -> Path:
    return Path(__file__).parent / "db" / "code.db"

def init_db(conn: sqlite3.Connection):
    conn.executescript("""
        DROP TABLE IF EXISTS references_;
        DROP TABLE IF EXISTS symbols;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS imports;

        CREATE TABLE files (
            id INTEGER PRIMARY KEY,
            path TEXT UNIQUE NOT NULL,
            mtime REAL NOT NULL,
            indexed_at REAL NOT NULL
        );

        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            col_start INTEGER NOT NULL,
            col_end INTEGER NOT NULL,
            parent TEXT,
            signature TEXT,
            docstring TEXT,
            snippet TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id)
        );

        CREATE TABLE imports (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            module TEXT NOT NULL,
            name TEXT,
            alias TEXT,
            line INTEGER NOT NULL,
            FOREIGN KEY (file_id) REFERENCES files(id)
        );

        CREATE TABLE references_ (
            id INTEGER PRIMARY KEY,
            file_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            line INTEGER NOT NULL,
            col INTEGER NOT NULL,
            context TEXT,
            caller TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id)
        );

        CREATE INDEX idx_symbols_name ON symbols(name);
        CREATE INDEX idx_symbols_kind ON symbols(kind);
        CREATE INDEX idx_symbols_file ON symbols(file_id);
        CREATE INDEX idx_imports_module ON imports(module);
        CREATE INDEX idx_imports_name ON imports(name);
        CREATE INDEX idx_refs_name ON references_(name);
        CREATE INDEX idx_refs_file ON references_(file_id);
        CREATE INDEX idx_refs_kind ON references_(kind);
    """)
    conn.commit()

def should_skip(path: Path) -> bool:
    parts = path.parts
    for skip in SKIP_DIRS:
        if skip in parts:
            return True
    return False

def find_python_files(root: Path) -> Generator[Path, None, None]:
    for path in root.rglob("*.py"):
        if not should_skip(path):
            yield path

def get_node_text(source: bytes, node) -> str:
    return source[node.start_byte:node.end_byte].decode('utf-8', errors='ignore')

def extract_docstring(source: bytes, body_node) -> str | None:
    if body_node is None or body_node.child_count == 0:
        return None
    first_stmt = body_node.children[0]
    if first_stmt.type == 'expression_statement':
        expr = first_stmt.children[0] if first_stmt.child_count > 0 else None
        if expr and expr.type == 'string':
            doc = get_node_text(source, expr)
            if doc.startswith('"""') or doc.startswith("'''"):
                doc = doc[3:-3]
            elif doc.startswith('"') or doc.startswith("'"):
                doc = doc[1:-1]
            return doc.strip()[:500]
    return None

def extract_signature(source: bytes, node) -> str:
    params_node = node.child_by_field_name('parameters')
    if params_node:
        return get_node_text(source, params_node)
    return "()"

def extract_snippet(source: bytes, node, max_lines: int = 5) -> str:
    text = get_node_text(source, node)
    lines = text.split('\n')[:max_lines]
    return '\n'.join(lines)

def get_line_context(source: bytes, line_num: int, max_chars: int = 120) -> str:
    """Get the source line for context."""
    lines = source.split(b'\n')
    if 0 <= line_num < len(lines):
        return lines[line_num].decode('utf-8', errors='ignore').strip()[:max_chars]
    return ""

def parse_file(filepath: Path, parser: Parser) -> dict:
    source = filepath.read_bytes()
    tree = parser.parse(source)

    symbols = []
    imports = []
    references = []

    def current_scope_name(node):
        """Walk up to find the enclosing function/method/class name."""
        p = node.parent
        while p:
            if p.type in ('function_definition', 'async_function_definition', 'class_definition'):
                name_node = p.child_by_field_name('name')
                if name_node:
                    return get_node_text(source, name_node)
            p = p.parent
        return None

    def extract_references(node, parent_class=None):
        """Extract function calls and attribute accesses as references."""
        if node.type == 'call':
            func_node = node.child_by_field_name('function')
            if func_node:
                if func_node.type == 'identifier':
                    # Simple call: foo()
                    name = get_node_text(source, func_node)
                    references.append({
                        'name': name,
                        'kind': 'call',
                        'line': node.start_point[0] + 1,
                        'col': node.start_point[1],
                        'context': get_line_context(source, node.start_point[0]),
                        'caller': current_scope_name(node),
                    })
                elif func_node.type == 'attribute':
                    # Method call: obj.method()
                    attr_node = func_node.child_by_field_name('attribute')
                    obj_node = func_node.child_by_field_name('object')
                    if attr_node:
                        attr_name = get_node_text(source, attr_node)
                        obj_name = get_node_text(source, obj_node) if obj_node else None
                        full_name = f"{obj_name}.{attr_name}" if obj_name else attr_name
                        references.append({
                            'name': attr_name,
                            'kind': 'method_call',
                            'line': node.start_point[0] + 1,
                            'col': node.start_point[1],
                            'context': get_line_context(source, node.start_point[0]),
                            'caller': current_scope_name(node),
                        })
                        # Also store the full qualified name
                        if obj_name and obj_name != 'self':
                            references.append({
                                'name': full_name,
                                'kind': 'qualified_call',
                                'line': node.start_point[0] + 1,
                                'col': node.start_point[1],
                                'context': get_line_context(source, node.start_point[0]),
                                'caller': current_scope_name(node),
                            })

        # Recurse into children
        for child in node.children:
            extract_references(child, parent_class)

    def visit(node, parent_class=None):
        if node.type in ('function_definition', 'async_function_definition'):
            name_node = node.child_by_field_name('name')
            if name_node:
                name = get_node_text(source, name_node)
                body = node.child_by_field_name('body')
                is_async = node.type == 'async_function_definition'
                if parent_class:
                    kind = 'async_method' if is_async else 'method'
                else:
                    kind = 'async_function' if is_async else 'function'
                symbols.append({
                    'name': name,
                    'kind': kind,
                    'line_start': node.start_point[0] + 1,
                    'line_end': node.end_point[0] + 1,
                    'col_start': node.start_point[1],
                    'col_end': node.end_point[1],
                    'parent': parent_class,
                    'signature': extract_signature(source, node),
                    'docstring': extract_docstring(source, body),
                    'snippet': extract_snippet(source, node),
                })
        elif node.type == 'class_definition':
            name_node = node.child_by_field_name('name')
            if name_node:
                name = get_node_text(source, name_node)
                body = node.child_by_field_name('body')
                symbols.append({
                    'name': name,
                    'kind': 'class',
                    'line_start': node.start_point[0] + 1,
                    'line_end': node.end_point[0] + 1,
                    'col_start': node.start_point[1],
                    'col_end': node.end_point[1],
                    'parent': None,
                    'signature': '',
                    'docstring': extract_docstring(source, body),
                    'snippet': extract_snippet(source, node),
                })
                for child in node.children:
                    visit(child, parent_class=name)
                return
        elif node.type == 'import_statement':
            for child in node.children:
                if child.type == 'dotted_name':
                    imports.append({
                        'module': get_node_text(source, child),
                        'name': None,
                        'alias': None,
                        'line': node.start_point[0] + 1,
                    })
                elif child.type == 'aliased_import':
                    name_node = child.child_by_field_name('name')
                    alias_node = child.child_by_field_name('alias')
                    if name_node:
                        imports.append({
                            'module': get_node_text(source, name_node),
                            'name': None,
                            'alias': get_node_text(source, alias_node) if alias_node else None,
                            'line': node.start_point[0] + 1,
                        })
        elif node.type == 'import_from_statement':
            module_node = node.child_by_field_name('module_name')
            module = get_node_text(source, module_node) if module_node else ''
            for child in node.children:
                if child.type == 'dotted_name' and child != module_node:
                    imports.append({
                        'module': module,
                        'name': get_node_text(source, child),
                        'alias': None,
                        'line': node.start_point[0] + 1,
                    })
                elif child.type == 'aliased_import':
                    name_node = child.child_by_field_name('name')
                    alias_node = child.child_by_field_name('alias')
                    if name_node:
                        imports.append({
                            'module': module,
                            'name': get_node_text(source, name_node),
                            'alias': get_node_text(source, alias_node) if alias_node else None,
                            'line': node.start_point[0] + 1,
                        })
        for child in node.children:
            visit(child, parent_class)

    visit(tree.root_node)
    extract_references(tree.root_node)
    return {'symbols': symbols, 'imports': imports, 'references': references}

def index_directory(root: Path, db_path: Path, verbose: bool = False):
    root = root.expanduser().resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    init_db(conn)
    parser = Parser(PY_LANGUAGE)
    files = list(find_python_files(root))
    total = len(files)
    if verbose:
        print(f"Indexing {total} Python files in {root}")
    start = time.time()
    symbols_count = 0
    imports_count = 0
    refs_count = 0
    errors = 0
    for i, filepath in enumerate(files):
        try:
            rel_path = str(filepath.relative_to(root))
            mtime = filepath.stat().st_mtime
            cursor = conn.execute(
                "INSERT INTO files (path, mtime, indexed_at) VALUES (?, ?, ?)",
                (rel_path, mtime, time.time())
            )
            file_id = cursor.lastrowid
            result = parse_file(filepath, parser)
            for sym in result['symbols']:
                conn.execute("""
                    INSERT INTO symbols
                    (file_id, name, kind, line_start, line_end, col_start, col_end,
                     parent, signature, docstring, snippet)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_id, sym['name'], sym['kind'],
                    sym['line_start'], sym['line_end'],
                    sym['col_start'], sym['col_end'],
                    sym['parent'], sym['signature'],
                    sym['docstring'], sym['snippet']
                ))
                symbols_count += 1
            for imp in result['imports']:
                conn.execute("""
                    INSERT INTO imports (file_id, module, name, alias, line)
                    VALUES (?, ?, ?, ?, ?)
                """, (file_id, imp['module'], imp['name'], imp['alias'], imp['line']))
                imports_count += 1
            for ref in result['references']:
                conn.execute("""
                    INSERT INTO references_ (file_id, name, kind, line, col, context, caller)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (file_id, ref['name'], ref['kind'], ref['line'], ref['col'],
                      ref['context'], ref['caller']))
                refs_count += 1
            if verbose and (i + 1) % 100 == 0:
                print(f"  {i + 1}/{total} files...")
        except Exception as e:
            errors += 1
            if verbose:
                print(f"  Error parsing {filepath}: {e}", file=sys.stderr)
    conn.commit()
    conn.close()
    elapsed = time.time() - start
    print(f"Indexed {total} files in {elapsed:.2f}s")
    print(f"  {symbols_count} symbols")
    print(f"  {imports_count} imports")
    print(f"  {refs_count} references")
    if errors:
        print(f"  {errors} errors")

def main():
    parser = argparse.ArgumentParser(description="Index Python code for fast searching")
    parser.add_argument("root", nargs="?", default="~/Dev", help="Root directory to index")
    parser.add_argument("--db", default=None, help="Database path (default: ./db/code.db)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()
    root = Path(args.root).expanduser()
    db_path = Path(args.db) if args.db else get_db_path()
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        sys.exit(1)
    index_directory(root, db_path, verbose=args.verbose)

if __name__ == "__main__":
    main()
