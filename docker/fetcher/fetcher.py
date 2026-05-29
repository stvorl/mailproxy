import time
import poplib
import mailbox
import os
import yaml

ACCOUNTS_FILE = '/app/accounts.yml'
MAIL_BASE = '/var/mail'
CREDS_BASE = '/var/credentials'


def _write_file(path, content):
    """Write content atomically via a temp file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)


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
    Format: imap-login<TAB>canonical-email
    Only alias logins (where imap_user != mailbox) are included.
    """
    lines = []
    for a in accounts:
        for imap_user, _password, mailbox in get_logins(a):
            if imap_user != mailbox:
                lines.append(f"{imap_user}\t{mailbox}")
    _write_file(os.path.join(CREDS_BASE, 'alias_map'), '\n'.join(lines) + '\n' if lines else '')


def write_postfix_maps(accounts):
    """
    Generate three Postfix lookup tables in MAIL_BASE:
      sender_relay  — local sender address  ->  [relay]:port
      sasl_passwd   — [relay]:port           ->  username:password
      tls_policy    — [relay]:port           ->  encrypt | may
    """
    sender_relay_lines = []
    sasl_map = {}   # relay -> 'user:pass'
    tls_map = {}    # relay -> 'encrypt' | 'may'

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
            print(f"WARNING: {sender} is missing outbound host/user/pass — skipping", flush=True)
            continue

        relay = f"[{host}]:{port}"
        sender_relay_lines.append(f"{sender} {relay}")
        sasl_map[relay] = f"{user}:{passwd}"
        tls_map[relay] = 'encrypt' if use_tls else 'may'

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


def get_mailbox(account):
    """Return the mailbox storage name (directory under MAIL_BASE)."""
    local = account.get('local', {})
    return local.get('mailbox') or local.get('user') or account.get('address')


def ensure_maildir(account):
    path = os.path.join(MAIL_BASE, get_mailbox(account), 'Maildir')
    mailbox.Maildir(path, create=True)


def fetch_account(account):
    ib = account['inbound']
    local = account['local']
    host = ib['host']
    port = ib.get('port', 110)
    use_ssl = ib.get('tls', False) and port == 995

    ensure_maildir(account)
    md = mailbox.Maildir(os.path.join(MAIL_BASE, get_mailbox(account), 'Maildir'))

    try:
        if use_ssl:
            conn = poplib.POP3_SSL(host, port, timeout=30)
        else:
            conn = poplib.POP3(host, port, timeout=30)
            if ib.get('tls'):
                conn.stls()

        conn.user(ib['user'])
        conn.pass_(ib['pass'])
        _, items, _ = conn.list()

        for item in items:
            num = int(item.decode().split()[0])
            _, lines, _ = conn.retr(num)
            md.add(b'\r\n'.join(lines))
            if ib.get('delete_remote', True):
                conn.dele(num)

        conn.quit()
        print(f"Fetched {len(items)} message(s) for {local['user']}", flush=True)

    except Exception as e:
        print(f"ERROR fetching {local['user']} from {host}: {e}", flush=True)


def main():
    prev_accounts = None
    interval = 15

    while True:
        try:
            with open(ACCOUNTS_FILE) as f:
                cfg = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"ERROR reading {ACCOUNTS_FILE}: {e}", flush=True)
            time.sleep(60)
            continue

        accounts = cfg.get('accounts', [])
        interval = cfg.get('global', {}).get('default_fetch_interval', interval)

        if accounts != prev_accounts:
            os.makedirs(CREDS_BASE, exist_ok=True)
            write_dovecot_passwd(accounts)
            write_postfix_maps(accounts)
            write_alias_map(accounts)
            prev_accounts = accounts
            print("Config reloaded: updated Dovecot passwd, Postfix maps, alias_map", flush=True)

        for account in accounts:
            fetch_account(account)

        time.sleep(int(interval) * 60)


if __name__ == '__main__':
    main()
