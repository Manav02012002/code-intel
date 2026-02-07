#!/opt/miniconda3/envs/openclaw/bin/python3
"""
Code Intelligence Safety Guard
Path allowlisting, command blocking, and audit logging.

Usage:
  guard.py check-path <path>        — check if path is allowed
  guard.py check-cmd "<command>"    — check if command is safe
  guard.py audit                    — show audit log
  guard.py audit --tail 20          — show last 20 entries
  guard.py config                   — show current safety config
  guard.py allow-path <path>        — add path to allowlist
  guard.py remove-path <path>       — remove path from allowlist
  guard.py block-pattern <pattern>  — add a blocked command pattern
"""

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────

CONFIG_DIR = Path(__file__).parent.parent
CONFIG_FILE = CONFIG_DIR / "safety.json"
AUDIT_DB = CONFIG_DIR / "jobs" / "audit.db"

DEFAULT_CONFIG = {
    "version": 1,
    "allowed_paths": [
        "~/Dev"
    ],
    "blocked_patterns": [
        r"rm\s+(-rf?|--recursive)\s+[/~]",
        r"rm\s+-rf?\s+\.",
        r">\s*/etc/",
        r"curl\s+.*\|\s*(sh|bash|python)",
        r"wget\s+.*\|\s*(sh|bash|python)",
        r"chmod\s+777",
        r"sudo\s+",
        r"mkfs\.",
        r"dd\s+if=",
        r":()\{.*\}",
        r"eval\s+",
        r"\bexec\s+",
        r"os\.system\s*\(",
        r"subprocess\.call.*shell\s*=\s*True",
        r"__import__\s*\(",
        r"shutil\.rmtree\s*\(\s*['\"/~]",
    ],
    "blocked_commands": [
        "rm -rf /",
        "rm -rf ~",
        "rm -rf .",
        "rm -rf ..",
        "format",
        "mkfs",
    ],
    "max_job_duration_seconds": 3600,
    "max_concurrent_jobs": 5,
    "log_all_commands": True,
}

def load_config() -> dict:
    """Load safety config, creating default if missing."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        # Merge any missing defaults
        for k, v in DEFAULT_CONFIG.items():
            if k not in cfg:
                cfg[k] = v
        return cfg
    else:
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    """Save safety config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=2)

# ─── Audit Log ───────────────────────────────────────────────────

def init_audit():
    """Initialize audit database."""
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            command TEXT,
            result TEXT NOT NULL,
            reason TEXT,
            user TEXT DEFAULT 'openclaw'
        )
    """)
    conn.commit()
    conn.close()

def log_audit(action: str, target: str = None, command: str = None,
              result: str = "allowed", reason: str = None):
    """Log an action to the audit trail."""
    init_audit()
    conn = sqlite3.connect(AUDIT_DB)
    conn.execute("""
        INSERT INTO audit_log (timestamp, action, target, command, result, reason)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        action, target, command, result, reason
    ))
    conn.commit()
    conn.close()

# ─── Path Checking ───────────────────────────────────────────────

def normalize_path(p: str) -> Path:
    """Expand and resolve a path."""
    return Path(os.path.expanduser(p)).resolve()

def is_path_allowed(path: str, config: dict = None) -> tuple[bool, str]:
    """Check if a path is within the allowlist."""
    if config is None:
        config = load_config()

    target = normalize_path(path)
    allowed_paths = [normalize_path(p) for p in config['allowed_paths']]

    for allowed in allowed_paths:
        try:
            target.relative_to(allowed)
            return True, f"Path is under {allowed}"
        except ValueError:
            continue

    return False, f"Path {target} is not under any allowed directory: {config['allowed_paths']}"

# ─── Command Checking ────────────────────────────────────────────

def is_command_safe(command: str, config: dict = None) -> tuple[bool, str]:
    """Check if a command matches any blocked patterns."""
    if config is None:
        config = load_config()

    cmd_lower = command.lower().strip()

    # Check exact blocked commands
    for blocked in config.get('blocked_commands', []):
        if blocked.lower() in cmd_lower:
            return False, f"Blocked command: contains '{blocked}'"

    # Check regex patterns
    for pattern in config.get('blocked_patterns', []):
        try:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Blocked pattern: {pattern}"
        except re.error:
            continue  # skip invalid patterns

    return True, "Command appears safe"

# ─── Combined Guard ──────────────────────────────────────────────

def guard_index(root_path: str) -> tuple[bool, str]:
    """Guard for indexer: check path is allowed."""
    config = load_config()
    allowed, reason = is_path_allowed(root_path, config)
    action = "index"
    if allowed:
        log_audit(action, target=root_path, result="allowed")
    else:
        log_audit(action, target=root_path, result="blocked", reason=reason)
    return allowed, reason

def guard_job(command: str, label: str = None) -> tuple[bool, str]:
    """Guard for job submission: check command is safe."""
    config = load_config()

    # Check command safety
    safe, reason = is_command_safe(command, config)
    if not safe:
        log_audit("job_submit", command=command, result="blocked", reason=reason)
        return False, reason

    # Check if command references paths outside allowlist
    # Extract paths from command (naive but catches obvious cases)
    path_patterns = re.findall(r'(?:^|\s)([/~][^\s;|&]+)', command)
    for p in path_patterns:
        if p in ('/', '~'):
            continue
        allowed, path_reason = is_path_allowed(p, config)
        if not allowed:
            log_audit("job_submit", command=command, result="blocked", reason=path_reason)
            return False, f"Command references disallowed path: {p}"

    # Check concurrent job limit
    from jobs import DB_PATH as JOBS_DB
    if JOBS_DB.exists():
        conn = sqlite3.connect(JOBS_DB)
        running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status='running'").fetchone()[0]
        conn.close()
        if running >= config.get('max_concurrent_jobs', 5):
            reason = f"Too many concurrent jobs ({running}/{config['max_concurrent_jobs']})"
            log_audit("job_submit", command=command, result="blocked", reason=reason)
            return False, reason

    log_audit("job_submit", command=command, result="allowed")
    return True, "Command approved"

