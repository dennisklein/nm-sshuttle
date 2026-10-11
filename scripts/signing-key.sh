#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Create and rotate the OpenPGP keys that sign nm-sshuttle's RPM packages and
# repository metadata. Run it on a trusted machine, from a checkout of the
# repo.
#
# Usage: signing-key.sh                 create the first key
#        signing-key.sh --next          add a new key beside the current one
#        signing-key.sh --switch FILE   sign with the secret key in FILE
#        signing-key.sh --retire FPR    stop trusting key FPR
#
# Without options, it
# - generates an RSA-4096 signing key without passphrase or expiry in a
#   throwaway GnuPG home
# - creates the GitHub environment rpm-signing if needed and restricts it to
#   the main branch, so that only workflow runs on main can read its secrets;
#   it removes any other deployment branch or tag policy of that environment
# - stores the secret key as secret RPM_SIGNING_KEY of that environment via
#   the gh CLI
# - writes the public key to packaging/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle, to be
#   committed
# - keeps a backup of the secret key and its revocation certificate in
#   ${XDG_DATA_HOME:-~/.local/share}/nm-sshuttle/signing-key/; move them to offline
#   storage
#
# RPM-GPG-KEY-nm-sshuttle lists every key that clients trust, and publish.sh signs
# with one of them, re-signing the packages it restores that another listed
# key signed. A planned rotation therefore takes three steps, each committed
# with a new nm-sshuttle-release Version, so that clients get the new key from a
# package signed by one they trust:
# 1. --next generates a key, adds it to RPM-GPG-KEY-nm-sshuttle and backs it up as
#    above, without signing with it yet.
# 2. Once clients have updated nm-sshuttle-release, --switch with that backup makes
#    the next publish sign everything with the new key.
# 3. After that publish, --retire removes the old key.
#
# Env: NM_SSHUTTLE_SIGNING_UID  key user ID (default: nm-sshuttle package signing <d.klein@gsi.de>)
#      NM_SSHUTTLE_REPO         GitHub repository (default: dennisklein/nm-sshuttle)
set -euo pipefail

uid=${NM_SSHUTTLE_SIGNING_UID:-nm-sshuttle package signing <d.klein@gsi.de>}
repo=${NM_SSHUTTLE_REPO:-dennisklein/nm-sshuttle}
environment=rpm-signing
secret=RPM_SIGNING_KEY

mode=${1:-create}
case $mode in
  create) [ $# -eq 0 ] ;;
  --next) [ $# -eq 1 ] ;;
  --switch | --retire) [ $# -eq 2 ] ;;
  *) false ;;
esac || { echo "usage: $0 [--next | --switch SECRET-KEY-FILE | --retire FINGERPRINT]" >&2; exit 2; }

for cmd in gpg gh git; do
  command -v "$cmd" > /dev/null || { echo "$cmd is required" >&2; exit 1; }
done
root=$(git rev-parse --show-toplevel)
pubkey=$root/packaging/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle
backup=${XDG_DATA_HOME:-$HOME/.local/share}/nm-sshuttle/signing-key

GNUPGHOME=$(mktemp -d)
export GNUPGHOME
trap 'rm -rf "$GNUPGHOME"' EXIT

# fingerprints prints the fingerprints of the primary keys in file $1.
fingerprints() {
  gpg --batch --with-colons --show-keys "$1" |
    awk -F: '$1 == "pub" || $1 == "sec" { p = 1; next } $1 == "fpr" && p { print $10; p = 0 }'
}

# generate creates a key in $GNUPGHOME, backs it up and sets $fpr. --yes
# lets --next give the new key the user ID of the current one.
generate() {
  gpg --batch --quiet --yes --pinentry-mode loopback --passphrase '' \
    --quick-gen-key "$uid" rsa4096 sign never
  fpr=$(gpg --batch --with-colons --list-secret-keys | awk -F: '$1 == "fpr" { print $10; exit }')
  (
    umask 077
    mkdir -p "$backup"
    gpg --batch --pinentry-mode loopback --passphrase '' --armor \
      --export-secret-keys "$fpr" > "$backup/$fpr.secret.asc"
    cp "$GNUPGHOME/openpgp-revocs.d/$fpr.rev" "$backup/$fpr.rev"
  )
}

if [ "$mode" != create ] && [ ! -e "$pubkey" ]; then
  echo "$pubkey does not exist; create the first key without options" >&2
  exit 1
fi
case $mode in
  --next)
    # Export the listed keys and the new one as one keyring, which rpm and
    # dnf import as a whole.
    gpg --batch --quiet --import "$pubkey"
    generate
    gpg --batch --armor --export > "$pubkey.new"
    mv "$pubkey.new" "$pubkey"
    cat <<MSG

