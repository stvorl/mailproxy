# mailproxy — mail proxy server

> [Russian version](README.ru.md)

A Docker Compose deployment kit for fetching mail from remote servers (POP3, IMAP), caching it locally, and serving it to internal users via IMAP and SMTP.

## Use cases

A small enterprise, team, or home user has one or more mailboxes hosted on public or paid services and needs to give access to one or more internal users, with some of the following goals:

- add or remove individual users without touching the real mailbox;
- avoid sharing the real mailbox password with end users;
- have a single point of control for all mail accounts;
- have a single point for filtering / sorting (manually, or by attaching an external IMAP client);
- consolidate mail storage in one place for regular backups;
- provide mail access when internet connectivity is restricted or isolated;
- cache messages locally but delete them from the remote server — immediately or after a configured retention period — to save space at the provider.

Configuration is file-based (no GUI). You define the external mailboxes (addresses, servers, credentials) and the local users who get access to them.

Each mailbox can have one or more local users.

```
user 1 --\
user 2 --+---  mailbox 1 (mailproxy) --- mailbox 1 (gmail.com)
user 3 --/

user 4 --\
user 5 --+---  mailbox 2 (mailproxy) --- mailbox 2 (yourcompany.com)
```

External mail clients (Outlook, Thunderbird, etc.) connect using the local usernames and passwords. A web client (Roundcube) can optionally be deployed.

The proxy regularly polls the real mail servers, stores messages in local Maildir copies, and serves them to clients via IMAP. It also accepts outbound mail and forwards it through the real SMTP servers.

## Components

| Service | Image | Role |
|---|---|---|
| **fetcher** | Python 3.11 | Polls POP3/IMAP, delivers to Maildir, generates credential maps for Dovecot and Postfix |
| **dovecot** | Debian / Dovecot 2.4 | IMAP server (ports 143 / 993) |
| **postfix** | Debian / Postfix 3.x | SMTP relay with per-sender authentication (port 587) |
| **roundcube** *(optional)* | roundcube/roundcubemail 1.6 | Web UI, enabled via `ENABLE_ROUNDCUBE=true` in `.env` |

## Installation

```sh
# 0. Install prerequisites
apt install git docker.io docker-compose-plugin

# 1. Clone the repository (into /opt/mailproxy)
cd /opt
git clone https://github.com/stvorl/mailproxy.git
cd mailproxy
chmod +x mpcontrol

# 2. Choose: new deployment (2A) or restore from backup (2B)

# 2A. Create config files from templates
./mpcontrol init

# Fill in credentials and settings as indicated by the comments in each file.

nano accounts.yml
# In short: add mailbox entries with remote server details (host, login,
# password, retention period, poll interval). Add local users.

nano .env
# In short: enable Roundcube if needed; set which ports IMAP, SMTP, and
# Roundcube should listen on, and whether they should be network-accessible.

# 2B. Restoring from another machine — use ./mpcontrol import <backup file>:
# it restores logs, certs, maildata, rcdata, accounts.yml, .env
./mpcontrol import backup.tgz   # your backup file

# 3. Start
./mpcontrol up

# 4. Verify
# 4.1 Watch logs:
./mpcontrol logs

# 4.2 Check that containers started and opened the expected ports:
docker compose ps

# 4.3 Open Roundcube in a browser and/or configure a mail client
#     (Thunderbird, Outlook, etc.) to test local user access.
```

## Project structure

```
.env                  # service configuration
mpcontrol             # management script
accounts.yml          # credentials — NOT committed (.gitignore)
accounts.example.yml  # annotated template
certs/                # TLS certificates for IMAP and SMTP
docker/
  fetcher/            # mail fetcher service
  dovecot/            # Dovecot service
  postfix/            # Postfix service
  roundcube/          # Roundcube service
    plugins/
      fix_identity/   # plugin: alias redirect + From address fix
logs/                 # container logs on host — NOT committed
maildata/             # Maildirs — NOT committed
rcdata/               # Roundcube SQLite database — NOT committed
```

## accounts.yml

All proxied mail accounts are described in `accounts.yml`. The fetcher re-reads the file on every poll cycle — no container restart is needed.

### Single login (simple form)