def guard_search(query: str) -> tuple[bool, str]:
    """Guard for searches: always allowed but logged."""
    log_audit("search", target=query, result="allowed")
    return True, "Search allowed"

# ─── CLI ─────────────────────────────────────────────────────────

def show_config():
    config = load_config()
    print("═══ Safety Configuration ═══\n")
    print("📁 Allowed paths:")
    for p in config['allowed_paths']:
        resolved = normalize_path(p)
        exists = "✅" if resolved.exists() else "❌"
        print(f"  {exists} {p} → {resolved}")
    print(f"\n🚫 Blocked patterns ({len(config['blocked_patterns'])}):")
    for p in config['blocked_patterns']:
        print(f"  • {p}")
    print(f"\n🚫 Blocked commands ({len(config.get('blocked_commands', []))}):")
    for c in config.get('blocked_commands', []):
        print(f"  • {c}")
    print(f"\n⏱️  Max job duration: {config.get('max_job_duration_seconds', 3600)}s")
    print(f"🔢 Max concurrent jobs: {config.get('max_concurrent_jobs', 5)}")

def show_audit(tail: int = 50):
    init_audit()
    conn = sqlite3.connect(AUDIT_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (tail,)
    ).fetchall()
    conn.close()

    if not rows:
        print("No audit entries yet.")
        return

    result_emoji = {'allowed': '✅', 'blocked': '🚫'}
    print(f"═══ Audit Log (last {len(rows)} entries) ═══\n")
    for row in reversed(rows):
        emoji = result_emoji.get(row['result'], '❓')
        ts = row['timestamp'][:19]
        target = row['target'] or row['command'] or ''
        target_short = target[:60] + ('...' if len(target) > 60 else '')
        reason_str = f" — {row['reason']}" if row['reason'] else ""
        print(f"  {emoji} [{ts}] {row['action']}: {target_short}{reason_str}")

def main():
    parser = argparse.ArgumentParser(description="Code Intelligence Safety Guard")
    sub = parser.add_subparsers(dest='cmd')

    # check-path
    p_path = sub.add_parser('check-path', help='Check if path is allowed')
    p_path.add_argument('path', help='Path to check')

    # check-cmd
    p_cmd = sub.add_parser('check-cmd', help='Check if command is safe')
    p_cmd.add_argument('command', help='Command to check')

    # audit
    p_audit = sub.add_parser('audit', help='Show audit log')
    p_audit.add_argument('--tail', type=int, default=50, help='Show last N entries')

    # config
    sub.add_parser('config', help='Show safety config')

    # allow-path
    p_allow = sub.add_parser('allow-path', help='Add allowed path')
    p_allow.add_argument('path', help='Path to allow')

    # remove-path
    p_remove = sub.add_parser('remove-path', help='Remove allowed path')
    p_remove.add_argument('path', help='Path to remove')

    # block-pattern
    p_block = sub.add_parser('block-pattern', help='Add blocked command pattern')
    p_block.add_argument('pattern', help='Regex pattern to block')

    args = parser.parse_args()

    if args.cmd == 'check-path':
        allowed, reason = is_path_allowed(args.path)
        log_audit('check-path', target=args.path, result='allowed' if allowed else 'blocked', reason=reason)
        emoji = '✅' if allowed else '🚫'
        print(f"{emoji} {reason}")
        sys.exit(0 if allowed else 1)

    elif args.cmd == 'check-cmd':
        safe, reason = is_command_safe(args.command)
        log_audit('check-cmd', command=args.command, result='allowed' if safe else 'blocked', reason=reason)
        emoji = '✅' if safe else '🚫'
        print(f"{emoji} {reason}")
        sys.exit(0 if safe else 1)

    elif args.cmd == 'audit':
        show_audit(args.tail)

    elif args.cmd == 'config':
        show_config()

    elif args.cmd == 'allow-path':
        config = load_config()
        p = args.path
        if p not in config['allowed_paths']:
            config['allowed_paths'].append(p)
            save_config(config)
            log_audit("config_change", target=f"allow-path {p}", result="allowed")
            print(f"✅ Added {p} to allowed paths")
        else:
            print(f"Path {p} is already allowed")

    elif args.cmd == 'remove-path':
        config = load_config()
        p = args.path
        if p in config['allowed_paths']:
            config['allowed_paths'].remove(p)
            save_config(config)
            log_audit("config_change", target=f"remove-path {p}", result="allowed")
            print(f"🚫 Removed {p} from allowed paths")
        else:
            print(f"Path {p} is not in the allowlist")

    elif args.cmd == 'block-pattern':
        config = load_config()
        config['blocked_patterns'].append(args.pattern)
        save_config(config)
        log_audit("config_change", target=f"block-pattern {args.pattern}", result="allowed")
        print(f"🚫 Added blocked pattern: {args.pattern}")

    else:
        parser.print_help()

if __name__ == "__main__":
    main()
