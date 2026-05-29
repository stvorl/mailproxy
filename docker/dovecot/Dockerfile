FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends dovecot-imapd dovecot-pop3d && rm -rf /var/lib/apt/lists/*
COPY dovecot.conf /etc/dovecot/dovecot.conf
EXPOSE 143 993
CMD ["sh", "-c", "mkdir -p /var/log/dovecot && touch /var/log/dovecot/dovecot.log && chmod 644 /var/log/dovecot/dovecot.log && exec dovecot -F"]