```yaml
global:
  default_fetch_interval: 15   # minutes

accounts:
  - address: alice@example.com

    inbound:                     # fetch mail from remote POP3
      host: pop.example.com
      port: 995
      proto: pop3
      tls: true
      user: alice@example.com
      pass: remote_password
      keep_remote: false         # false — delete immediately; true — keep forever; 7d — 7 days

    outbound:                    # SMTP relay for sending
      host: smtp.example.com
      port: 587
      tls: true
      user: alice@example.com
      pass: smtp_password

    local:                       # IMAP credentials (independent from remote)
      mailbox: alice@example.com # local IMAP/SMTP login
      password: local_password   # local IMAP/SMTP password
```

`local.mailbox` and `local.password` are required.

### Multiple logins for one mailbox

Several users can share a single mailbox — the same inbox, folders, and Roundcube address book — each with their own login and password, individually revocable.

```yaml
    # inbound and outbound sections as above
    local:
      mailbox: bob@example.org
      password: default_pass
      logins:
        - user: bob
          password: bobs_pass
        - user: carol
          password: carols_pass
```

`local.mailbox` and `local.password` are required here as well.

When `logins:` is present, signing in as `bob` or `carol` is automatically redirected to the canonical account `bob@example.org` — both see the same folders, messages, and contacts. The master password (`local.password: default_pass`) continues to grant direct access under the canonical address but should not be shared with end users. Individual users can be removed or new ones added at any time.

### keep_remote — how long to keep messages on the remote server

| Value | Behaviour |
|---|---|
| `false` or `0` | Delete from remote immediately after fetching (default) |
| `true` | Keep on remote forever |
| `10m` | Delete after 10 minutes |
| `12h` | Delete after 12 hours |
| `7d` or `7D` | Delete after 7 days |
| `3M` | Delete after 3 months (~90 days) |
| `30s` | Delete after 30 seconds |

Messages are downloaded **only once** regardless of `keep_remote`. Fetch state is tracked in `.fetch_state_*.json` files in the mailbox directory.

### fetch_depth — limit how far back IMAP fetcher searches (IMAP only)

When `keep_remote: true` and the remote mailbox holds thousands of old messages, every poll cycle asks the server to list them all. `fetch_depth` tells the fetcher to issue `SEARCH SINCE <date>` instead of `SEARCH ALL`, drastically reducing the UID list returned.

```yaml
    inbound:
      proto: imap
      keep_remote: true
      fetch_depth: 30d   # only consider messages from the last 30 days
```

Accepts the same suffixes as `keep_remote`: `30d`, `3M`, `12h`, etc. Default: unlimited.

**Interaction with `keep_remote` timedelta:** if `keep_remote` is also a duration, the effective search depth is automatically extended to `keep_remote + 10 × fetch_interval`. This ensures messages near their expiry deadline remain visible to the fetcher long enough to be deleted. A log line is emitted when the adjustment occurs.

**Interaction with `keep_remote: false`:** only messages within `fetch_depth` are fetched, stored, and deleted from the remote server. Messages older than `fetch_depth` are neither fetched nor deleted.

`fetch_depth` has no effect for POP3 — the protocol always returns a full message list, which may slow down new mail retrieval on mailboxes with a large number of undeleted messages. In this case, prefer IMAP fetching and configure `fetch_depth`.

### IMAP source

If the provider does not support POP3, or you need to fetch from a specific folder, use `proto: imap`:

```yaml
    inbound:
      host: imap.example.com
      port: 993
      proto: imap
      tls: true
      folder: INBOX               # optional; comma-separated: "INBOX, Spam"
      user: charlie@example.com
      pass: remote_password
      keep_remote: 5d             # delete after 5 days
```

## Configuration (.env)

`.env` controls how the services are exposed on the network.

### Roundcube

An optional web mail client that sits on top of the IMAP/SMTP stack. When using it exclusively, external IMAP/SMTP access can be disabled to prevent third-party mail clients from connecting.

Set `ENABLE_ROUNDCUBE=true` in `.env` to include Roundcube in the stack.
Roundcube is available by default at **http://\<hostname\>:8080**.

