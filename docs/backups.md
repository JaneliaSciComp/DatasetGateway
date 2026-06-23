---
doc_status: living
sync_policy: Update when backup_db/restore_db behavior, knobs, or the runbook change.
last_reviewed: 2026-06-23
---

# Backups & Restore

DatasetGateway stores all authorization state in a single SQLite DB
(`DATABASE_PATH`, default `db.sqlite3`). That DB holds **live bearer tokens**
(`APIKey.key`, `ServiceAccountToken.key`) and user PII, so a single host is a
single point of loss and the backups must be encrypted at rest.

`manage.py backup_db` takes a consistent snapshot, bundles everything needed to
reconstruct DSG on a new host, encrypts the whole bundle to **offline admin
public keys**, ships it to nearline storage, and prunes old bundles on a tiered
(GFS) schedule. `manage.py restore_db` reverses it. A systemd timer runs the
backup hourly.

> The server holds only **public** keys. A host compromise lets an attacker read
> the live DB, but it cannot decrypt past backups — those need an admin private
> key kept off the host.

## What is `age`?

[`age`](https://age-encryption.org) ("actually good encryption") is the
command-line file-encryption tool used to lock each backup. The name is just the
tool's name — it has nothing to do with users' ages or any other data; it is
purely the encryption machinery wrapped around the bundle. It is a modern,
deliberately-simple alternative to GPG, chosen in this slice (you can still swap
in GPG via `DSG_BACKUP_ENCRYPT_CMD` — see below).

It uses **public-key (asymmetric)** encryption, so two kinds of key exist:

- a **public key** (looks like `age1ql3z7...`) — safe to share; used only to
  *encrypt*. These are what the server holds, in the recipients file.
- a **private/secret key** (looks like `AGE-SECRET-KEY-1...`) — kept off the
  server by each admin; the only thing that can *decrypt*.

`age-keygen` creates a matched pair. The server encrypts to every admin's public
key (`age -R <recipients-file>`) and can never decrypt; an admin later decrypts a
bundle with their private key (`age -d -i <identity-file>`). That asymmetry is
why a compromised host cannot read past backups.

## What a bundle contains

Each backup produces two files in the nearline dir (`DSG_BACKUP_DIR`):

| File | Encrypted? | Contents |
|---|---|---|
| `dsg-backup-<UTC>.tar.gz.age` | yes | the bundle (see below) |
| `dsg-backup-<UTC>.meta.json`  | no  | ops sidecar: timestamp, git SHA/branch, Django/Python versions, encrypted artifact sha256 + size. **No secrets.** |

Inside the encrypted `.tar.gz`:

- `db.sqlite3` — the consistent snapshot (`VACUUM INTO`, then `PRAGMA integrity_check`).
- `manifest.json` — git commit SHA + branch, Django/Python versions, applied
  migrations, per-file sha256, UTC timestamp.
- `dotenv` + `secrets/client_credentials.json` — only when `DSG_BACKUP_INCLUDE_SECRETS=true`.

The artifact name always ends in `.age` when encrypted, even if you plug in a
non-age encryptor via `DSG_BACKUP_ENCRYPT_CMD` — the suffix is the "this is
encrypted" marker that `restore_db` and the pruner key off.

## Configuration

All knobs are read from the environment (see `.env.example`); defaults live in
`dsg/settings.py`.

| Variable | Default | Meaning |
|---|---|---|
| `DSG_BACKUP_STAGING_DIR` | `/var/tmp/dsg-backups` | fast local NVMe scratch; all heavy work happens here |
| `DSG_BACKUP_DIR` | `/shared/flyem/dsg` | slow nearline target; receives only the finished, verified artifact |
| `DSG_BACKUP_KEEP_HOURLY` | `24` | keep newest bundle in each of the 24 most-recent hours |
| `DSG_BACKUP_KEEP_DAILY` | `14` | …each of the 14 most-recent days |
| `DSG_BACKUP_KEEP_WEEKLY` | `8` | …each of the 8 most-recent ISO weeks |
| `DSG_BACKUP_INCLUDE_SECRETS` | `true` | also bundle `.env` + `secrets/client_credentials.json` |
| `DSG_BACKUP_AGE_RECIPIENTS_FILE` | `/etc/dsg/age-recipients.txt` | admin **public** keys for `age -R` |
| `DSG_BACKUP_AGE_IDENTITY_FILE` | _(unset)_ | restore-side **private** key for `age -d -i` |
| `DSG_BACKUP_ENCRYPT_CMD` | _(unset)_ | override encryptor; a `stdin→stdout` shell command |
| `DSG_BACKUP_DECRYPT_CMD` | _(unset)_ | override decryptor; a `stdin→stdout` shell command |

"Most-recent N buckets" counts only buckets that actually contain a backup
(restic/borg semantics), so a missed run or an outage never causes the surviving
backups to be pruned.

**Encryption is mandatory for secrets.** If `DSG_BACKUP_INCLUDE_SECRETS=true` and
neither `DSG_BACKUP_AGE_RECIPIENTS_FILE` nor `DSG_BACKUP_ENCRYPT_CMD` is
configured, `backup_db` exits non-zero and writes nothing.

## One-time key setup (age)

`age` must be on PATH on the DSG host (deploy prerequisite). Each admin who
should be able to restore generates a keypair **on their own machine** — the
private key never touches the server:

```bash
age-keygen -o ~/dsg-backup-admin.key      # prints the public key on stderr
# Public key looks like: age1q...   (this is what the server needs)
```

Collect every admin's **public** key into the recipients file on the DSG host:

```bash
sudo mkdir -p /etc/dsg
sudo tee /etc/dsg/age-recipients.txt >/dev/null <<'EOF'
# one age public key per line (comments allowed)
age1qexamplepublickeyforadmin1...
age1qexamplepublickeyforadmin2...
EOF
sudo chmod 644 /etc/dsg/age-recipients.txt
```

Any one of those private keys can later decrypt a bundle. Store the private keys
in your team's secret manager / hardware token; **do not** put them on the DSG
host. To rotate, edit the recipients file — it only affects future backups.

### GPG instead of age

If you must use GPG, set the override commands (and leave the age recipients file
unset):

```bash
DSG_BACKUP_ENCRYPT_CMD="gpg --batch --yes --encrypt --recipient admin@example.org"
DSG_BACKUP_DECRYPT_CMD="gpg --batch --yes --decrypt"
```

## Running a backup manually

```bash
pixi run backup                 # uses the configured staging/nearline dirs
pixi run backup --no-prune      # skip the retention prune this run
pixi run backup --backup-dir /tmp/test-nearline --staging-dir /tmp/test-staging
```

Exit code is non-zero on any failure (bad snapshot, missing encryption, transfer
mismatch), which is what systemd/`journalctl` surface.

## Restore runbook (tested)

Restore is a two-phase operation: **verify** (default, read-only) then **apply**.

1. Copy the bundle somewhere local and confirm your private key decrypts it.
   Point DSG at your identity (or use the `DSG_BACKUP_DECRYPT_CMD` override):

   ```bash
   export DSG_BACKUP_AGE_IDENTITY_FILE=~/dsg-backup-admin.key
   ```

2. **Verify** — decrypts, extracts, recomputes every file's sha256 against the
   manifest, and prints the git SHA the snapshot was taken at. Writes nothing:

   ```bash
   pixi run restore /path/to/dsg-backup-<UTC>.tar.gz.age
   ```

3. **Check out matching code.** The schema in the snapshot matches the printed
   git SHA. On a fresh host:

   ```bash
   git checkout <sha-from-step-2>
   pixi install
   ```

4. **Apply** — drop the snapshot into the DB. Add `--restore-secrets` to also
   restore `.env` + `secrets/`. Use `--database-path` to restore into a scratch
   location first if you want to inspect before going live:

   ```bash
   # dry-run target, then verify it loads:
   pixi run restore <bundle> --apply --database-path /tmp/restored.sqlite3
   DATABASE_PATH=/tmp/restored.sqlite3 pixi run python manage.py check

   # the real thing (default DATABASE_PATH), including secrets:
   pixi run restore <bundle> --apply --restore-secrets
   ```

5. **Finish:**

   ```bash
   pixi run python manage.py migrate     # should be a no-op (schema matches)
   sudo systemctl restart datasetgateway
   ```

   Then load `/admin/` and confirm data is present.

## Scheduling (systemd)

Two units live in `dsg/scripts/`:

- `datasetgateway-backup.service` — `Type=oneshot`, runs `pixi run backup`.
- `datasetgateway-backup.timer` — fires hourly (`OnCalendar=hourly`,
  `Persistent=true`).

Install (as root, editing the `<...>` placeholders to match
`datasetgateway.service`):

```bash
cp scripts/datasetgateway-backup.service /etc/systemd/system/
cp scripts/datasetgateway-backup.timer   /etc/systemd/system/
# edit User=, WorkingDirectory=, EnvironmentFile=, and the pixi path
systemctl daemon-reload
systemctl enable --now datasetgateway-backup.timer
systemctl start datasetgateway-backup.service     # one manual run to verify
journalctl -u datasetgateway-backup -f            # watch logs / failures
systemctl list-timers datasetgateway-backup.timer # confirm next fire time
```

A failed run exits non-zero; `systemctl status datasetgateway-backup` and
`journalctl -u datasetgateway-backup` show it. There is intentionally no
in-process scheduler: gunicorn runs multiple workers, so an in-app thread would
double-fire or need leader election — systemd is the single external trigger.

## How consistency & integrity are guaranteed

- **Consistent snapshot:** `VACUUM INTO` runs in its own connection and is
  transactionally consistent against concurrent gunicorn readers/writers — no
  torn, mid-write file (unlike a naive `cp`).
- **Snapshot verified:** `PRAGMA integrity_check` must return `ok`, or the run
  aborts with no "successful" backup.
- **Transfer verified:** the artifact's sha256 is re-computed on nearline after
  the copy; a mismatch deletes the bad nearline copy, keeps the local staging
  copy, and fails loudly.
- **Prune is last:** old bundles are removed only after a new backup is verified
  on nearline.
