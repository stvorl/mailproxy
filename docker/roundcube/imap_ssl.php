<?php
// Disable SSL peer verification for self-signed certificates.
// Mounted into Roundcube config dir when IMAP_TLS >= 2.
$config['imap_conn_options'] = [
    'ssl' => ['verify_peer' => false, 'verify_peer_name' => false],
];
