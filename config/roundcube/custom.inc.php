<?php
// Send through the mail server unauthenticated on port 25. The compose bridge is a
// PERMIT_DOCKER trusted network, so Postfix relays for it; Postfix will not advertise
// SMTP AUTH without TLS, and Roundcube's default (reuse the IMAP login) would then fail.
$config['smtp_host'] = 'mailserver:25';
$config['smtp_user'] = '';
$config['smtp_pass'] = '';
$config['imap_host'] = 'mailserver:143';
