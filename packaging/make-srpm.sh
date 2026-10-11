#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Build the source RPMs of nm-sshuttle and nm-sshuttle-gnome.
#
# Usage: make-srpm.sh <outdir> [<tag>]
#
# With a release tag, vX.Y.Z, it packages that tag's source under version
# X.Y.Z. Without one, it packages HEAD under the version of the spec. The
# specs are the ones of this checkout either way, so a fix to them applies
# to a rebuild of an older tag too. It runs `rpmbuild -bs` without a dist tag,
# so one SRPM serves every Fedora release; the rpm jobs turn it into the SRPM
# that they publish, one per release. Needs git and rpmbuild, and runs in a
# checkout with the tag.
set -euo pipefail

outdir=${1:?usage: make-srpm.sh <outdir> [<tag>]}
tag=${2:-}

here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)

if [ -n "$tag" ]; then
  [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "invalid release tag: $tag" >&2; exit 1; }
  version=${tag#v}
  ref=refs/tags/$tag
  git -C "$root" rev-parse --verify --quiet "$ref^{commit}" > /dev/null ||
    { echo "no tag $tag in this checkout" >&2; exit 1; }
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
