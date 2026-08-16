#!/bin/bash
# docker-mailserver runs this on every start, after its own config is generated.
#
# WHAT CHANGED FROM THE ZEENIE VERSION. The old file forced `disable_plaintext_auth = no`,
# because with SSL_TYPE empty there was no TLS and Dovecot would otherwise have refused every
# IMAP login. That patch is GONE, and its absence is the point: this stack now has a
# certificate, so credentials are carried inside STARTTLS instead of across the LAN in clear.
# Re-adding it would silently undo the reason for the move.
#
# The agents cooperate with this already — agent_inbox issues STARTTLS whenever the server
# advertises it, and agent_outbox does the same on submission.

set -euo pipefail

# Submission (587) must not be optional about it. Postfix will happily fall back to an
# unauthenticated MAIL FROM if a client simply does not offer credentials, which would make
# the whole exercise decorative: the port would accept exactly what :25 used to.
postconf -P "submission/inet/smtpd_tls_security_level=encrypt"
postconf -P "submission/inet/smtpd_sasl_auth_enable=yes"
postconf -P "submission/inet/smtpd_client_restrictions=permit_sasl_authenticated,reject"

# Port 25 is for server-to-server delivery, not for clients. With PERMIT_DOCKER removed from
# the compose file there is no trusted network left, so this is belt to that brace: relaying
# to anywhere other than a local mailbox requires authentication, and authentication is not
# offered here.
postconf "smtpd_relay_restrictions=permit_mynetworks,permit_sasl_authenticated,reject_unauth_destination"

echo "[user-patches] submission requires STARTTLS + SASL; :25 will not relay for strangers"
