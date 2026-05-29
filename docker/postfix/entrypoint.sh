#!/bin/sh
set -e

umask 022
mkdir -p /var/log/postfix
touch /var/log/postfix/mail.log
chmod 644 /var/log/postfix/mail.log

MAPS_DIR=/var/mail

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
for map in sasl_passwd sender_relay tls_policy; do
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
