<?php
// Roundcube now AUTHENTICATES, where it used to relay unauthenticated on :25.
//
// The old config hardcoded empty credentials and sent through port 25, with a comment
// explaining that Postfix would not advertise AUTH without TLS so reusing the IMAP login
// could not work. That was true, and it is the reason the whole stack moved to a self-signed
// certificate: with TLS present, AUTH is advertised, and the ordinary Roundcube behaviour —
// reuse the credentials the user already logged in with — is available.
//
// %u and %p are the IMAP username and password of whoever is signed in. Nothing is stored
// here, which is why this file can live in git.
$config['smtp_host'] = 'tls://mailserver:587';
$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';
$config['imap_host'] = 'tls://mailserver:143';

// The certificate is self-signed and names a host that exists in no public DNS, so it cannot
// be verified and must not be required to be. This is the same posture the agents run with
// (TLS_VERIFY=false): encrypt the connection, do not pretend to authenticate the server.
// The value of TLS here is that mailbox passwords stop crossing the LAN in clear — not that
// it proves the far end is who it claims.
$config['imap_conn_options'] = array(
    'ssl' => array('verify_peer' => false, 'verify_peer_name' => false, 'allow_self_signed' => true),
);
$config['smtp_conn_options'] = $config['imap_conn_options'];
