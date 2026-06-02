import time
import re
import calendar
import poplib
import imaplib
import mailbox
import os
import json
import yaml
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.utils import parsedate_to_datetime


class _MonthOffset:
    """Calendar-accurate month offset for keep_remote / fetch_depth.
    Supports  datetime - _MonthOffset  (same day/time, N months back;
    clamps to last day of month when the source day doesn't exist)."""
    __slots__ = ('months',)

    def __init__(self, months):
        self.months = months

    def __rsub__(self, dt):
        m = dt.month - self.months
        y = dt.year + (m - 1) // 12
        m = (m - 1) % 12 + 1
        d = min(dt.day, calendar.monthrange(y, m)[1])
        return dt.replace(year=y, month=m, day=d)

    def __repr__(self):
        return f'_MonthOffset({self.months})'


def _as_td_approx(d):
    """Return timedelta approximation of d (30 d per month) for comparisons."""
    if isinstance(d, _MonthOffset):
        return timedelta(days=d.months * 30)
    return d


def _encode_imap_folder(name):
    """
    Encode a folder name to IMAP Modified UTF-7 (RFC 3501 §5.1.3).
    ASCII printable characters (except '&') pass through unchanged.
    Non-ASCII sequences are base64-encoded (with '/' replaced by ',')
    and wrapped in &...-.  
    """
    import base64
    res = []
    buf = []

    def flush():
        if buf:
            b64 = base64.b64encode(''.join(buf).encode('utf-16-be')).decode('ascii')
            # IMAP modified UTF-7 uses ',' instead of '/'.
            b64 = b64.replace('/', ',')
            res.append('&' + b64.rstrip('=') + '-')
            buf.clear()

    for ch in name:
        if ch == '&':
            flush()
            res.append('&-')
        elif 0x20 <= ord(ch) <= 0x7e:
            flush()
            res.append(ch)
        else:
            buf.append(ch)
    flush()
    return ''.join(res)


def _decode_imap_folder(name):
    """Decode IMAP Modified UTF-7 folder name to Unicode for logs/files."""
    import base64
    out = []
    i = 0
    n = len(name)
    while i < n:
        ch = name[i]
        if ch != '&':
            out.append(ch)
            i += 1
            continue

        j = name.find('-', i)
        if j == -1:
            out.append(name[i:])
            break

        chunk = name[i + 1:j]
        if chunk == '':
            out.append('&')  # "&-"
        else:
            b64 = chunk.replace(',', '/')
            pad = '=' * ((4 - len(b64) % 4) % 4)
            try:
                raw = base64.b64decode(b64 + pad)
                out.append(raw.decode('utf-16-be'))
            except Exception:
                out.append(name[i:j + 1])
        i = j + 1

    return ''.join(out)


def _parse_imap_list_entry(row):
    """Parse one IMAP LIST row into (flags_list, encoded_folder_name)."""
    line = row.decode(errors='replace')
    m = re.match(r'^\((?P<flags>[^)]*)\)\s+"[^"]*"\s+(?P<name>.+)$', line)
    if not m:
        return None

    flags_raw = m.group('flags').strip()
    flags = flags_raw.split() if flags_raw else []
    name = m.group('name').strip()
    if name.startswith('"') and name.endswith('"') and len(name) >= 2:
        name = name[1:-1]
    return flags, name


def _list_imap_folders(conn):
    """Return parsed LIST entries as [{'flags': [...], 'name': '...'}]."""
    typ, data = conn.list()
    if typ != 'OK' or not data:
        return []

    entries = []
    for row in data:
        parsed = _parse_imap_list_entry(row)
        if not parsed:
            continue
        flags, name = parsed
        entries.append({'flags': flags, 'name': name})
    return entries


def _update_imap_folders_file(conn, label):
    """
    Write /var/mail/<mailbox>/imap_folders.txt with two columns:
      1) full remote folder path (decoded to Unicode)
      2) folder flags from LIST
    """
    entries = _list_imap_folders(conn)
    out_path = os.path.join(MAIL_BASE, label, 'imap_folders.txt')
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    rows = []
    for e in entries:
        full_path = _decode_imap_folder(e['name'])
        flags_col = ' '.join(e['flags']) if e['flags'] else '-'
        rows.append((full_path, flags_col))

    rows.sort(key=lambda x: x[0].lower())

    lines = ['folder\tflags']
    lines.extend(f"{path}\t{flags}" for path, flags in rows)
    _write_file(out_path, '\n'.join(lines) + '\n')
    try:
        os.chown(out_path, 1000, 1000)
    except OSError:
        pass


