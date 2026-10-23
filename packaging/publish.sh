#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Assemble the signed dnf repository site.
#
# Usage: publish.sh <incoming-dir> <site-dir>
#
# Restores the packages already published at $PAGES_URL (listed with their
# SHA-256 in packages.sha256), signs and files the new packages from
# <incoming-dir>, prunes old versions, verifies every package signature,
# regenerates and signs the repository metadata, and writes the static site
# into <site-dir> with a listing of every directory.
#
# Env:  PAGES_URL        published repository site
#       KEEP_VERSIONS    versions to keep per package, newest release each
#                        (default 2), plus the newest of the previous
#                        major.minor line
#       RESTORE          "false" starts from an empty repository instead of
#                        the published packages: for a dry run with another
#                        key, which could not verify them, and after a key
#                        compromise, when no listed key may (default true)
#       RPM_SIGNING_KEY  armored OpenPGP secret key without passphrase; it
#                        must be one of the keys that
#                        packaging/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle lists
#
# Restored packages that another key listed there signed are re-signed with
# RPM_SIGNING_KEY, so that a rotation (see scripts/signing-key.sh) moves the
# whole repository to the new key. Packages that no listed key signed are an
# error.
set -euo pipefail

incoming=${1:?usage: publish.sh <incoming-dir> <site-dir>}
site=${2:?usage: publish.sh <incoming-dir> <site-dir>}
here=$(cd "$(dirname "$0")" && pwd)
pubkey=$here/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle
: "${PAGES_URL:?}" "${RPM_SIGNING_KEY:?RPM_SIGNING_KEY is not set}"
mapfile -t releases < <(grep -E '^[0-9]+$' "$here/fedora-releases")

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
export GNUPGHOME=$work/gnupg
mkdir -m 0700 "$GNUPGHOME"

gpg --batch --quiet --import <<<"$RPM_SIGNING_KEY"
fpr=$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')
listed=$(gpg --batch --with-colons --show-keys "$pubkey" |
  awk -F: '$1 == "pub" { p = 1; next } $1 == "fpr" && p { print $10; p = 0 }')
if [ -z "$fpr" ] || ! grep -qx "$fpr" <<<"$listed"; then
  echo "::error::signing key '$fpr' is not one of the keys in $pubkey: ${listed//$'\n'/ }"
  exit 1
fi
# rpmdb trusts the signing key only, trusted every key that $pubkey lists.
mkdir "$work/rpmdb" "$work/trusted"
gpg --batch --armor --export "$fpr" > "$work/signing-key.asc"
rpmkeys --dbpath "$work/rpmdb" --import "$work/signing-key.asc"
rpmkeys --dbpath "$work/trusted" --import "$pubkey"
signed() {
  rpmkeys --dbpath "$work/$1" --checksig "$2" | grep -q 'signatures OK$'
}

mkdir -p "$site"

# Restore the published packages; the site is the repository's only state.
# Only a 404 means that nothing is published yet: any other failure would
# otherwise deploy a repository without the packages it had.
status=
if [ "${RESTORE:-true}" != false ]; then
  status=$(curl -sSL --retry 3 -o "$work/packages.sha256" -w '%{http_code}' "$PAGES_URL/packages.sha256") || status=failed
fi
if [ "${RESTORE:-true}" = false ]; then
  echo "not restoring the packages published at $PAGES_URL"
elif [ "$status" = 404 ]; then
  echo "no packages.sha256 at $PAGES_URL; starting an empty repository"
elif [ "$status" != 200 ]; then
  echo "::error::fetching $PAGES_URL/packages.sha256 failed ($status); not publishing a repository without its packages"
  exit 1
