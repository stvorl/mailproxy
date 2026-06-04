#!/bin/sh
# Configure Roundcube IMAP connection based on IMAP_TLS level,
# then hand off to the official Roundcube entrypoint.

IMAP_TLS=${IMAP_TLS:-1}
SMTP_TLS=${SMTP_TLS:-1}

export ROUNDCUBEMAIL_DEFAULT_PORT=143

if [ "$IMAP_TLS" -ge 2 ]; then
    export ROUNDCUBEMAIL_DEFAULT_HOST=tls://dovecot
else
    export ROUNDCUBEMAIL_DEFAULT_HOST=dovecot
fi

if [ "$SMTP_TLS" -ge 2 ]; then
    export ROUNDCUBEMAIL_SMTP_SERVER=tls://postfix
fi

# Run official entrypoint setup + apache, but intercept after setup
# by wrapping: run setup phase (all but last exec), patch config, then apache.

# The official entrypoint ends with: exec apache2-foreground
# We temporarily replace it with a no-op, let setup run, patch, then start apache.

# Simpler: run setup via a subshell that exits before apache starts,
# patch config, then exec apache normally.

# Extract and run only the setup portion of the official entrypoint.
# The official entrypoint calls apache2-foreground at the very end via exec.
# We override apache2-foreground to just signal done and exit.
mkdir -p /tmp/mailproxy-bin
cat > /tmp/mailproxy-bin/apache2-foreground << 'SENTINEL'
#!/bin/sh
exit 0
SENTINEL
chmod +x /tmp/mailproxy-bin/apache2-foreground

echo "y" | PATH=/tmp/mailproxy-bin:$PATH /docker-entrypoint.sh apache2-foreground 2>&1 || true

# Patch config.inc.php with imap_conn_options if TLS >= 2
if [ "$IMAP_TLS" -ge 2 ]; then
    CONFIG=/var/www/html/config/config.inc.php
    if [ -f "$CONFIG" ] && ! grep -q 'imap_conn_options' "$CONFIG"; then
        printf '\n$config["imap_conn_options"] = ["ssl" => ["verify_peer" => false, "verify_peer_name" => false]];\n' >> "$CONFIG"
    fi
fi

# Disable SSL verification for SMTP if TLS >= 2 (self-signed cert on internal network)
if [ "$SMTP_TLS" -ge 2 ]; then
    CONFIG=/var/www/html/config/config.inc.php
    if [ -f "$CONFIG" ] && ! grep -q 'smtp_conn_options' "$CONFIG"; then
        printf '\n$config["smtp_conn_options"] = ["ssl" => ["verify_peer" => false, "verify_peer_name" => false]];\n' >> "$CONFIG"
    fi
fi

# Override log_driver to write logs to bind-mounted ./logs/roundcube/.
# Appending works because PHP uses the last assignment.
CONFIG=/var/www/html/config/config.inc.php
echo '$config["log_driver"] = "file";' >> "$CONFIG"

# Set custom page title if ROUNDCUBE_TITLE is provided.
if [ -n "${ROUNDCUBE_TITLE:-}" ]; then
    escaped=$(printf '%s' "$ROUNDCUBE_TITLE" | sed "s/'/\\\\'/g")
    printf "\n\$config[\"product_name\"] = '%s';\n" "$escaped" >> "$CONFIG"
fi

# Attachment size limits
# ROUNDCUBE_MAX_MESSAGE_SIZE   — Roundcube max_message_size (entire message)
# ROUNDCUBE_MAX_ATTACHMENT_SIZE — PHP upload_max_filesize (single file)
# PHP post_max_size = ROUNDCUBE_MAX_MESSAGE_SIZE + 10%
ROUNDCUBE_MAX_MESSAGE_SIZE=${ROUNDCUBE_MAX_MESSAGE_SIZE:-8M}
ROUNDCUBE_MAX_ATTACHMENT_SIZE=${ROUNDCUBE_MAX_ATTACHMENT_SIZE:-5M}

# Roundcube max_message_size
printf "\n\$config[\"max_message_size\"] = '%s';\n" "$ROUNDCUBE_MAX_MESSAGE_SIZE" >> "$CONFIG"

# awk helper: human-readable size + 10%, ceiled
to_php_size() {
    awk -v val="$1" 'BEGIN {
        n = substr(val, 1, length(val)-1);
        s = substr(val, length(val));
        if (s == "K" || s == "k") mult = 1024;
        else if (s == "M" || s == "m") mult = 1048576;
        else if (s == "G" || s == "g") mult = 1073741824;
        else { mult = 1; n = val; }
        bytes = int(n * mult * 1.1);
        if (bytes >= 1073741824 && bytes % 1073741824 == 0) printf "%.0fG", bytes / 1073741824;
        else if (bytes >= 1073741824) printf "%.0fG", int(bytes / 1073741824) + 1;
        else if (bytes >= 1048576) printf "%.0fM", int(bytes / 1048576) + 1;
        else if (bytes >= 1024) printf "%.0fK", int(bytes / 1024) + 1;
        else printf "%.0f", bytes;
    }'
}

UPLOAD_SIZE="$ROUNDCUBE_MAX_ATTACHMENT_SIZE"
POST_SIZE=$(to_php_size "$ROUNDCUBE_MAX_MESSAGE_SIZE")

# Write PHP ini overrides — must load AFTER roundcube-defaults.ini (hence zzz- prefix)
PHP_CONF_DIR=/usr/local/etc/php/conf.d
mkdir -p "$PHP_CONF_DIR"
cat > "$PHP_CONF_DIR/zzz-mailproxy.ini" << PHPEOF
upload_max_filesize = $UPLOAD_SIZE
post_max_size = $POST_SIZE
PHPEOF

# Ensure container-owned directories have correct permissions.
# /var/www/html/logs may be a bind mount owned by host root after import.
mkdir -p /var/www/html/logs /var/roundcube/db
chown -R www-data:www-data /var/www/html/logs /var/roundcube/db

exec apache2-foreground