def _parse_imap_folder_mappings(val):
    """
    Parse inbound.folder into a list of (remote_folder, local_folder) pairs.

    Supported entry formats (comma-separated):
      REMOTE            -> local defaults to INBOX
      REMOTE>LOCAL      -> explicit local mapping

    Backward compatibility:
      "INBOX, News" is interpreted as "INBOX>INBOX, News>INBOX".
    """
    spec = 'INBOX' if val is None else str(val)
    pairs = []

    for raw_part in spec.split(','):
        part = raw_part.strip()
        if not part:
            continue

        if '>' in part:
            remote, local = part.split('>', 1)
            remote = remote.strip()
            local = local.strip() or 'INBOX'
        else:
            remote = part
            local = 'INBOX'

        if not remote:
            log(f"WARNING: skipping malformed folder mapping entry {raw_part!r}")
            continue
        pairs.append((remote, local))

    if not pairs:
        return [('INBOX', 'INBOX')]

    # Keep first occurrence order; ignore duplicate remote folders.
    seen_remote = set()
    result = []
    for remote, local in pairs:
        if remote in seen_remote:
            log(f"WARNING: duplicate remote folder mapping for {remote!r}; using first entry")
            continue
        seen_remote.add(remote)
        result.append((remote, local))
    return result


def _chown_tree(path, uid=1000, gid=1000):
    """Best-effort recursive chown for paths created by fetcher running as root."""
    try:
        os.chown(path, uid, gid)
    except OSError:
        return
    for dirpath, _dirnames, filenames in os.walk(path):
        try:
            os.chown(dirpath, uid, gid)
        except OSError:
            pass
        for fname in filenames:
            try:
                os.chown(os.path.join(dirpath, fname), uid, gid)
            except OSError:
                pass


def _append_subscription(md, folder_name):
    """Ensure folder is listed in Maildir subscriptions file."""
    subs_path = os.path.join(md._path, 'subscriptions')
    line = f'{folder_name}\n'
    try:
        with open(subs_path, 'r', encoding='utf-8') as f:
            existing = f.readlines()
    except FileNotFoundError:
        existing = []
    if line not in existing:
        with open(subs_path, 'a', encoding='utf-8') as f:
            f.write(line)


def _ensure_local_maildir_folder(md, local_folder):
    """
    Return a Maildir destination for local_folder.
    INBOX maps to the root Maildir object. Other folders are auto-created.
    """
    if local_folder.upper() == 'INBOX':
        return md

    # Dovecot Maildir++ stores non-ASCII mailbox names on disk using
    # IMAP modified UTF-7. Keep subscription names human-readable, but use
    # encoded names for filesystem folder paths.
    disk_folder = _encode_imap_folder(local_folder)

    # One-time migration for folders previously created with raw UTF-8 names.
    if disk_folder != local_folder:
        old_path = os.path.join(md._path, '.' + local_folder)
        new_path = os.path.join(md._path, '.' + disk_folder)
        if os.path.isdir(old_path) and not os.path.exists(new_path):
            os.rename(old_path, new_path)
            _chown_tree(new_path)

    try:
        sub = md.get_folder(disk_folder)
    except mailbox.NoSuchMailboxError:
        md.add_folder(disk_folder)
        sub = md.get_folder(disk_folder)

    sub_path = getattr(sub, '_path', None)
    if sub_path:
        _chown_tree(sub_path)
    _append_subscription(md, local_folder)
    return sub

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


