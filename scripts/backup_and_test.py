"""Copy the Coolify volume off the VPS and verify a restored SQLite/file snapshot.

Requires SSH access to the VPS. The app container is paused only while the archive
is streamed, keeping SQLite and files in one consistent snapshot.
"""

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
metadata = json.loads((Path.home() / ".config/coolify-fileshare/deployment.json").read_text())
app_uuid = metadata["coolify_app_uuid"]
if not re.fullmatch(r"[a-zA-Z0-9]+", app_uuid):
    raise SystemExit("Invalid application UUID")
host = os.environ.get("FILESHARE_SSH_HOST", "root@65.108.49.28")
volume = app_uuid + "-fileshare-data"
backup_dir = ROOT / "backups"
backup_dir.mkdir(exist_ok=True)
archive = backup_dir / ("fileshare-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".tar.gz")
remote = f"""set -eu
container=$(docker ps --filter label=coolify.applicationUuid={app_uuid} --format '{{{{.Names}}}}' | head -1)
test -n "$container"
mount=$(docker volume inspect {volume} --format '{{{{.Mountpoint}}}}')
docker pause "$container" >/dev/null
trap 'docker unpause "$container" >/dev/null' EXIT
tar -C "$mount" -czf - .
"""

with archive.open("wb") as output:
    run = subprocess.run(["ssh", "-o", "BatchMode=yes", host, remote], stdout=output, stderr=subprocess.PIPE)
if run.returncode:
    archive.unlink(missing_ok=True)
    raise SystemExit("Backup transfer failed: " + run.stderr.decode(errors="replace")[-500:])

with tempfile.TemporaryDirectory() as restore_dir:
    restore = Path(restore_dir)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(restore, filter="data")
    database = restore / "shares.sqlite3"
    assert database.is_file(), "Restored database is missing"
    with sqlite3.connect(database) as conn:
        rows = conn.execute("SELECT id, size, sha256 FROM shares WHERE active=1").fetchall()
        for share_id, size, expected_hash in rows:
            file_path = restore / "files" / share_id
            assert file_path.is_file(), f"Restored file {share_id} is missing"
            actual = file_path.read_bytes()
            assert len(actual) == size, f"Restored file {share_id} has wrong size"
            assert hashlib.sha256(actual).hexdigest() == expected_hash, f"Restored file {share_id} has wrong hash"

print(json.dumps({"backup": str(archive), "archive_bytes": archive.stat().st_size,
                  "restored_active_shares": len(rows), "restore_verified": True}, indent=2))
