#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
#
# Decide what the packages workflow builds and publishes.
#
# Env:  PAGES_URL      published repository site
#       INPUT_TAG      release tag to package; empty means the newest
#                      vX.Y.Z tag of the checkout
#       REBUILD        "true" rebuilds even if the packages are already
#                      published or the repository would not keep the release
#       GITHUB_REF     only refs/heads/main may publish
#       GITHUB_EVENT_NAME  "push" republishes the site even if nothing needs
#                      building, so that changes to it go live
#       KEEP_VERSIONS  versions kept per package (default 2); a release that
#                      publish.sh would prune is not built
# Writes tag, build (true|false), fedora (JSON list) and publish
# (real|dry-run|none) to $GITHUB_OUTPUT (stdout when unset).
#
# A real publish packages a release tag. A dry run assembles the repository
# with a throwaway key from the checkout's HEAD instead (tag is empty then),
# for pull requests and for as long as there is no tag or no signing key.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
root=$(git -C "$here" rev-parse --show-toplevel)
out=${GITHUB_OUTPUT:-/dev/stdout}
pubkey=$here/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle

tag=${INPUT_TAG:-}
if [ -z "$tag" ]; then
  tag=$(git -C "$root" tag --list 'v[0-9]*' --sort=-v:refname |
    grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n1 || true)
fi
if [ -n "$tag" ] && [[ ! $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "::error::invalid release tag: $tag"
  exit 1
fi
if [ -n "$tag" ] && ! git -C "$root" rev-parse --verify --quiet "refs/tags/$tag^{commit}" > /dev/null; then
  echo "::error::no tag $tag in this repository"
  exit 1
fi
version=${tag#v}

mapfile -t releases < <(grep -E '^[0-9]+$' "$here/fedora-releases")
release=$(sed -n 's/^Release: *\([0-9][0-9]*\).*/\1/p' "$here/rpm/nm-sshuttle-gnome.spec")
rel_version=$(sed -n 's/^Version: *//p' "$here/nm-sshuttle-release/nm-sshuttle-release.spec")
rel_release=$(sed -n 's/^Release: *//p' "$here/nm-sshuttle-release/nm-sshuttle-release.spec")

# published reports whether a file is on the site. Only a 404 means it is
# not: on any other failure, guessing could rebuild and republish.
published() {
  local status
  status=$(curl -sSI --retry 3 -o /dev/null -w '%{http_code}' "$PAGES_URL/$1") || status=failed
  case $status in
    200) return 0 ;;
    404) return 1 ;;
  esac
  echo "::error::checking $PAGES_URL/$1 failed ($status)" >&2
  exit 1
}

# superseded reports whether publish.sh would prune version $3 of package $2
# at once for Fedora release $1, as it keeps only the KEEP_VERSIONS newest
# versions and the newest of the previous major.minor line (see
# prune-repo.py); the next run would then build it again.
listing=
superseded() {
  local out status v kept=() line top_line version=$3
  if [ -z "$listing" ]; then
    out=$(curl -sSL --retry 3 -w '\n%{http_code}' "$PAGES_URL/packages.sha256") || out=$'\nfailed'
    status=${out##*$'\n'}
    case $status in
      200) listing=${out%$'\n'*} ;;
      404) listing=none ;;
      *)
        echo "::error::fetching $PAGES_URL/packages.sha256 failed ($status)" >&2
        exit 1 ;;
    esac
  fi
  local versions
  versions=$( {
    echo "$version"
    sed -n "s|^.* rpm/fedora/$1/x86_64/$2-\\([0-9][^-/]*\\)-[^/]*\\.fc$1\\.x86_64\\.rpm\$|\\1|p" <<<"$listing"
  } | sort -Vru)
  top_line=
  while read -r v; do
    line=$(cut -d. -f1-2 <<<"$v")
    [ -n "$top_line" ] || top_line=$line
    if [ "${#kept[@]}" -lt "${KEEP_VERSIONS:-2}" ]; then
      kept+=("$v")
    elif [ "$line" != "$top_line" ]; then
      kept+=("$v")
      break
    fi
    if [ "$line" != "$top_line" ]; then
      break
    fi
  done <<<"$versions"
  for v in "${kept[@]}"; do
    [ "$v" != "$version" ] || return 1
  done
  return 0
}

build=false
release_missing=false
if [ -n "$tag" ]; then
  for f in "${releases[@]}"; do
    # nm-sshuttle-gnome is the arch-specific package, so it stands for both.
    if ! published "rpm/fedora/$f/x86_64/nm-sshuttle-gnome-$version-$release.fc$f.x86_64.rpm"; then
      if superseded "$f" nm-sshuttle-gnome "$version"; then
        echo "::notice::the repository would not keep $tag next to the published versions for Fedora $f; not building it" >&2
      else
        build=true
      fi
    fi
    published "rpm/fedora/$f/x86_64/nm-sshuttle-release-$rel_version-$rel_release.noarch.rpm" || release_missing=true
  done
  if [ "${REBUILD:-false}" = true ]; then
    build=true
  fi
fi

publish=none
if [ "${GITHUB_REF:-}" = refs/heads/main ] && [ -n "$tag" ] && [ -s "$pubkey" ]; then
  if [ "$build" = true ] || [ "$release_missing" = true ] || [ "${GITHUB_EVENT_NAME:-}" = push ]; then
    publish=real
  fi
else
  if [ ! -s "$pubkey" ]; then
    echo "::warning::packaging/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle is missing; run scripts/signing-key.sh. Publishing as a dry run with a throwaway key."
  elif [ -z "$tag" ]; then
    echo "::notice::no release tag yet; assembling the repository from HEAD as a dry run"
  fi
  # A dry run restores nothing and packages HEAD, which no tag names.
  publish=dry-run
  build=true
  tag=
fi

{
  echo "tag=$tag"
  echo "fedora=$(printf '%s\n' "${releases[@]}" | jq -R . | jq -cs .)"
  echo "build=$build"
  echo "publish=$publish"
} >> "$out"
