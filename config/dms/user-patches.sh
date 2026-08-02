#!/bin/bash
# docker-mailserver runs this on every start, after its own config is generated.
#
# With SSL_TYPE empty there is no TLS, and Dovecot's default `disable_plaintext_auth = yes`
# would then refuse every IMAP login — including the agent's. On a LAN-only compose bridge
# with no route to the internet, plaintext auth is an acceptable MVP trade. Remove this file
# the moment this stack gets a real certificate.
sed -i 's/^#\?disable_plaintext_auth.*/disable_plaintext_auth = no/' /etc/dovecot/conf.d/10-auth.conf
echo "[user-patches] plaintext IMAP auth enabled (no TLS on this LAN-only stack)"
