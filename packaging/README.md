# Packaging

- [`rpm/`](rpm) holds the specs of `nm-sshuttle` (noarch) and
  `nm-sshuttle-gnome` (the editor for GNOME Settings, architecture-specific).
  The `rpm` job of the CI workflow builds and installs them for Fedora 43 and
  44 on every change.
- `../debian/` builds the Debian and Ubuntu packages.

## The dnf repository

<https://dennisklein.github.io/nm-sshuttle/> is a signed dnf repository on
GitHub Pages, for the Fedora releases listed in [`fedora-releases`](fedora-releases).
The [packages workflow](../.github/workflows/packages.yaml) builds a release
tag from source (`make-srpm.sh`, then one `rpmbuild --rebuild` per Fedora
release), signs the packages and the repository metadata (`publish.sh`),
installs from the assembled site in a Fedora container (`smoke-test.sh`)
and only then deploys it. The site is the repository's only state: each run
restores the published packages first, adds the new ones and prunes all but
the newest two versions (`prune-repo.py`). The
[`nm-sshuttle-release`](nm-sshuttle-release) package carries the `.repo`
file and the public key, so that users get both with one `dnf install`.

Pull requests that change this directory, and runs before the first tag or
before the signing key exist, assemble and smoke-test the same repository
with a throwaway key and deploy nothing.

### Setting it up

1. Run `scripts/signing-key.sh` on a trusted machine, from a checkout, with
   the `gh` CLI signed in as an administrator of the repository. It creates
   the `rpm-signing` environment, restricts it to `main`, stores the new
   secret key as its secret `RPM_SIGNING_KEY` and writes the public key to
   `packaging/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle`. Commit and push
   that file to `main`. Move the backup the script prints out of the machine.
2. In the repository's settings, under Pages, set the source to GitHub
   Actions.

### Releasing

Push an annotated tag whose version is the release's:

```console
$ git tag -a v0.1.0 -m 'nm-sshuttle 0.1.0'
$ git push origin v0.1.0
```

The [release workflow](../.github/workflows/release.yaml) starts the packages
workflow on `main`, the only branch that may use the signing key. The tag's
source is packaged under its version, whatever the specs in the tree say.
Tags with a hyphen (`v0.2.0-rc.1`) are not packaged. A change to a package
that is already published needs a new `Release:` in its spec, or a new
version: published packages are never replaced. Run the packages workflow by
hand to rebuild a tag (`tag`, `rebuild`), or, after a key compromise, to
publish from scratch (`discard-published`).

Rotating the key takes three steps, described at the top of
`scripts/signing-key.sh`.