else
  while read -r _ path; do
    if [[ ! $path =~ ^rpm/fedora/([0-9]+/x86_64|source)/[A-Za-z0-9._+~^-]+\.rpm$ ]]; then
      echo "::error::unexpected path in packages.sha256: $path"
      exit 1
    fi
    mkdir -p "$site/${path%/*}"
    curl -fsSL --retry 3 "$PAGES_URL/$path" -o "$site/$path"
  done < "$work/packages.sha256"
  (cd "$site" && sha256sum --check --quiet --strict "$work/packages.sha256")
  # The listings show when each package was first published.
  if curl -fsSL "$PAGES_URL/packages.mtime" -o "$work/packages.mtime"; then
    while read -r mtime path; do
      if [[ ! $mtime =~ ^[0-9]+$ || ! $path =~ ^rpm/fedora/([0-9]+/x86_64|source)/[A-Za-z0-9._+~^-]+\.rpm$ ]]; then
        echo "::error::unexpected line in packages.mtime: $mtime $path"
        exit 1
      fi
      touch -c -m -d "@$mtime" "$site/$path"
    done < "$work/packages.mtime"
  fi
  # Move packages that another listed key signed onto the signing key,
  # keeping their dates.
  while read -r _ path; do
    pkg=$site/$path
    signed rpmdb "$pkg" && continue
    if ! signed trusted "$pkg"; then
      echo "::error::$path is not signed by a key that $pubkey lists; after a key compromise, publish with discard-published"
      rpmkeys --dbpath "$work/trusted" --checksig --verbose "$pkg"
      exit 1
    fi
    mtime=$(stat -c %Y "$pkg")
    rpmsign --define "_gpg_name $fpr" --resign "$pkg" > /dev/null
    touch -m -d "@$mtime" "$pkg"
    echo "re-signed $path"
  done < "$work/packages.sha256"
fi

# Sign and file the new packages.
shopt -s nullglob
for pkg in "$incoming"/*.rpm; do
  rpmsign --define "_gpg_name $fpr" --addsign "$pkg" > /dev/null
  name=${pkg##*/}
  dests=()
  case $name in
    *.src.rpm)
      dests=(rpm/fedora/source) ;;
    nm-sshuttle-release-*.noarch.rpm)
      for f in "${releases[@]}"; do dests+=("rpm/fedora/$f/x86_64"); done ;;
    # Built per Fedora release, with a dist tag: nm-sshuttle (noarch) and
    # nm-sshuttle-gnome, which ship side by side.
    *.fc[0-9]*.x86_64.rpm | *.fc[0-9]*.noarch.rpm)
      f=${name##*.fc}
      dests=("rpm/fedora/${f%%.*}/x86_64") ;;
    *)
      echo "::error::cannot tell which repository $name belongs to"
      exit 1 ;;
  esac
  for d in "${dests[@]}"; do
    mkdir -p "$site/$d"
    # A rebuild, such as for a newly added Fedora release, signs every
    # package anew. Keep the one published under the same name, so that its
    # bytes and its listed date stay put; a changed package needs a new
    # version or baserelease.
    cp -n "$pkg" "$site/$d/"
  done
done

# Every release repository carries the newest nm-sshuttle-release, also after a
# Fedora release was added without a new nm-sshuttle-release build.
newest=$(find "$site/rpm/fedora" -name 'nm-sshuttle-release-*.noarch.rpm' -printf '%f\t%p\n' | sort -V | tail -n1 | cut -f2)
if [ -z "$newest" ]; then
  echo "::error::no nm-sshuttle-release package to publish"
  exit 1
fi
for f in "${releases[@]}"; do
  mkdir -p "$site/rpm/fedora/$f/x86_64"
  cp -np "$newest" "$site/rpm/fedora/$f/x86_64/"
done
cp -p "$newest" "$site/nm-sshuttle-release.noarch.rpm"

# Prune, verify that the signing key signed every package, regenerate and
# sign the metadata.
for dir in "$site"/rpm/fedora/*/x86_64 "$site"/rpm/fedora/source; do
  [ -d "$dir" ] || continue
  python3 "$here/prune-repo.py" --keep "${KEEP_VERSIONS:-2}" "$dir"
  for pkg in "$dir"/*.rpm; do
    if ! signed rpmdb "$pkg"; then
      echo "::error::bad or missing signature: $pkg"
      rpmkeys --dbpath "$work/rpmdb" --checksig --verbose "$pkg"
      exit 1
    fi
  done
  createrepo_c --quiet "$dir"
  gpg --batch --yes --local-user "$fpr" --detach-sign --armor "$dir/repodata/repomd.xml"
done

cp "$pubkey" "$site/RPM-GPG-KEY-nm-sshuttle"
# As gpg --show-keys prints it, which the install steps compare.
sed -e "s/@FINGERPRINT@/$fpr/g" "$here/site/index.html" > "$site/index.html"
(cd "$site" && find rpm -name '*.rpm' | LC_ALL=C sort | xargs -r sha256sum) > "$site/packages.sha256"
(cd "$site" && find rpm -name '*.rpm' | LC_ALL=C sort | xargs -r stat -c '%Y %n') > "$site/packages.mtime"
python3 "$here/site-index.py" "$PAGES_URL" "$site"
echo "assembled $(wc -l < "$site/packages.sha256") packages signed by $fpr"
