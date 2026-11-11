# Fileshare

Standalone owner-controlled file sharing service for Coolify. Browser users sign in with the owner password. Recipients download through a random bearer URL without an account. There is no public signup or anonymous upload.

## API contract

`POST /api/uploads` accepts multipart form data with one field named `file`. Supply `Authorization: Bearer <upload-token>`. An optional `X-Content-SHA256` header can contain the local file's lowercase or uppercase SHA-256. The service hashes the bytes it writes to persistent storage, rejects a mismatched client hash, and only publishes the share after the complete file is stored.

Success returns HTTP 201 with JSON: `id`, `url`, `filename`, `size` (bytes), and server-calculated `sha256`. Download the `url` using a plain GET. It returns `application/octet-stream`, `Content-Disposition: attachment`, and `X-Content-Type-Options: nosniff`.

`DELETE /api/shares/<id>` with the same bearer credential revokes a share and returns 204. Errors are JSON with `error`: `unauthorized` (401), `validation` (400 or 422), `size_limit` (413), `not_found` (404), or an HTTP 5xx for temporary server failures. The client should not retry a 201 response. If a connection ends before a response, it may have completed; retrying creates another share, so investigate or use the owner dashboard to remove duplicates.

## Configuration

Required environment variables (store in Coolify, never in Git):

- `PUBLIC_BASE_URL`: HTTPS origin, without a trailing slash.
- `SESSION_SECRET`: at least 32 random bytes encoded as text.
- `OWNER_PASSWORD_HASH`: Werkzeug password hash from `generate_password_hash(password)`.
- `UPLOAD_TOKEN_SHA256`: lowercase SHA-256 hex digest of the revocable upload token.
- `DATA_DIR`: persistent mount path, normally `/data`.
- `MAX_UPLOAD_BYTES`: file limit in bytes, default 262144000 (250 MiB).

The service stores files and the SQLite database under `DATA_DIR`. Use one Gunicorn worker; threads handle concurrent requests. Back up the entire directory consistently, including the database and WAL files, to storage outside the VPS. Test a restore into an isolated copy before relying on it. Persistent mounts alone are not backups.

In Coolify, set `OWNER_PASSWORD_HASH` to **Literal** interpolation so its `$` characters are preserved. Mark `OWNER_PASSWORD_HASH`, `SESSION_SECRET`, and `UPLOAD_TOKEN_SHA256` as locked secrets. This deployment uses `https://beta.getseymour.com` and a persistent Docker volume at `/data`. Locally generated credentials are in `~/.config/coolify-fileshare/credentials.json` with mode 0600; that file is outside this repository.

On the owner's Mac, `python3 scripts/backup_and_test.py` pauses only the fileshare container while it streams a consistent volume archive into the local ignored `backups/` directory. It then extracts the archive into a temporary directory and verifies every active share against its database size and SHA-256. SSH access to the VPS is required. The off-VPS archive is stored on this Mac; retain separate copies if the Mac's disk is not an adequate disaster-recovery destination.

A macOS LaunchAgent at `~/Library/LaunchAgents/com.getseymour.fileshare.backup.plist` runs that script daily at 03:15 local time while the owner is logged in and the Mac is available. Its logs are in `~/Library/Logs/fileshare-backup*.log`. It does not prune archives automatically; monitor available disk space. The LaunchAgent was kicked off once and its restore check passed.

## Local verification

Create a virtual environment, install `requirements.txt`, set the required variables, then run `python -m unittest discover -s tests -v`. The Docker image runs on port 8080 and exposes `/healthz`.

For a deployed check, run `python3 scripts/verify_live.py`. It writes the original and downloaded ZIP, SHA-256 evidence, and the active test share URL under ignored `proof/`. It also tests anonymous upload rejection, authenticated API upload, and share revocation.

## Candidate selection

Chibisafe was checked at commit `a857345c3d9cf1b5182eccce79564e353d4ea793` (November 11, 2026). It supports private uploads and API keys, but its upload response lacks file size and SHA-256. Its stored hash is BLAKE3. This service implements the required server-verified SHA-256 contract directly.
