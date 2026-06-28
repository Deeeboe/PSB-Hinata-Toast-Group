#!/bin/bash
# Setup script for the "PITCH Data Pipeline" cloud environment.
# Paste this into the environment's "Setup script" field (runs once per session,
# before Claude Code launches). The cloud image lacks the SFTP client, so install
# it. Requires the Ubuntu apt mirrors to be reachable — either tick "include
# default list of common package managers" in Network access, or add
# archive.ubuntu.com + security.ubuntu.com to Allowed domains.
set -e
if ! command -v sftp >/dev/null 2>&1; then
  # Unrelated third-party PPAs (deadsnakes/php) baked into the image 403 and are
  # not needed; don't let them abort. The main Ubuntu repos (which carry
  # openssh-client) update fine, so the install still succeeds.
  apt-get update || true
  apt-get install -y openssh-client
fi
sftp -V || true   # print version to confirm it's present
