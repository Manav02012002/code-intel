#!/opt/miniconda3/envs/openclaw/bin/python3
"""
Code Intelligence Job System
Background execution with job IDs, artifacts, and status tracking.

Usage:
  jobs.py submit "<command>"    — run command in background, return job ID
  jobs.py status <id>           — check job status
  jobs.py result <id>           — show job output
  jobs.py list                  — show recent jobs
  jobs.py cancel <id>           — kill a running job
  jobs.py clean                 — remove old completed jobs
"""

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

JOBS_DIR = Path(__file__).parent.parent / "jobs"
ARTIFACTS_DIR = JOBS_DIR / "artifacts"
DB_PATH = JOBS_DIR / "jobs.db"

def init():
    """Ensure directories and DB exist."""
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            command TEXT NOT NULL,
            label TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            pid INTEGER,
            created_at TEXT NOT NULL,
            finished_at TEXT,
            exit_code INTEGER,
            output_file TEXT,
            error TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL,
            delivered INTEGER DEFAULT 0,
            FOREIGN KEY (job_id) REFERENCES jobs(id)
        )
    """)
    conn.commit()
    conn.close()

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

# ─── Submit ──────────────────────────────────────────────────────

def submit_job(command: str, label: str | None = None) -> int:
    """Submit a command for background execution. Returns job ID."""
    init()
    conn = sqlite3.connect(DB_PATH)

    # Insert job record
    cursor = conn.execute(
        "INSERT INTO jobs (command, label, status, created_at) VALUES (?, ?, 'running', ?)",
        (command, label, now_iso())
    )
    job_id = cursor.lastrowid
    output_file = str(ARTIFACTS_DIR / f"job_{job_id}.out")
    error_file = str(ARTIFACTS_DIR / f"job_{job_id}.err")

    # Launch subprocess detached
    with open(output_file, 'w') as stdout_f, open(error_file, 'w') as stderr_f:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=stdout_f,
            stderr=stderr_f,
            start_new_session=True,  # detach from parent
        )

    # Update with PID and output path
    conn.execute(
        "UPDATE jobs SET pid = ?, output_file = ? WHERE id = ?",
        (proc.pid, output_file, job_id)
    )
    conn.commit()
    conn.close()

    # Start the watcher (also detached)
    watcher_script = f"""
import sqlite3, time, os
from datetime import datetime, timezone
DB = "{DB_PATH}"
JOB_ID = {job_id}
PID = {proc.pid}

# Poll until process exits (can't waitpid on non-child)
while True:
    try:
        os.kill(PID, 0)
        time.sleep(0.5)
    except OSError:
        break
time.sleep(1)  # let stdout/stderr flush to disk

# Read output for notification preview
out_file = "{output_file}"
preview = ""
try:
    with open(out_file) as f:
        lines = f.readlines()
        preview = "".join(lines[-5:])[:300] if lines else "(no output)"
except:
    preview = "(could not read output)"

# Check for errors
err_file = "{error_file}"
err_text = ""
try:
    with open(err_file) as f:
        err_text = f.read().strip()
except:
    pass

exit_code = 1 if err_text else 0
status_word = "finished with warnings" if err_text else "finished"

conn = sqlite3.connect(DB)
now = datetime.now(timezone.utc).isoformat()
conn.execute("UPDATE jobs SET status='done', finished_at=?, exit_code=? WHERE id=?", (now, exit_code, JOB_ID))
if err_text:
    conn.execute("UPDATE jobs SET error=? WHERE id=?", (err_text[:500], JOB_ID))
