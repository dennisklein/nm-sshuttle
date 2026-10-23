#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Build the source RPMs of nm-sshuttle and nm-sshuttle-gnome.
#
# Usage: make-srpm.sh <outdir> [<tag> [<commit>]]
#
# With a release tag, vX.Y.Z, it packages that tag's source under version
# X.Y.Z. With a commit as well, it refuses a tag that no longer names that
# commit, the one that packages.yaml's plan job checked is on main.
# Without a tag, it packages HEAD under the version of the spec. The
# specs are the ones of this checkout either way, so a fix to them applies
# to a rebuild of an older tag too. It runs `rpmbuild -bs` without a dist tag,
# so one SRPM serves every Fedora release; the rpm jobs turn it into the SRPM
# that they publish, one per release. Needs git and rpmbuild, and runs in a
# checkout with the tag.
set -euo pipefail

usage='usage: make-srpm.sh <outdir> [<tag> [<commit>]]'
outdir=${1:?$usage}
tag=${2:-}
if [ $# -gt 3 ] || { [ $# -eq 3 ] && [ -z "$3" ]; }; then
  echo "$usage" >&2
  exit 2
fi

here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)

if [ -n "$tag" ]; then
  [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid release tag: $tag" >&2; exit 1; }
  version=${tag#v}
  ref=$(git -C "$root" rev-parse --verify --quiet "refs/tags/$tag^{commit}") ||
    { echo "no tag $tag in this checkout" >&2; exit 1; }
  if [ $# -eq 3 ] && [ "$ref" != "$3" ]; then
    echo "tag $tag names $ref, not $3; it has moved since it was checked" >&2
    exit 1
  fi
else
  version=$(sed -n 's/^Version: *//p' "$here/rpm/nm-sshuttle.spec")
  ref=HEAD
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/sources"

git -C "$root" archive --format=tar.gz --prefix="nm-sshuttle-$version/" \
  -o "$work/sources/nm-sshuttle-$version.tar.gz" "$ref"

mkdir -p "$outdir"
for spec in "$here"/rpm/*.spec; do
  sed "s/^Version: .*/Version:        $version/" "$spec" > "$work/sources/${spec##*/}"
  rpmbuild -bs --nodeps \
    --define "_sourcedir $work/sources" \
    --define "_srcrpmdir $outdir" \
    --define "dist %{nil}" \
    "$work/sources/${spec##*/}"
done
