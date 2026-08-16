#!/usr/bin/env bash
# Generate the self-signed certificate the mail server needs, BEFORE starting it.
#
# WHY THIS EXISTS. `SSL_TYPE=self-signed` reads as "the server will make its own certificate".
# It does not: docker-mailserver v15 expects the files to be there already and aborts during
# TLS setup when they are not. The name describes the KIND of certificate, not who produces it.
# Found on hp-tiger, where the stack would not come up until the certificate was created by
# hand — a step that existed only in someone's shell history.
#
# So the compose file now uses SSL_TYPE=manual with explicit paths, and this script fills them.
# Manual rather than self-signed on purpose: with explicit SSL_CERT_PATH/SSL_KEY_PATH there is
# exactly one place the certificate can be, named in the compose file, and no dependency on a
# discovery convention that can change between DMS versions. The failure mode if it is missing
# becomes "no such file" — which says what to do — instead of an abort inside TLS setup.
#
#   ./make-certs.sh          # create if absent
#   ./make-certs.sh --force  # replace (rotation, or a changed hostname)
#
# Idempotent: re-running without --force touches nothing, so it is safe in a provisioning
# script that may run more than once.

set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SSL_DIR="$DIR/config/dms/ssl"
# Must match `hostname:` in docker-compose.mail.yml. The certificate is not verified by any
# client here (see the README on TLS_VERIFY), so a mismatch is not fatal — but a certificate
# naming a different host than the server answers as is the sort of detail that wastes an hour
# the day somebody DOES turn verification on.
FQDN="mail.agents.local"
DAYS=3650                      # a decade: nobody is going to remember to rotate this one

CERT="$SSL_DIR/cert.pem"
KEY="$SSL_DIR/key.pem"

if [[ "${1:-}" != "--force" && -f "$CERT" && -f "$KEY" ]]; then
    echo "certificate already present at $CERT — nothing to do (use --force to replace)"
    exit 0
fi

command -v openssl >/dev/null || { echo "openssl is not installed" >&2; exit 1; }

mkdir -p "$SSL_DIR"
echo "==> generating a self-signed certificate for $FQDN (valid ${DAYS} days)"
openssl req -x509 -nodes -newkey rsa:4096 \
    -keyout "$KEY" -out "$CERT" -days "$DAYS" \
    -subj "/CN=$FQDN" \
    -addext "subjectAltName=DNS:$FQDN,DNS:mailserver,DNS:hp-tiger" 2>/dev/null

# The key is readable by the container as root; nothing else on the host needs it.
chmod 600 "$KEY"
chmod 644 "$CERT"

echo "==> wrote:"
echo "    $CERT"
echo "    $KEY"
echo
echo "These paths are mounted into the container at /tmp/docker-mailserver/ssl/ and named by"
echo "SSL_CERT_PATH / SSL_KEY_PATH in docker-compose.mail.yml. Start the stack now."
