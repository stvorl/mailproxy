import time
import re
import poplib
import imaplib
import mailbox
import os
import json
import yaml
from datetime import datetime, timedelta, timezone

def log(*args):
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    print(ts, *args, flush=True)

ACCOUNTS_FILE = '/app/accounts.yml'
MAIL_BASE = '/var/mail'
CREDS_BASE = '/var/credentials'
SMPH_DIR = '/smph'
FETCH_TRIGGER = os.path.join(SMPH_DIR, 'fetch_now')


def _write_file(path, content):
    """Write content atomically via a temp file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)


def parse_keep_remote(val):
    """
    Parse keep_remote config value.
    Returns: False (delete immediately), True (keep forever),
             or timedelta (keep for that duration, then delete).

    Accepted formats:
      false / 0        — delete from remote immediately after fetching
      true             — keep on remote indefinitely (never delete)
      10m              — keep for 10 minutes (lowercase m)
      12h              — 12 hours
      30s              — 30 seconds
      7d / 7D          — 7 days
      3M               — 3 months (~30 days each, uppercase M)
    """
    if isinstance(val, bool):
        return True if val else False
    if isinstance(val, int):
        return False if val == 0 else timedelta(days=val)
    if isinstance(val, str):
        s = val.strip()
        if s.lower() in ('false', 'no', '0'):
            return False
        if s.lower() in ('true', 'yes'):
            return True
        m = re.fullmatch(r'(\d+)([dDhsmM])', s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            if n == 0:
                return False
            if unit in ('d', 'D'):
                return timedelta(days=n)
            if unit == 'M':
                return timedelta(days=n * 30)
            if unit == 'h':
                return timedelta(hours=n)
            if unit == 'm':
                return timedelta(minutes=n)
            if unit == 's':
                return timedelta(seconds=n)
    log(f"WARNING: unrecognised keep_remote value {val!r}, treating as keep-forever")
    return True


def load_state(path):
    """Load fetch state {id: iso_timestamp} from JSON, or return empty dict."""
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path, state):
    """Persist fetch state atomically."""
    _write_file(path, json.dumps(state, indent=2))


def get_logins(account):
    """
    Return list of (imap_user, password, mailbox) tuples for an account.

    Supported formats for the 'local' section:

    New format — explicit mailbox + one or more logins:
      local:
        mailbox: alice@example.com
        logins:
          - user: alice@example.com
            password: pass1
          - user: alice-work
            password: pass2

    Legacy format — single login, backwards-compatible:
      local:
        user: alice@example.com   # also used as mailbox path
        password: pass

    If 'password' is omitted and 'logins' is absent, no IMAP access is created
    (mailbox still receives mail via fetcher).
    """
    local = account.get('local', {})
    mailbox = local.get('mailbox') or local.get('user')
    if not mailbox:
        return []

    if 'logins' in local:
        result = []
        # If main password is also present, include it as the canonical login
        if 'password' in local:
            login_user = local.get('user', mailbox)
            result.append((login_user, local['password'], mailbox))
        result.extend((l['user'], l['password'], mailbox) for l in local['logins'])
        return result

    if 'password' in local:
        # Legacy: single login whose username equals the mailbox name
        login_user = local.get('user', mailbox)
        return [(login_user, local['password'], mailbox)]

    return []


def write_dovecot_passwd(accounts):
    """
    Write Dovecot passwd-file with home directory per login.
    Format: user:{PLAIN}pass:uid:gid::home:
    """
    lines = []
    canonical_in_file = set()  # mailboxes that already have a direct passdb entry
    alias_mailboxes = set()    # mailboxes only reached via alias logins

    for a in accounts:
        for imap_user, password, mailbox in get_logins(a):
            home = os.path.join(MAIL_BASE, mailbox)
            if imap_user != mailbox:
                # Alias login: override effective username so Dovecot session
                # (and Roundcube From field) uses the canonical mailbox address.
                lines.append(f"{imap_user}:{{PLAIN}}{password}:1000:1000::{home}::user={mailbox}")
                alias_mailboxes.add((mailbox, home))
            else:
                lines.append(f"{imap_user}:{{PLAIN}}{password}:1000:1000::{home}:")
                canonical_in_file.add(mailbox)

    # When all logins are aliases, the canonical mailbox still needs a userdb
    # entry so Dovecot can resolve home after the user= override.
    for mailbox, home in alias_mailboxes:
        if mailbox not in canonical_in_file:
            # '!' prefix disables passdb auth; userdb lookup still works.
            lines.append(f"{mailbox}:!:1000:1000::{home}:")

    _write_file(os.path.join(CREDS_BASE, 'passwd'), '\n'.join(lines) + '\n')


def write_alias_map(accounts):
    """
    Write /var/credentials/alias_map for the Roundcube fix_identity plugin.
    Format: imap-login<TAB>canonical-email<TAB>canonical-password
    Only alias logins (where imap_user != mailbox) are included.
    The canonical password comes from the entry where imap_user == mailbox;
    if absent, aliases are listed without a password (identity fix only, no re-login).
    """
    lines = []
    for a in accounts:
        logins = get_logins(a)
        # Find canonical password: the entry whose login name equals the mailbox.
        canonical_pass = next(
            (pwd for user, pwd, mb in logins if user == mb),
            None
        )
        if canonical_pass is None:
            mailbox = get_mailbox(a)
            log(f"WARNING: {mailbox} has logins but no local.password — "
                  f"Roundcube re-login won't work for alias users")
        for imap_user, _password, mailbox in logins:
            if imap_user != mailbox:
                if canonical_pass:
                    lines.append(f"{imap_user}\t{mailbox}\t{canonical_pass}")
                else:
                    lines.append(f"{imap_user}\t{mailbox}")
    _write_file(os.path.join(CREDS_BASE, 'alias_map'), '\n'.join(lines) + '\n' if lines else '')


def write_postfix_maps(accounts):
    """
    Generate four Postfix lookup tables in CREDS_BASE:
      sender_relay  — local sender address  ->  [relay]:port
      sasl_passwd   — [relay]:port           ->  username:password
      tls_policy    — [relay]:port           ->  encrypt | may
      sender_access — local sender address  ->  OK  (access control)
    """
    sender_relay_lines = []
    sasl_map = {}   # relay -> 'user:pass'
    tls_map = {}    # relay -> 'encrypt' | 'may'
    allowed_senders = []  # senders with a valid outbound relay

    for a in accounts:
        local = a.get('local', {})
        sender = local.get('mailbox') or local.get('user')
        if not sender:
            continue
        ob = a.get('outbound', {})
        host = ob.get('host')
        port = ob.get('port', 587)
        user = ob.get('user')
        passwd = ob.get('pass')
        use_tls = ob.get('tls', True)  # default: require TLS

        if not (host and user and passwd):
            log(f"WARNING: {sender} has no outbound relay — SMTP sending will be rejected")
            continue

        relay = f"[{host}]:{port}"
        sender_relay_lines.append(f"{sender} {relay}")
        sasl_map[relay] = f"{user}:{passwd}"
        tls_map[relay] = 'encrypt' if use_tls else 'may'
        allowed_senders.append(sender)

    _write_file(
        os.path.join(CREDS_BASE, 'sender_relay'),
        ''.join(line + '\n' for line in sender_relay_lines),
    )
    _write_file(
        os.path.join(CREDS_BASE, 'sasl_passwd'),
        ''.join(f"{relay} {creds}\n" for relay, creds in sasl_map.items()),
    )
    _write_file(
        os.path.join(CREDS_BASE, 'tls_policy'),
        ''.join(f"{relay} {policy}\n" for relay, policy in tls_map.items()),
    )
    _write_file(
        os.path.join(CREDS_BASE, 'sender_access'),
        ''.join(f"{s} OK\n" for s in allowed_senders),
    )


def get_fetch_interval(account, default_interval):
    """Return fetch interval in minutes for this account, or the global default."""
    return int(account.get('inbound', {}).get('fetch_interval', default_interval))


def get_mailbox(account):
    """Return the mailbox storage name (directory under MAIL_BASE)."""
    local = account.get('local', {})
    return local.get('mailbox') or local.get('user') or account.get('address')


def ensure_maildir(account):
    mailbox_root = os.path.join(MAIL_BASE, get_mailbox(account))
    os.makedirs(mailbox_root, exist_ok=True)
    path = os.path.join(mailbox_root, 'Maildir')
    mailbox.Maildir(path, create=True)


def fetch_imap(account):
    ib = account['inbound']
    host = ib['host']
    port = ib.get('port', 993 if ib.get('tls') else 143)
    use_ssl = ib.get('tls', False) and port == 993
    keep_for = parse_keep_remote(ib.get('keep_remote', False))
    folders = [f.strip() for f in str(ib.get('folder', 'INBOX')).split(',') if f.strip()]
    label = get_mailbox(account)

    state_file = os.path.join(MAIL_BASE, label, f'.fetch_state_imap_{host}_{port}.json')
    state = load_state(state_file)
    now = datetime.now(timezone.utc)

    ensure_maildir(account)
    md = mailbox.Maildir(os.path.join(MAIL_BASE, label, 'Maildir'))

    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port)
        else:
            conn = imaplib.IMAP4(host, port)
            if ib.get('tls'):
                conn.starttls()

        conn.login(ib['user'], ib['pass'])

        fetched = 0
        deleted = 0
        seen_keys = set()

        for folder in folders:
            conn.select(folder)
            _, data = conn.uid('SEARCH', None, 'ALL')
            uids = data[0].split()

            for uid_bytes in uids:
                uid_str = uid_bytes.decode()
                key = f'{folder}/{uid_str}'
                seen_keys.add(key)

                if key not in state:
                    _, msg_data = conn.uid('FETCH', uid_bytes, '(RFC822)')
                    raw = msg_data[0][1]
                    md.add(raw)
                    fetched += 1
                    if keep_for is False:
                        conn.uid('STORE', uid_bytes, '+FLAGS', '\\Deleted')
                        deleted += 1
                    else:
                        state[key] = now.isoformat()
                        seen_keys.add(key)
                elif isinstance(keep_for, timedelta):
                    fetch_time = datetime.fromisoformat(state[key])
                    if (now - fetch_time) >= keep_for:
                        conn.uid('STORE', uid_bytes, '+FLAGS', '\\Deleted')
                        del state[key]
                        seen_keys.discard(key)
                        deleted += 1

            if keep_for is not True:
                conn.expunge()

        conn.logout()

        state = {k: v for k, v in state.items() if k in seen_keys}
        save_state(state_file, state)

        log(f'IMAP {label}: fetched {fetched}, deleted {deleted} from {host}')

    except Exception as e:
        log(f'ERROR fetching IMAP {label} from {host}: {e}')


def fetch_account(account):
    if 'inbound' not in account:
        return  # local-only account, no remote fetching
    proto = account['inbound'].get('proto', 'pop3').lower()
    if proto == 'imap':
        fetch_imap(account)
    else:
        fetch_pop3(account)


def fetch_pop3(account):
    ib = account['inbound']
    host = ib['host']
    port = ib.get('port', 995 if ib.get('tls') else 110)
    use_ssl = ib.get('tls', False) and port == 995
    keep_for = parse_keep_remote(ib.get('keep_remote', False))
    label = get_mailbox(account)

    state_file = os.path.join(MAIL_BASE, label, f'.fetch_state_pop3_{host}_{port}.json')
    state = load_state(state_file)
    now = datetime.now(timezone.utc)

    ensure_maildir(account)
    md = mailbox.Maildir(os.path.join(MAIL_BASE, label, 'Maildir'))

    try:
        if use_ssl:
            conn = poplib.POP3_SSL(host, port, timeout=30)
        else:
            conn = poplib.POP3(host, port, timeout=30)
            if ib.get('tls'):
                conn.stls()

        conn.user(ib['user'])
        conn.pass_(ib['pass'])

        # UIDL provides stable per-message IDs that persist across sessions.
        _, uidl_list, _ = conn.uidl()
        uidl_map = {}  # msg_num -> uidl string
        for entry in uidl_list:
            parts = entry.decode().split(None, 1)
            uidl_map[int(parts[0])] = parts[1]

        fetched = 0
        deleted = 0
        to_delete = []

        for num, uidl in uidl_map.items():
            if uidl not in state:
                _, lines, _ = conn.retr(num)
                md.add(b'\r\n'.join(lines))
                fetched += 1
                if keep_for is False:
                    to_delete.append((num, uidl))
                else:
                    state[uidl] = now.isoformat()
            elif isinstance(keep_for, timedelta):
                fetch_time = datetime.fromisoformat(state[uidl])
                if (now - fetch_time) >= keep_for:
                    to_delete.append((num, uidl))

        for num, uidl in to_delete:
            conn.dele(num)
            state.pop(uidl, None)
            deleted += 1

        conn.quit()

        # Drop state entries for messages no longer present on the server.
        current_uidls = set(uidl_map.values())
        state = {k: v for k, v in state.items() if k in current_uidls}
        save_state(state_file, state)

        log(f'POP3 {label}: fetched {fetched}, deleted {deleted} from {host}')

    except Exception as e:
        log(f'ERROR fetching POP3 {label} from {host}: {e}')


def main():
    prev_accounts = None
    default_interval = 15
    last_fetched = {}  # mailbox key -> monotonic timestamp
    os.makedirs(SMPH_DIR, exist_ok=True)
    os.chmod(SMPH_DIR, 0o1777)  # world-writable + sticky, like /tmp

    while True:
        try:
            with open(ACCOUNTS_FILE) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            log(f"ERROR reading {ACCOUNTS_FILE}: {e}")
            time.sleep(60)
            continue

        accounts = cfg.get('accounts', [])
        default_interval = cfg.get('global', {}).get('default_fetch_interval', default_interval)

        if accounts != prev_accounts:
            os.makedirs(CREDS_BASE, exist_ok=True)
            write_dovecot_passwd(accounts)
            write_postfix_maps(accounts)
            write_alias_map(accounts)
            prev_accounts = accounts
            log("Config reloaded: updated Dovecot passwd, Postfix maps, alias_map")

        triggered = False
        if os.path.exists(FETCH_TRIGGER):
            try:
                os.remove(FETCH_TRIGGER)
            except OSError:
                pass
            log("Triggered fetch by semaphore")
            triggered = True

        now = time.monotonic()
        for account in accounts:
            key = get_mailbox(account) or account.get('address', '')
            interval_sec = get_fetch_interval(account, default_interval) * 60
            due = triggered or (key not in last_fetched) or (now - last_fetched[key] >= interval_sec)
            if due:
                fetch_account(account)
                last_fetched[key] = time.monotonic()

        time.sleep(2)


if __name__ == '__main__':
    main()
