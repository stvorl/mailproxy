import time
import poplib
import mailbox
import os
import yaml

ACCOUNTS_FILE = '/app/accounts.yml'
MAIL_BASE = '/var/mail'


def _write_file(path, content):
    """Write content atomically via a temp file."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write(content)
    os.replace(tmp, path)


def write_dovecot_passwd(accounts):
    """Write Dovecot passwd-file: one 'user:{PLAIN}password' line per account."""
    lines = [
        f"{a['local']['user']}:{{PLAIN}}{a['local']['password']}"
        for a in accounts
    ]
    _write_file(os.path.join(MAIL_BASE, 'passwd'), '\n'.join(lines) + '\n')


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
        sender = a['local']['user']
        ob = a.get('outbound', {})
        host = ob.get('host')
        port = ob.get('port', 587)
        user = ob.get('user')
        passwd = ob.get('pass')
        use_tls = ob.get('tls', True)  # default: require TLS

        if not (host and user and passwd):
            print(f"WARNING: {sender} is missing outbound host/user/pass — skipping")
            continue

        relay = f"[{host}]:{port}"
        sender_relay_lines.append(f"{sender} {relay}")
        sasl_map[relay] = f"{user}:{passwd}"
        tls_map[relay] = 'encrypt' if use_tls else 'may'

    _write_file(
        os.path.join(MAIL_BASE, 'sender_relay'),
        ''.join(line + '\n' for line in sender_relay_lines),
    )
    _write_file(
        os.path.join(MAIL_BASE, 'sasl_passwd'),
        ''.join(f"{relay} {creds}\n" for relay, creds in sasl_map.items()),
    )
    _write_file(
        os.path.join(MAIL_BASE, 'tls_policy'),
        ''.join(f"{relay} {policy}\n" for relay, policy in tls_map.items()),
    )


def ensure_maildir(user):
    path = os.path.join(MAIL_BASE, user, 'Maildir')
    mailbox.Maildir(path, create=True)


def fetch_account(account):
    ib = account['inbound']
    local = account['local']
    host = ib['host']
    port = ib.get('port', 110)
    use_ssl = ib.get('tls', False) and port == 995

    ensure_maildir(local['user'])
    md = mailbox.Maildir(os.path.join(MAIL_BASE, local['user'], 'Maildir'))

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
            write_dovecot_passwd(accounts)
            write_postfix_maps(accounts)
            prev_accounts = accounts
            print("Config reloaded: updated Dovecot passwd and Postfix maps", flush=True)

        for account in accounts:
            fetch_account(account)

        time.sleep(int(interval) * 60)


if __name__ == '__main__':
    main()
