#!/bin/bash
# Setup script for the "PITCH Data Pipeline" cloud environment.
# Paste this into the environment's "Setup script" field (runs once per session,
# before Claude Code launches). The cloud image lacks the SFTP client, so install
# it. Requires the Ubuntu apt mirrors to be reachable — either tick "include
# default list of common package managers" in Network access, or add
# archive.ubuntu.com + security.ubuntu.com to Allowed domains.
set -e
if ! command -v sftp >/dev/null 2>&1; then
  apt-get update
  apt-get install -y openssh-client
fi
sftp -V || true   # print version to confirm it's present