def _message_before_cutoff(raw, cutoff):
    """
    Return True if the message's Date header is strictly before cutoff.
    Used for sub-day filtering when fetch_depth is finer than one calendar day.
    Returns False on any parse error so messages are not silently dropped.
    """
    try:
        msg = message_from_bytes(raw)
        date_str = msg.get('Date')
        if not date_str:
            return False
        msg_dt = parsedate_to_datetime(date_str)
        if msg_dt.tzinfo is None:
            msg_dt = msg_dt.replace(tzinfo=timezone.utc)
        return msg_dt < cutoff
    except Exception:
        return False  # on parse error, keep the message


def _parse_duration_str(s):
    """
    Parse a duration string with a unit suffix into a timedelta.
    Returns timedelta on success, or None if the string is not a valid duration.

    Accepted formats: 10m, 12h, 30s, 7d, 7D, 3M
      s — seconds
      m — minutes
      h — hours
      d / D — days
      M — months (~30 days each)
    """
    m = re.fullmatch(r'(\d+)([dDhsmM])', s.strip())
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    if n == 0:
        return None
    if unit in ('d', 'D'):
        return timedelta(days=n)
    if unit == 'M':
        return _MonthOffset(n)
    if unit == 'h':
        return timedelta(hours=n)
    if unit == 'm':
        return timedelta(minutes=n)
    if unit == 's':
        return timedelta(seconds=n)
    return None  # unreachable, but satisfies linters


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
        td = _parse_duration_str(s)
        if td is not None:
            return td
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


def parse_fetch_depth(val):
    """
    Parse fetch_depth config value.
    Returns timedelta (how far back to search) or None (unlimited, SEARCH ALL).

    Accepted formats: same suffixes as keep_remote (s, m, h, d/D, M).
    Absent / 0 / false / true all mean unlimited.
    """
    if val is None or isinstance(val, bool) or val == 0:
        return None
    if isinstance(val, int):
        return timedelta(days=val)
    if isinstance(val, str):
        s = val.strip()
        if s.lower() in ('', 'true', 'false', 'no', '0'):
            return None
        td = _parse_duration_str(s)
        if td is not None:
            return td
    log(f"WARNING: unrecognised fetch_depth value {val!r}, treating as unlimited")
    return None


def compute_imap_search_since(fetch_depth, keep_for, fetch_interval_min, label):
    """
    Return the SEARCH SINCE cutoff datetime, or None for SEARCH ALL.

    When keep_for is a timedelta the effective search window must be at least
    keep_for + 10 * fetch_interval so that messages approaching expiry are
    still visible between poll cycles and get properly deleted.
    fetch_depth: true/false do not require any adjustment.
    """
    if isinstance(keep_for, (timedelta, _MonthOffset)):
        buffer = timedelta(minutes=fetch_interval_min * 10)
        min_required = _as_td_approx(keep_for) + buffer
        if fetch_depth is None:
            return None  # unlimited always satisfies min_required
        if _as_td_approx(fetch_depth) < min_required:
            log(f"INFO {label}: fetch_depth extended from {fetch_depth} to "
                f"{min_required} to cover keep_remote expiry window")
            return datetime.now(timezone.utc) - min_required
        return datetime.now(timezone.utc) - fetch_depth

    # keep_for is False or True — no adjustment
    if fetch_depth is None:
        return None
    return datetime.now(timezone.utc) - fetch_depth


def get_mailbox(account):
    """Return the mailbox storage name (directory under MAIL_BASE)."""
    local = account.get('local', {})
    return local.get('mailbox') or local.get('user') or account.get('address')


def ensure_maildir(account):
    mailbox_root = os.path.join(MAIL_BASE, get_mailbox(account))
    created = not os.path.exists(mailbox_root)
    os.makedirs(mailbox_root, exist_ok=True)
    path = os.path.join(mailbox_root, 'Maildir')
    mailbox.Maildir(path, create=True)
    if created:
        # Fetcher runs as root; Dovecot expects uid=gid=1000.
        # chown the freshly created tree so Dovecot can access it immediately
        # without waiting for its own entrypoint chown on the next restart.
        _chown_tree(mailbox_root)


