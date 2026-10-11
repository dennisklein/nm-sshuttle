#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Build the nm-sshuttle-release RPM (no dist tag; it serves every Fedora release).
#
# Usage: build.sh <outdir>
set -euo pipefail

outdir=${1:?usage: build.sh <outdir>}
here=$(cd "$(dirname "$0")" && pwd)
topdir=$(mktemp -d)
trap 'rm -rf "$topdir"' EXIT

rpmbuild -bb --quiet \
  --define "_topdir $topdir" \
  --define "_sourcedir $here" \
  --define "dist %{nil}" \
  "$here/nm-sshuttle-release.spec"
mkdir -p "$outdir"
cp "$topdir"/RPMS/noarch/nm-sshuttle-release-*.rpm "$outdir/"
