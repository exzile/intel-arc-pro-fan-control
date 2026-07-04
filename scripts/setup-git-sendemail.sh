#!/bin/bash
# setup-git-sendemail.sh — configure git identity + send-email for kernel patches.
# Usage:  ./setup-git-sendemail.sh "Your Name" you@gmail.com
# You still enter the SMTP *app password* yourself at send time (never stored by this script).
set -euo pipefail
NAME="${1:-}"; EMAIL="${2:-}"
[ -n "$NAME" ] && [ -n "$EMAIL" ] || { echo "usage: $0 \"Your Name\" you@example.com"; exit 1; }

# --- identity (goes on Signed-off-by / Tested-by / From) ---
git config --global user.name  "$NAME"
git config --global user.email "$EMAIL"

# --- send-email transport (Gmail example; adjust for another provider) ---
git config --global sendemail.smtpserver     smtp.gmail.com
git config --global sendemail.smtpserverport 587
git config --global sendemail.smtpencryption tls
git config --global sendemail.smtpuser       "$EMAIL"
# Do NOT set sendemail.smtppass here. Options for the password:
#   (a) leave it unset -> git prompts for it interactively each send (simplest, nothing stored), or
#   (b) store it in a credential helper, or
#   (c) put it in ~/.gitconfig sendemail.smtppass (plaintext -- not recommended).
git config --global sendemail.confirm always     # always show the recipient list before sending
git config --global sendemail.annotate yes       # let you review each patch before it goes out

echo "Configured git identity + send-email for: $NAME <$EMAIL>"
echo
echo "GMAIL NOTE: normal password will NOT work. Create an App Password:"
echo "  Google Account -> Security -> 2-Step Verification (must be ON) -> App passwords"
echo "  -> generate one for 'Mail' -> use that 16-char string when git prompts for the password."
echo "(Other providers: set smtpserver/port/encryption to theirs; most also want an app password.)"
