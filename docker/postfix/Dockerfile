FROM debian:stable-slim
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
    postfix postfix-pcre sasl2-bin libsasl2-modules ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/*
COPY main.cf /etc/postfix/main.cf
COPY smtp_header_checks /etc/postfix/smtp_header_checks
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 25
CMD ["/entrypoint.sh"]