Added key $fpr ($uid) to ${pubkey#"$root"/}; packages are still signed
with the current key.

- Bump nm-sshuttle-release's Version, then commit and push.
- Once clients have updated nm-sshuttle-release, run
  $0 --switch $backup/$fpr.secret.asc
- Backups of the secret key and revocation certificate are in $backup.
  Move them to offline storage and delete them from this machine.
MSG
    exit 0 ;;
  --switch)
    fpr=$(fingerprints "$2" | head -n1)
    if [ -z "$fpr" ] || ! fingerprints "$pubkey" | grep -qx "$fpr"; then
      echo "$2 holds no key that ${pubkey#"$root"/} lists; add one with --next first" >&2
      exit 1
    fi
    gh auth status > /dev/null
    gh secret set "$secret" --repo "$repo" --env "$environment" < "$2"
    cat <<MSG

$secret now holds key $fpr. The next publish signs every package with it,
re-signing those that another listed key signed. Once it has, retire the
old key with $0 --retire OLD-FINGERPRINT.
MSG
    exit 0 ;;
  --retire)
    mapfile -t listed < <(fingerprints "$pubkey")
    if ! printf '%s\n' "${listed[@]}" | grep -qx "$2"; then
      echo "${pubkey#"$root"/} does not list key $2" >&2
      exit 1
    fi
    if [ "${#listed[@]}" -lt 2 ]; then
      echo "$2 is the only key in ${pubkey#"$root"/}; add its successor with --next first" >&2
      exit 1
    fi
    id=${2: -8}
    gpg --batch --quiet --import "$pubkey"
    gpg --batch --yes --delete-keys "$2"
    gpg --batch --armor --export > "$pubkey.new"
    mv "$pubkey.new" "$pubkey"
    cat <<MSG

Removed key $2 from ${pubkey#"$root"/}. Bump nm-sshuttle-release's Version,
then commit and push. publish.sh refuses to publish while $secret still
holds that key. Clients keep trusting it until they delete it with
sudo rpmkeys --delete ${id,,}
MSG
    exit 0 ;;
esac

if [ -e "$pubkey" ]; then
  echo "$pubkey already exists; rotate the key with --next, --switch and --retire" >&2
  exit 1
fi
gh auth status > /dev/null
# The public key may be missing from this checkout although the secret exists,
# e.g. in a clone from before the key was committed. Only a 404 means that
# neither the environment nor the secret exists yet.
if out=$(gh api "repos/$repo/environments/$environment/secrets/$secret" 2>&1); then
  echo "$secret already exists in the $environment environment of $repo; update this checkout to get ${pubkey#"$root"/}" >&2
  echo "or, to replace the key, use --next and --switch" >&2
  exit 1
elif ! grep -q 'HTTP 404' <<< "$out"; then
  echo "cannot check for an existing $secret: $out" >&2
  exit 1
fi

# Restrict the environment before any key exists. This replaces the
# environment's protection rules with a deployment branch policy and removes
# every other branch or tag policy an existing environment may have, so that
# main is the only ref that can read the key.
gh api --silent --method PUT "repos/$repo/environments/$environment" \
  -F 'deployment_branch_policy[protected_branches]=false' \
  -F 'deployment_branch_policy[custom_branch_policies]=true'
policies="repos/$repo/environments/$environment/deployment-branch-policies"
# Read the listing into a variable first: a failed listing then aborts the
# script, where one read through process substitution would pass as "no
# policies".
list=$(gh api --paginate "$policies" --jq '.branch_policies[] | [.id, .type, .name] | @tsv')
have_main=
while IFS=$'\t' read -r id type name; do
  [ -n "$id" ] || continue
  if [ "$type" = branch ] && [ "$name" = main ]; then
    have_main=1
  else
    echo "removing $type policy $name from the $environment environment" >&2
    gh api --silent --method DELETE "$policies/$id"
  fi
done <<< "$list"
if [ -z "$have_main" ]; then
  gh api --silent --method POST "$policies" -f name=main -f type=branch
fi

generate
gh secret set "$secret" --repo "$repo" --env "$environment" < "$backup/$fpr.secret.asc"
gpg --batch --armor --export "$fpr" > "$pubkey"

cat <<MSG

Created signing key $fpr ($uid).

- Secret key stored as $secret in the $environment environment of $repo,
  which only workflow runs on main can use.
- Public key written to ${pubkey#"$root"/}; commit and push it.
- Backups of the secret key and revocation certificate are in $backup.
  Move them to offline storage and delete them from this machine.
MSG
