FROM debian:stable-slim
RUN apt-get update && apt-get install -y --no-install-recommends dovecot-imapd dovecot-pop3d && rm -rf /var/lib/apt/lists/*
COPY dovecot.conf /etc/dovecot/dovecot.conf
EXPOSE 143 993
CMD ["dovecot", "-F"]
