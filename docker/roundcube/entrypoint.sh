#!/bin/sh
# Configure Roundcube IMAP connection based on IMAP_TLS level,
# then hand off to the official Roundcube entrypoint.

IMAP_TLS=${IMAP_TLS:-1}

export ROUNDCUBEMAIL_DEFAULT_PORT=143

if [ "$IMAP_TLS" -ge 2 ]; then
    export ROUNDCUBEMAIL_DEFAULT_HOST=tls://dovecot
else
    export ROUNDCUBEMAIL_DEFAULT_HOST=dovecot
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

# Override log_driver to write logs to bind-mounted ./logs/roundcube/.
# Appending works because PHP uses the last assignment.
CONFIG=/var/www/html/config/config.inc.php
echo '$config["log_driver"] = "file";' >> "$CONFIG"

exec apache2-foreground