```sh
ENABLE_ROUNDCUBE=true     # uncomment to enable the web interface

ROUNDCUBE_LISTEN=0.0.0.0  # bind address (0.0.0.0 = all interfaces, 127.0.0.1 = localhost only)
ROUNDCUBE_PORT=8080       # HTTP port
ROUNDCUBE_TITLE=Roundcube # custom browser/tab title (optional)
```

### IMAP server (Dovecot)

Two ports: **143** (plain/STARTTLS) and **993** (IMAPS). Bind address and TLS level are configured independently.

| `IMAP_TLS` | Behaviour |
|---|---|
| `1` | Plain, no TLS offered |
| `2` | STARTTLS available (on client request) |
| `3` | STARTTLS required + port 993 (IMAPS) enabled |

Recommended combinations:

| Scenario | `IMAP_LISTEN` | `IMAPS_LISTEN` | `IMAP_TLS` |
|---|---|---|---|
| Local only (e.g. for Roundcube) | `127.0.0.1` | `127.0.0.1` | `1` |
| LAN, STARTTLS + IMAPS | `0.0.0.0` | `0.0.0.0` | `2` |
| LAN or internet, IMAPS only | `127.0.0.1` | `0.0.0.0` | `3` |

```sh
IMAP_LISTEN=127.0.0.1
IMAP_PORT=143
IMAPS_LISTEN=0.0.0.0
IMAPS_PORT=993
IMAP_TLS=3
```

### SMTP server (Postfix)

Outbound mail proxy that forwards messages to the real SMTP servers.

Single port **587** (submission). Same parameters:

| `SMTP_TLS` | Behaviour |
|---|---|
| `1` | Plain, no TLS |
| `2` | STARTTLS available |
| `3` | STARTTLS required |

```sh
SMTP_LISTEN=0.0.0.0
SMTP_PORT=587
SMTP_TLS=3
```

## Management

All management goes through the `mpcontrol` script.

| Command | Description |
|---|---|
| `./mpcontrol init` | Create working configs from templates, prepare for first run |
| `./mpcontrol up` | Build images and start all services |
| `./mpcontrol down` | Stop all services |
| `./mpcontrol restart` | `down` + `up` |
| `./mpcontrol rebuild` | Force-rebuild images without Docker cache (use after base image updates) |
| `./mpcontrol logs` | Tail logs from all services |
| `./mpcontrol export <file>` | Stop services and pack `maildata/`, `rcdata/`, `certs/`, `logs/`, `accounts.yml`, `.env` into an archive |
| `./mpcontrol import <file>` | Unpack archive into project directory (does not start services) |
| `./mpcontrol clean` | Stop services and **permanently delete** all data (`maildata/`, `rcdata/`, `certs/`, `accounts.yml`, `.env`). Requires typing `YES` to confirm. |

## Security

- All services listen on `127.0.0.1` by default. Set `IMAP_LISTEN`, `IMAPS_LISTEN`, `SMTP_LISTEN`, `ROUNDCUBE_LISTEN` in `.env` to expose them on the network.
- When exposing services externally, set `IMAP_TLS=3` and `SMTP_TLS=3` to enforce TLS. A self-signed certificate is generated automatically in `certs/` on first start. Replace it with a real certificate if needed.
- For backups, use `./mpcontrol export` or copy the directories `maildata/`, `rcdata/`, `logs/`, `certs/` and the files `.env`, `accounts.yml` — e.g. with borg, restic, etc. Everything else can be restored via `git clone`.

## Migration

To move mail archive, configured accounts, logs, certificates, and server settings to another machine:

### On the source machine:

```sh
./mpcontrol export backup.tgz
# copy the file to the target machine
```

### On the target machine

Follow steps 0–1, 2B, and 4 from the **Installation** section above.

## Upgrading

```sh
# 1. Save a backup (just in case)
./mpcontrol export backup-before-upgrade.tgz

# 2. Stop services
./mpcontrol down

# 3. Pull updates from the repository
git pull

# 4. Rebuild images without cache
./mpcontrol rebuild

# 5. Start
./mpcontrol up
```

After upgrading, check logs for errors: `./mpcontrol logs`.

## Performance

The directories `maildata/`, `rcdata/`, and `logs/` contain the performance-critical data. If needed, they can be moved to dedicated physical or logical volumes, with symlinks left in place.
