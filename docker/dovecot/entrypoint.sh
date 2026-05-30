#!/bin/sh
set -e

mkdir -p /var/log/dovecot /etc/dovecot/conf.d
touch /var/log/dovecot/dovecot.log
chmod 644 /var/log/dovecot/dovecot.log

IMAP_TLS=${IMAP_TLS:-1}

# Ensure shared certificate exists (TLS >= 2).
# Uses mkdir as an atomic lock to prevent two containers generating simultaneously.
if [ "$IMAP_TLS" -ge 2 ]; then
    mkdir -p /certs
    if [ ! -f /certs/mailproxy.pem ] || [ ! -f /certs/mailproxy.key ]; then
        if mkdir /certs/.certlock 2>/dev/null; then
            echo "Generating self-signed certificate..."
            openssl req -new -x509 -days 3650 -nodes \
                -out /certs/mailproxy.pem \
                -keyout /certs/mailproxy.key \
                -subj "/CN=mailproxy" 2>/dev/null
            chmod 600 /certs/mailproxy.key
            echo "Certificate generated at /certs/mailproxy.pem"
            rm -rf /certs/.certlock
        else
            echo "Waiting for shared certificate to be generated..."
            while [ -d /certs/.certlock ] || [ ! -f /certs/mailproxy.pem ]; do
                sleep 1
            done
            echo "Certificate ready."
        fi
    fi
fi

# Write TLS config snippet that dovecot.conf includes at startup.
case "$IMAP_TLS" in
    1)
        cat > /etc/dovecot/conf.d/tls.conf <<'EOF'
ssl = no
EOF
        echo "IMAP: plain (no TLS)"
        ;;
    2)
        cat > /etc/dovecot/conf.d/tls.conf <<'EOF'
ssl = yes
ssl_server_cert_file = /certs/mailproxy.pem
ssl_server_key_file = /certs/mailproxy.key
auth_allow_cleartext = yes
EOF
        echo "IMAP: STARTTLS available on port 143, IMAPS on port 993"
        ;;
    3)
        cat > /etc/dovecot/conf.d/tls.conf <<'EOF'
ssl = required
ssl_server_cert_file = /certs/mailproxy.pem
ssl_server_key_file = /certs/mailproxy.key
auth_allow_cleartext = no
EOF
        echo "IMAP: STARTTLS required on port 143, IMAPS on port 993"
        ;;
    *)
        echo "ERROR: unknown IMAP_TLS value '$IMAP_TLS' (expected 1, 2, or 3)" >&2
        exit 1
        ;;
esac

exec dovecot -F
