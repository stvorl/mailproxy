#!/bin/sh
set -e

umask 022
mkdir -p /var/log/postfix
touch /var/log/postfix/mail.log
chmod 644 /var/log/postfix/mail.log

SMTP_TLS=${SMTP_TLS:-1}

# Ensure shared certificate exists (TLS >= 2).
# Uses mkdir as an atomic lock to prevent two containers generating simultaneously.
if [ "$SMTP_TLS" -ge 2 ]; then
    mkdir -p /certs
    if [ ! -f /certs/mailproxy.pem ] || [ ! -f /certs/mailproxy.key ]; then
        if mkdir /certs/.certlock 2>/dev/null; then
            echo "Generating self-signed certificate..."
            openssl req -new -x509 -days 3650 -nodes \
                -out /certs/mailproxy.pem \
                -keyout /certs/mailproxy.key \
                -subj "/CN=mailproxy" 2>/dev/null
            chmod 644 /certs/mailproxy.key
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

case "$SMTP_TLS" in
    1)
        postconf -e 'smtpd_tls_security_level = none'
        echo "SMTP: plain (no TLS)"
        ;;
    2)
        postconf -e 'smtpd_tls_security_level = may'
        postconf -e 'smtpd_tls_cert_file = /certs/mailproxy.pem'
        postconf -e 'smtpd_tls_key_file = /certs/mailproxy.key'
        echo "SMTP: STARTTLS available"
        ;;
    3)
        postconf -e 'smtpd_tls_security_level = encrypt'
        postconf -e 'smtpd_tls_cert_file = /certs/mailproxy.pem'
        postconf -e 'smtpd_tls_key_file = /certs/mailproxy.key'
        echo "SMTP: STARTTLS required"
        ;;
    *)
        echo "ERROR: unknown SMTP_TLS value '$SMTP_TLS' (expected 1, 2, or 3)" >&2
        exit 1
        ;;
esac

MAPS_DIR=/var/credentials

# Wait up to 90 seconds for the fetcher to generate relay maps.
# The fetcher writes maps before its first fetch cycle, so this
# resolves the startup race between postfix and fetcher.
echo "Waiting for relay maps in $MAPS_DIR ..."
waited=0
while [ ! -f "$MAPS_DIR/sasl_passwd" ] && [ "$waited" -lt 90 ]; do
  sleep 3
  waited=$((waited + 3))
done

if [ ! -f "$MAPS_DIR/sasl_passwd" ]; then
  echo "WARNING: relay maps not found after ${waited}s. Postfix will start without relay config."
fi

# Load whichever maps are present
for map in sasl_passwd sender_relay tls_policy sender_access; do
  if [ -f "$MAPS_DIR/$map" ]; then
    cp "$MAPS_DIR/$map" "/etc/postfix/$map"
    postmap "/etc/postfix/$map"
    echo "Loaded $map"
  fi
done

# Protect credentials file
if [ -f /etc/postfix/sasl_passwd ]; then
  chmod 600 /etc/postfix/sasl_passwd /etc/postfix/sasl_passwd.db 2>/dev/null || true
fi

exec postfix start-fg
