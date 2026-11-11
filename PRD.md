PRD: Standalone file sharing service

**Status:** Proposed for a separate project

**Updated:** November 11, 2026

## 1. Goal

The owner uploads any file in a browser and receives a URL to send to recipients. Recipients open the URL and download the file without an account. The immediate use case is sharing Fusion Parameter Studio ZIP builds with about five beta testers because Gmail may reject ZIP attachments containing code files.

The service belongs in its **own project** and runs on the existing Coolify-managed VPS. Fusion Parameter Studio will use its upload integration, but the service must be useful for unrelated files too. Do not build tester management, release management, licensing, or an email-sending system into the service.

## 2. User workflow

1. The owner signs in to the file-sharing site.
2. The owner selects a file and uploads it.
3. The site displays a copyable HTTPS share URL.
4. The owner sends that URL in a normal email or message.
5. Anyone with the URL can download the exact file. The owner can later remove the share or file.

The site must allow a ZIP containing legitimate `.py`, `.js`, and other code files. It should accept ordinary files of other types within a configurable size limit.

## 3. Access model

- Only the owner or explicitly authorized uploader accounts can upload, list, manage, or delete files. Public signup and anonymous upload are disabled.
- A share URL is an opaque bearer link. A recipient needs no account, but anyone they forward it to can also use it. Use a cryptographically random identifier with at least 128 bits of entropy; do not expose an index of other files.
- Deleting or disabling a share prevents new downloads through its URL. A previously downloaded copy cannot be recalled.
- Serve files over HTTPS. Treat uploaded files as untrusted content and return safe download headers rather than executing or rendering active content in the app's origin.
- No per-recipient access records, personalized ZIPs, passwordless sign-in, or automated notification emails are required.

This is link-based sharing, not identity-verified private storage. Files requiring recipient identity checks need a separate access policy.

## 4. Standalone project responsibilities

The new project owns the running service, its deployment configuration, documentation, backups, and maintenance. An existing self-hosted uploader is preferable to writing a custom portal. **Chibisafe** is the first candidate because it supports browser uploads and shareable links and appears in Coolify's service catalog. Before committing to it, verify that its current version supports owner-only uploads, recipient downloads by link, link removal, safe handling of arbitrary files, and a noninteractive authenticated upload path. If it cannot meet those checks, select or build a small alternative in the standalone project.

The service must provide a stable integration contract, directly or through a thin adapter:

- An authenticated machine upload accepts a streamed file, its filename, and an optional client SHA-256.
- A successful response returns a share URL, file size, and server-calculated SHA-256. The service verifies the bytes it stored; it must not merely echo a client checksum.
- An incomplete or rejected upload does not create a downloadable partial file.
- Authentication uses a revocable upload credential stored outside source control. An upload credential does not grant anonymous users upload access.
- Errors are explicit enough for a command-line client to distinguish authentication, size-limit, validation, and temporary server failures.

The exact HTTP route and wire format can follow the selected product's API. Document them in the standalone project before implementing the client in Fusion Parameter Studio. The service need not send email or know anything about Fusion.

## 5. Fusion Parameter Studio integration

This repository owns only its build-side client. Add an **`upload` verb to the root `build.py`** command in a later implementation task. The existing `build` verb, `npm run build`, and local ZIP format continue to work unchanged.

Expected behavior of `python build.py upload`:

1. Run the existing build flow to create a **fresh** ZIP from the current checkout. Do not upload a stale file left in `dist/`.
2. Apply the existing ZIP privacy/content gate and version checks. No tester email, account ID, upload token, service URL, or other private data is inserted into the ZIP.
3. Stream the new ZIP to the configured standalone service over HTTPS using an upload credential from the local environment or another external secret store. Never commit the credential, print it, or include it in release evidence.
4. Compare the returned server SHA-256 and size with the local file. Report success only on an exact match, then print the share URL clearly for the owner to copy. On a mismatch, attempt to remove the uploaded share and report the failure.
5. Exit nonzero on build, connection, authentication, upload, or checksum failure. Keep the local ZIP for diagnosis but do not print a success URL. Ensure logs do not expose the credential.

The upload verb sends network traffic **only from the developer's build command**. The installed Fusion add-in remains fully offline and makes no requests to the file-sharing service. The command does not email testers automatically. Configuration names and retry/idempotency behavior should be finalized when the service's actual API is selected.

## 6. Deployment and operations

- Deploy the standalone service at an owner-selected HTTPS subdomain on the existing VPS without disrupting other Coolify apps. `beta.getseymour.com` is a proposed address, not a required one.
- Keep file storage and any service database/configuration in persistent mounts. Back them up off the VPS and test restore. Persistent volumes alone are not backups.
- Set a practical upload size limit in the service and reverse proxy. Verify it with an actual add-in ZIP.
- Change default credentials before exposing the service. Pin and update deployed images deliberately.
- Do not alter production DNS, deploy the service, or send real links as part of this PRD task.

## 7. Acceptance checks

1. The owner uploads an ordinary file in a browser, copies its URL, and downloads identical bytes in a private browser session.
2. The owner uploads a valid Fusion Parameter Studio ZIP containing its normal code files; the service accepts it without changing its bytes.
3. An anonymous visitor cannot upload or list files, but can download a file with its share URL.
4. A removed share no longer permits a new download.
5. A redeployment preserves files and account/configuration data; a tested backup restores them.
6. Once integrated, `python build.py upload` builds a fresh ZIP, uploads it, verifies the returned size and SHA-256, and prints a share URL. Negative tests cover a rejected upload, wrong checksum, missing credential, and interrupted connection.
7. The add-in ZIP privacy gate passes, and the installed add-in's offline behavior is unchanged.

## References

- [Chibisafe capabilities](https://github.com/chibisafe/chibisafe)
- [Chibisafe in Coolify](https://coolify.io/docs/services/chibisafe)
- [Coolify persistent storage](https://coolify.io/docs/core/persistent-storage/storage-mounts/overview)
- [Gmail blocked attachment types](https://support.google.com/mail/answer/6590)
