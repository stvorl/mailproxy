FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    dovecot-imapd dovecot-pop3d openssl \
    && rm -rf /var/lib/apt/lists/*
COPY dovecot.conf /etc/dovecot/dovecot.conf
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 143 993
CMD ["/entrypoint.sh"]