msg = f"Job #{{JOB_ID}} {{status_word}}.\\n{{preview}}"
conn.execute("INSERT INTO notifications (job_id, message, created_at) VALUES (?, ?, ?)", (JOB_ID, msg, now))
conn.commit()
conn.close()
"""
    # Write watcher to temp file and run detached
    watcher_path = ARTIFACTS_DIR / f"watcher_{job_id}.py"
    watcher_path.write_text(watcher_script)
    subprocess.Popen(
        [sys.executable, str(watcher_path)],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    print(f"⚡ Job #{job_id} submitted (PID {proc.pid})")
    if label:
        print(f"   Label: {label}")
    print(f"   Command: {command}")
    print(f"   Output: {output_file}")
    return job_id

# ─── Status ──────────────────────────────────────────────────────

def check_and_update_status(conn: sqlite3.Connection, job: dict) -> dict:
    """Check if a 'running' job is actually still running."""
    if job['status'] != 'running' or not job['pid']:
        return job
    try:
        os.kill(job['pid'], 0)  # Check if process exists
    except OSError:
        # Process is gone but status wasn't updated
        conn.execute(
            "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
            (now_iso(), job['id'])
        )
        conn.commit()
        job = dict(job)
        job['status'] = 'done'
    return job

def get_job_status(job_id: int) -> None:
    init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        print(f"❌ Job #{job_id} not found.")
        conn.close()
        return

    job = dict(row)
    job = check_and_update_status(conn, job)

    status_emoji = {'running': '🔄', 'done': '✅', 'failed': '❌', 'cancelled': '🚫'}
    emoji = status_emoji.get(job['status'], '❓')

    print(f"{emoji} Job #{job['id']} — {job['status']}")
    if job['label']:
        print(f"   Label: {job['label']}")
    print(f"   Command: {job['command']}")
    print(f"   Started: {job['created_at']}")
    if job['finished_at']:
        print(f"   Finished: {job['finished_at']}")
    if job['exit_code'] is not None:
        print(f"   Exit code: {job['exit_code']}")
    if job['error']:
        err_preview = job['error'][:200]
        print(f"   Errors: {err_preview}")
    conn.close()

# ─── Result ──────────────────────────────────────────────────────

def get_job_result(job_id: int, tail: int = 0) -> None:
    init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        print(f"❌ Job #{job_id} not found.")
        conn.close()
        return

    job = dict(row)
    job = check_and_update_status(conn, job)

    if not job['output_file'] or not Path(job['output_file']).exists():
        print(f"❌ No output file for Job #{job_id}.")
        conn.close()
        return

    output = Path(job['output_file']).read_text()
    if not output.strip():
        # Check error file too
        err_path = Path(job['output_file'].replace('.out', '.err'))
        if err_path.exists():
            err = err_path.read_text().strip()
            if err:
                print(f"⚠️  Job #{job_id} — no stdout, but stderr:\n{err}")
                conn.close()
                return
        print(f"Job #{job_id} — no output yet (still running or empty).")
        conn.close()
        return

    if tail > 0:
        lines = output.split('\n')
        output = '\n'.join(lines[-tail:])

    status_emoji = {'running': '🔄', 'done': '✅', 'failed': '❌', 'cancelled': '🚫'}
    emoji = status_emoji.get(job['status'], '❓')
    print(f"{emoji} Job #{job_id} output ({job['status']}):")
    print("─" * 40)
    print(output)
    print("─" * 40)
    conn.close()

# ─── List ────────────────────────────────────────────────────────

def list_jobs(limit: int = 10) -> None:
    init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()

    if not rows:
        print("No jobs found.")
        conn.close()
        return

    status_emoji = {'running': '🔄', 'done': '✅', 'failed': '❌', 'cancelled': '🚫'}

    print(f"Recent jobs ({len(rows)}):\n")
    for row in rows:
        job = dict(row)
        job = check_and_update_status(conn, job)
        emoji = status_emoji.get(job['status'], '❓')
        label_str = f" [{job['label']}]" if job['label'] else ""
        cmd_short = job['command'][:60] + ("..." if len(job['command']) > 60 else "")
        print(f"  {emoji} #{job['id']}{label_str} — {cmd_short}")
        print(f"     {job['status']} | {job['created_at'][:19]}")
    conn.close()

# ─── Cancel ──────────────────────────────────────────────────────

def cancel_job(job_id: int) -> None:
    init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row:
        print(f"❌ Job #{job_id} not found.")
        conn.close()
        return

    job = dict(row)
    if job['status'] != 'running':
        print(f"Job #{job_id} is already {job['status']}.")
        conn.close()
        return

    if job['pid']:
        try:
            os.killpg(os.getpgid(job['pid']), signal.SIGTERM)
            print(f"🚫 Sent SIGTERM to Job #{job_id} (PID {job['pid']})")
        except (OSError, ProcessLookupError):
            print(f"Process already gone for Job #{job_id}")

    conn.execute(
        "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=?",
        (now_iso(), job_id)
    )
    conn.commit()
    conn.close()
    print(f"🚫 Job #{job_id} cancelled.")

# ─── Notifications ───────────────────────────────────────────────

def get_notifications() -> None:
    """Get undelivered notifications (for polling by OpenClaw)."""
    init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM notifications WHERE delivered = 0 ORDER BY created_at"
    ).fetchall()

    if not rows:
        print("No pending notifications.")
        conn.close()
        return

    for row in rows:
        print(row['message'])
        print()
        conn.execute("UPDATE notifications SET delivered = 1 WHERE id = ?", (row['id'],))

    conn.commit()
    conn.close()

# ─── Clean ───────────────────────────────────────────────────────

def clean_jobs(keep: int = 20) -> None:
    """Remove old completed jobs and their artifacts."""
    init()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Get jobs to delete (keep most recent N)
    rows = conn.execute("""
        SELECT id, output_file FROM jobs
        WHERE status IN ('done', 'failed', 'cancelled')
        ORDER BY id DESC
        LIMIT -1 OFFSET ?
    """, (keep,)).fetchall()

    if not rows:
        print("Nothing to clean.")
        conn.close()
        return

    for row in rows:
        # Delete artifact files
        if row['output_file']:
            for ext in ['.out', '.err']:
                p = Path(row['output_file']).with_suffix(ext)
                if p.exists():
                    p.unlink()
            # Delete watcher script
            watcher = ARTIFACTS_DIR / f"watcher_{row['id']}.py"
            if watcher.exists():
                watcher.unlink()
        conn.execute("DELETE FROM notifications WHERE job_id = ?", (row['id'],))
        conn.execute("DELETE FROM jobs WHERE id = ?", (row['id'],))

    conn.commit()
    conn.close()
    print(f"🧹 Cleaned {len(rows)} old jobs.")

# ─── Main ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Code Intelligence Job System")
    sub = parser.add_subparsers(dest='cmd', required=True)

    # submit
    p_submit = sub.add_parser('submit', help='Submit a background job')
    p_submit.add_argument('command', help='Command to run')
    p_submit.add_argument('-l', '--label', help='Human-readable label')

    # status
    p_status = sub.add_parser('status', help='Check job status')
    p_status.add_argument('job_id', type=int, help='Job ID')

    # result
    p_result = sub.add_parser('result', help='Show job output')
    p_result.add_argument('job_id', type=int, help='Job ID')
    p_result.add_argument('--tail', type=int, default=0, help='Show last N lines')

    # list
    p_list = sub.add_parser('list', help='List recent jobs')
    p_list.add_argument('-n', '--limit', type=int, default=10, help='Max jobs to show')

    # cancel
    p_cancel = sub.add_parser('cancel', help='Cancel a running job')
    p_cancel.add_argument('job_id', type=int, help='Job ID')

    # notifications
    sub.add_parser('notify', help='Get pending notifications')

    # clean
    p_clean = sub.add_parser('clean', help='Remove old completed jobs')
    p_clean.add_argument('-k', '--keep', type=int, default=20, help='Keep N most recent')

    args = parser.parse_args()

    if args.cmd == 'submit':
        submit_job(args.command, args.label)
    elif args.cmd == 'status':
        get_job_status(args.job_id)
    elif args.cmd == 'result':
        get_job_result(args.job_id, args.tail)
    elif args.cmd == 'list':
        list_jobs(args.limit)
    elif args.cmd == 'cancel':
        cancel_job(args.job_id)
    elif args.cmd == 'notify':
        get_notifications()
    elif args.cmd == 'clean':
        clean_jobs(args.keep)

if __name__ == "__main__":
    main()