def fetch_imap(account, fetch_interval_min):
    ib = account['inbound']
    host = ib['host']
    port = ib.get('port', 993 if ib.get('tls') else 143)
    use_ssl = ib.get('tls', False) and port == 993
    keep_for = parse_keep_remote(ib.get('keep_remote', False))
    fetch_depth = parse_fetch_depth(ib.get('fetch_depth'))
    folder_mappings = _parse_imap_folder_mappings(ib.get('folder'))
    label = get_mailbox(account)

    search_since = compute_imap_search_since(fetch_depth, keep_for, fetch_interval_min, label)

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

        # Keep an up-to-date IMAP folder inventory near mailbox data.
        _update_imap_folders_file(conn, label)

        fetched = 0
        deleted = 0
        seen_keys = set()

        for remote_folder, local_folder in folder_mappings:
            encoded_folder = _encode_imap_folder(remote_folder)
            sel_status, sel_data = conn.select(encoded_folder)

            if sel_status != 'OK':
                log(f"WARNING IMAP {label}: cannot select folder {remote_folder!r} "
                    f"({encoded_folder!r}) on {host}: {sel_data}")
                continue

            try:
                dst = _ensure_local_maildir_folder(md, local_folder)
            except Exception as e:
                log(f"WARNING IMAP {label}: cannot prepare local folder {local_folder!r} "
                    f"for remote {remote_folder!r}: {e}")
                continue

            if search_since is not None:
                # Subtract 1 extra calendar day before formatting the SINCE date.
                # IMAP SEARCH SINCE uses the server's internal message date, which
                # is stored in the server's local timezone. A server west of UTC
                # will record messages arriving in the first hours of a UTC day
                # with an internal date of "yesterday". Without this buffer those
                # messages would never match a SINCE query for "today". The extra
                # day yields at most one additional day's worth of header-only
                # fetches; the sub-day filter discards anything outside the exact
                # cutoff window cheaply.
                date_str = (search_since - timedelta(days=1)).strftime('%d-%b-%Y')
                search_status, data = conn.uid('SEARCH', None, 'SINCE', date_str)
            else:
                search_status, data = conn.uid('SEARCH', None, 'ALL')
            if search_status != 'OK' or not data or data[0] is None:
                log(f"WARNING IMAP {label}: SEARCH failed in folder {remote_folder!r} "
                    f"on {host}: {data}")
                continue
            uids = data[0].split()

            for uid_bytes in uids:
                uid_str = uid_bytes.decode()
                key = f'{remote_folder}/{uid_str}'
                seen_keys.add(key)

                if key not in state:
                    # Sub-day filtering: IMAP SEARCH SINCE has day granularity only.
                    # Fetch just the Date header first to avoid downloading the full
                    # body for messages that fall outside the exact cutoff window.
                    if search_since is not None:
                        _, hdr_data = conn.uid('FETCH', uid_bytes,
                                               '(BODY.PEEK[HEADER.FIELDS (DATE)])')
                        hdr_raw = hdr_data[0][1]
                        if _message_before_cutoff(hdr_raw, search_since):
                            continue  # too old; skip body download entirely
                    _, msg_data = conn.uid('FETCH', uid_bytes, '(RFC822)')
                    raw = msg_data[0][1]
                    dst.add(raw)
                    fetched += 1
                    if keep_for is False:
                        conn.uid('STORE', uid_bytes, '+FLAGS', '\\Deleted')
                        deleted += 1
                    else:
                        state[key] = now.isoformat()
                        seen_keys.add(key)
                elif isinstance(keep_for, (timedelta, _MonthOffset)):
                    fetch_time = datetime.fromisoformat(state[key])
                    if fetch_time <= (now - keep_for):
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


def fetch_account(account, fetch_interval_min):
    if 'inbound' not in account:
        return  # local-only account, no remote fetching
    proto = account['inbound'].get('proto', 'pop3').lower()
    if proto == 'imap':
        fetch_imap(account, fetch_interval_min)
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
            elif isinstance(keep_for, (timedelta, _MonthOffset)):
                fetch_time = datetime.fromisoformat(state[uidl])
                if fetch_time <= (now - keep_for):
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
            interval_min = get_fetch_interval(account, default_interval)
            due = triggered or (key not in last_fetched) or (now - last_fetched[key] >= interval_min * 60)
            if due:
                fetch_account(account, interval_min)
                last_fetched[key] = time.monotonic()

        time.sleep(2)


if __name__ == '__main__':
    main()
