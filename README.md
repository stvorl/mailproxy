# mailproxy

A self-hosted Docker Compose stack that pulls mail from remote POP3 accounts and exposes it locally via IMAP and SMTP. Designed for households or small teams that need a single always-on mail hub accessible by multiple clients and users. Also useful when you want to consolidate mail from several providers into one place, or when remote mailbox storage is limited and you need to download and delete messages to free space on the server.

## Components

| Service | Image | Role |
|---|---|---|
| **fetcher** | Python 3.11 | Polls POP3 inboxes, delivers to Maildir, generates Dovecot/Postfix credential maps |
| **dovecot** | Debian / Dovecot 2.4 | IMAP server (ports 143 / 993) |
| **postfix** | Debian / Postfix 3.x | SMTP relay with per-sender authentication (port 587) |
| **roundcube** *(optional)* | roundcube/roundcubemail 1.6 | Web UI, enabled via `ENABLE_ROUNDCUBE=true` |

## Quick start

```sh
# 1. Create initial config files and copy accounts.example.yml → accounts.yml
make init
# If migrating from another machine, use `make import FILE=backup.tar.gz`
# instead of steps 1–2 — it restores maildata, rcdata, accounts.yml and .env.

# 2. Fill in credentials
$EDITOR accounts.yml
$EDITOR .env          # set ENABLE_ROUNDCUBE=true to turn on the web UI

# 3. Start
make up

# 4. Follow logs
make logs
```

IMAP is available on **port 143**, SMTP on **127.0.0.1:587**, Roundcube on **http://localhost:8080**.

## accounts.yml

All mail accounts are defined in `accounts.yml`. The fetcher re-reads it on every poll cycle — no restart needed.

### Single login (simple form)

```yaml
global:
  default_fetch_interval: 15   # minutes

accounts:
  - address: alice@example.com

    inbound:                    # fetch from remote POP3
      host: pop.example.com
      port: 995
      proto: pop3
      tls: true
      user: alice@example.com
      pass: remote_password
      keep_remote: false

    outbound:                   # relay outgoing mail via this SMTP server
      host: smtp.example.com
      port: 587
      tls: true
      user: alice@example.com
      pass: smtp_password

    local:                      # IMAP credentials (independent from remote)
      user: alice@example.com
      password: local_password
      fetch_interval: 10        # optional override in minutes
```

### Multiple logins for one mailbox

Several users can share a single mailbox — same Maildir, same address book in Roundcube:

```yaml
    # inbound and outbount sections as above
    local:
      mailbox: bob@example.org   # storage path
      password: default_pass     # required: used for direct login and as
                                 # the canonical credential for alias re-login
      logins:
        - user: bob
          password: bobs_pass
        - user: carol
          password: carols_pass
```

When `logins:` is present, logging in as `bob` or `carol` is transparently redirected to the canonical `bob@example.org` session — both see the same inbox, sent items, and contacts. Direct login under the canonical address (`bob@example.org`) remains available using `local.password`.

**Rules:**
- `local.password` is **required** when `logins:` is used.
- `local.user` (or `local.mailbox`) sets the mailbox storage directory name.
- Per-account `fetch_interval` overrides the global default.

## Makefile targets

| Target | Description |
|---|---|
| `make up` | Build images and start all services |
| `make down` | Stop all services |
| `make restart` | `down` + `up` |
| `make rebuild` | Force rebuild all images without Docker cache (use after base image updates) |
| `make logs` | Tail logs from all services |
| `make export FILE=<filename>` | Stop services, pack `maildata/`, `rcdata/`, `certs/`, `logs/`, `accounts.yml`, `.env` into an archive for migration. |
| `make import FILE=<filename>` | Unpack archive into project directory (does not start services) |
| `make clean` | Stop services and **permanently delete** all private data (`maildata/`, `rcdata/`, `certs/`, `accounts.yml`, `.env`). Requires typing `YES` to confirm. |

## Roundcube

Set `ENABLE_ROUNDCUBE=true` in `.env` to include Roundcube in the stack. Available at **http://localhost:8080**.

Log in with any IMAP username defined in `accounts.yml`. Alias logins (`logins:` entries) are automatically redirected to the canonical account, so all aliases share one address book, drafts folder, and settings.

The Roundcube SQLite database is stored in `./rcdata/` and is included in `make export`.

## Migrating to another machine

```sh
# On the old machine
make export FILE=backup.tar.gz

# Copy to new machine
scp backup.tar.gz newhost:/path/to/mailproxy/
git clone <this-repo> /path/to/mailproxy && cd /path/to/mailproxy

# On the new machine
make import FILE=backup.tar.gz
make up
```

## Directory layout

```
accounts.yml          # credentials — NOT committed (gitignored)
accounts.example.yml  # annotated template
.env                  # ENABLE_ROUNDCUBE flag — NOT committed
docker/
  fetcher/            # Python poller
  dovecot/            # Dovecot config + Dockerfile
  postfix/            # Postfix config + entrypoint
  roundcube/
    plugins/
      fix_identity/   # Plugin: alias re-login + From address fix
maildata/             # Maildirs — NOT committed
rcdata/               # Roundcube SQLite DB — NOT committed
logs/                 # Container logs mounted from host — NOT committed
```

## Security notes

- `accounts.yml` and `.env` contain plaintext credentials. Both are in `.gitignore`. Restrict permissions: `chmod 600 accounts.yml .env`.
- Dovecot and Postfix credential maps are stored in a named Docker volume (`mailproxy_creds`), not in the project directory. They are regenerated automatically on each fetch cycle and do not need to be backed up.
- By default all services listen on `127.0.0.1` only. Set `IMAP_LISTEN`, `SMTP_LISTEN`, `ROUNDCUBE_LISTEN` in `.env` to expose them on the network.
- When exposing services externally, set `IMAP_TLS=3` and `SMTP_TLS=3` to require TLS. A self-signed certificate is auto-generated in `certs/` on first start. Replace with a real certificate if needed.
- The TLS private key `certs/mailproxy.key` is auto-generated by the container on first start and is included in `make export`.
