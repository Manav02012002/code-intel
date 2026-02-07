
import sqlite3, time, os
from datetime import datetime, timezone
DB = "/Users/manavmadanrawal/Dev/code-intel/jobs/jobs.db"
JOB_ID = 1
PID = 90967

# Poll until process exits (can't waitpid on non-child)
while True:
    try:
        os.kill(PID, 0)
        time.sleep(0.5)
    except OSError:
        break
time.sleep(1)  # let stdout/stderr flush to disk

# Read output for notification preview
out_file = "/Users/manavmadanrawal/Dev/code-intel/jobs/artifacts/job_1.out"
preview = ""
try:
    with open(out_file) as f:
        lines = f.readlines()
        preview = "".join(lines[-5:])[:300] if lines else "(no output)"
except:
    preview = "(could not read output)"

# Check for errors
err_file = "/Users/manavmadanrawal/Dev/code-intel/jobs/artifacts/job_1.err"
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
msg = f"Job #{JOB_ID} {status_word}.\n{preview}"
conn.execute("INSERT INTO notifications (job_id, message, created_at) VALUES (?, ?, ?)", (JOB_ID, msg, now))
conn.commit()
conn.close()
