# mailproxy — IMAP/SMTP proxy stack (prototype)

This repository contains a small Docker Compose prototype that provides:
- Dovecot (IMAP)
- Postfix (SMTP relay, optional per-sender relays)
- A simple Python fetcher that downloads mail via POP3 into Maildir
- Optional Roundcube web UI

Quick start

1. Initialize files:

```sh
make init
# or: sh ./scripts/init.sh
```

2. Edit `.env` and `accounts.yml` with your real credentials.

3. Start the stack:

```sh
make up
```

4. Logs:

```sh
make logs
```

Notes
- Protect `accounts.yml` and `.env` — they contain credentials. These files
  are ignored by `.gitignore`.
- The fetcher periodically reloads `accounts.yml` and updates Dovecot and
  Postfix maps. There is NO global relay fallback: each account must specify
  an `outbound` block with SMTP relay credentials. The fetcher will generate
   per-sender relay maps and SASL passwords for Postfix. After editing
   `accounts.yml` you may need to run `postmap` and `postfix reload` inside the
   `postfix` container to apply new maps, or use the provided `make` targets.
