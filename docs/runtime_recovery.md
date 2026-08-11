# Runtime backup and recovery

This runbook covers repository tooling for backing up and restoring the
application's mutable runtime state. It does not deploy, stop services, change
host configuration, or discover production paths automatically. Every source
and destination must be supplied explicitly.

## What the archive contains

The backup tool accepts two source types:

- SQLite databases, copied with SQLite's online backup API and checked with
  PRAGMA quick_check.
- Named runtime directories, copied file by file and rejected if a file changes
  during its copy or if the tree contains a symlink or special file.

Every regular payload file has its byte size and SHA-256 digest recorded in
manifest.json. Absolute source paths are not written to the manifest. A restore
requires the payload file set to match the manifest exactly, verifies every
digest, and runs PRAGMA quick_check on every declared SQLite copy before the
restore target is changed.

When an explicitly named SQLite database lives inside an explicitly named
runtime directory, its database file and its -wal, -shm, and -journal companions
are omitted from the raw directory copy. The consistent online SQLite copy is
the authoritative backup.

Chroma and similar multi-file stores do not expose a generic cross-file
transaction through this script. The stable-file checks detect changes to an
individual file while it is copied, but cannot prove a transactionally
consistent view across the whole directory. Quiesce writers to that store or
snapshot its storage volume before running the backup. SQLite writers do not
need to be stopped for the separately declared databases.

## Encryption is fail-closed

Creation uses age encryption by default. It stops before reading a source when
the age executable or recipient is unavailable. Supply the recipient with
--age-recipient or RUNTIME_BACKUP_AGE_RECIPIENT:

    python deploy/runtime_backup.py create \
      --sqlite conversations=/srv/rentcompass/runtime/conversations.sqlite3 \
      --sqlite checkpoints=/srv/rentcompass/runtime/checkpoints.sqlite3 \
      --directory chroma=/srv/rentcompass/runtime/chroma_db \
      --directory runtime_files=/srv/rentcompass/runtime/files \
      --output /secure/backups/runtime-YYYYMMDDTHHMMSSZ.tar.gz.age \
      --age-recipient age1REPLACE_WITH_OPERATIONS_RECIPIENT

The paths above are placeholders. Resolve the active deployment's paths and
storage ownership during the change window; do not copy these examples
unchanged.

The recipient is public material, but it should still be managed as deployment
configuration. Keep the corresponding private identity outside the application
repository and backup directory. Restrict the output directory and archive to
the recovery operators.

The --no-encrypt option is deliberately explicit. Use it only in an isolated
test or when an independently encrypted storage layer and its recovery process
have been approved. A successful plaintext backup is mode 0600, but filesystem
permissions are not a replacement for encryption.

## Backup procedure

1. Confirm free space for the staged plaintext plus the final encrypted
   archive. The tool stages data in a mode-0700 directory next to the output so
   publication stays on one filesystem.
2. Quiesce writers for Chroma or other multi-file directories, or take a
   storage-level snapshot and point --directory at that snapshot.
3. Run create with every mutable SQLite database and runtime directory assigned
   a stable, non-sensitive label.
4. Require a zero exit status and retain the JSON success record. A missing age
   prerequisite, changing source file, failed SQLite check, existing output,
   symlink, or special file causes a non-zero exit.
5. Copy the encrypted result to the approved off-host location. Apply the
   retention and immutability policy there.
6. Resume any quiesced writers.
7. Periodically exercise the restore procedure below on isolated storage.

The output path must not already exist and must not be inside a declared
runtime directory. This prevents accidental overwrite and self-inclusion.

## Restore procedure

Restore into a new, empty sibling directory first. The age identity can be
provided with --age-identity or RUNTIME_BACKUP_AGE_IDENTITY:

    python deploy/runtime_backup.py restore \
      --input /secure/backups/runtime-YYYYMMDDTHHMMSSZ.tar.gz.age \
      --target /srv/rentcompass/runtime-restore-staging \
      --age-identity /secure/recovery/identity.txt

The tool decrypts and extracts into a private temporary directory, rejects
absolute paths, parent traversal, links, devices, duplicate members, excessive
member counts, and excessive expanded size, then performs all manifest,
SHA-256, and SQLite checks. It copies into a sibling staging tree and renames
that tree into place only after verification succeeds.

A non-empty target is refused by default. If an operator has explicitly
approved replacement, --overwrite-target installs the verified tree and
preserves the prior target under a sibling name beginning
.TARGET.pre-restore-. The JSON success result reports that path. It is not
deleted automatically.

After a successful isolated restore:

1. Open each restored database read-only and confirm expected high-level row
   counts in addition to the tool's structural quick_check.
2. Start a disposable application instance against only the restored tree and
   run its readiness and representative retrieval checks.
3. Confirm Chroma collection counts and a known retrieval sample.
4. Confirm restored ownership and permissions match the service account and
   current deployment policy. Archive modes are retained, but user and group
   ownership are intentionally not trusted from the archive.
5. Record the backup identifier, restore result, application version, and
   validation evidence.
6. Promote the restored tree only through the normal deployment/change
   procedure. This script does not reconfigure or restart the service.

If verification fails, the requested target is left untouched. Preserve the
failed archive for investigation, but do not retry with verification disabled;
there is no such option.

## Recovery boundaries

- The archive authenticates corruption against its internal SHA-256 manifest;
  encryption with age is what protects confidentiality and tamper resistance.
- Operators must retain and test the age identity independently. Losing the
  identity makes encrypted backups unrecoverable.
- The default extraction limits are 20 GiB and 250,000 members. Use the CLI
  limit options only to set a reviewed bound appropriate for the environment.
- A successful SQLite quick_check verifies structural integrity, not business
  completeness or semantic correctness.
- No command in this runbook should be run against production without the
  normal change approval, storage snapshot, and rollback controls.
