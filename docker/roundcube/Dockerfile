FROM roundcube/roundcubemail:1.6.x-apache

COPY entrypoint.sh /mailproxy-entrypoint.sh
COPY imap_ssl.php /mailproxy-imap-ssl.php
RUN chmod +x /mailproxy-entrypoint.sh

ENTRYPOINT ["/mailproxy-entrypoint.sh"]
