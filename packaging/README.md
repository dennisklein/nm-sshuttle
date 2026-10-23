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

1. In the repository's settings, under Pages, set the source to GitHub
   Actions.
2. Run `scripts/signing-key.sh` on a trusted machine, from a checkout, with
   the `gh` CLI signed in as an administrator of the repository. It creates
   the `rpm-signing` environment, restricts it and `github-pages` to `main`,
   stores the new secret key as the secret `RPM_SIGNING_KEY` of
   `rpm-signing` and writes the public key to
   `packaging/nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle`. Commit and push
   that file to `main`, with its fingerprint in the install section of
   `../README.md`. Move the backup the script prints out of the machine.
   `scripts/signing-key.sh --restrict` restricts the two environments again;
   run it once on a repository set up before it restricted `github-pages`,
   and after any change to the Pages settings.
3. Add a tag ruleset, so that only administrators can create, move or
   delete release tags (see below). In the repository's settings, under
   Rules, Rulesets, choose New ruleset, New tag ruleset, and set:
   - Ruleset name: `release-tags`; Enforcement status: Active.
   - Bypass list: Add bypass, Repository admin, Always allow.
   - Target tags: Add target, Include by pattern, `v*`.
   - Rules: Restrict creations, Restrict updates, Restrict deletions and
     Block force pushes.

   Then Create.

### Releasing

Merge the release's version into `main` first: `meson.build`,
`nm_sshuttle/__init__.py`, the two specs, `debian/changelog` and
`CHANGELOG.md`. Then tag `main` as GitHub has it, with an annotated tag
whose version is the release's:

```console
$ git fetch origin
$ git tag -a v0.1.0 -m 'nm-sshuttle 0.1.0' origin/main
$ git push origin v0.1.0
```

The [release workflow](../.github/workflows/release.yaml) starts the packages
workflow on `main`, the only branch that may use the signing key. The tag's
source is packaged under its version, whatever the specs in the tree say,
but only if the tag names a commit on `main`. Tags with a hyphen
(`v0.2.0-rc.1`) are not packaged. A change to a package that is already
published needs a new `Release:` in its spec, or a new version: published
packages are never replaced. Run the packages workflow by hand to rebuild a
tag (`tag`, `rebuild`), or, after a key compromise, to publish from scratch
(`discard-published`).

Rotating the key takes three steps, described at the top of
`scripts/signing-key.sh`.

### Tags are part of the trust boundary

The signing key is limited to workflow runs on `main`, but a run on `main`
signs whatever source the tag names, and anyone who can push a `v*` tag, or
start the packages workflow with a `tag` input, chooses the tag. `plan.sh`
therefore packages only a tag whose commit is on `main` (the plan job
fetches all of `main`'s history for that), and the source RPM job refuses
a tag that has moved since. So only source that went through `main`'s
branch rules is signed. A tag can still name an older commit of `main`
under a new version, or be moved or deleted: keep a tag ruleset that lets
only administrators create, update or delete `v*` tags. Tags are not
signed, and the pipeline does not check who made them; whoever has write
access to the repository can start a release of what is on `main`.

Releases therefore come only from `main`; a maintenance release cannot be
tagged on another branch. While the newest `vX.Y.Z` tag is off `main`,
every packages run fails, including the dry runs of pull requests and of
pushes to `main`. Delete such a tag to unblock them:

```console
$ git push origin :refs/tags/v0.1.1
```

### Container images

`publish` holds the signing key, so its image is pinned by digest, as
`quay.io/fedora/fedora:44@sha256:...`: a new image under the tag cannot run
next to the key. Fedora publishes its images on quay.io/fedora as well.
`srpm` and `release-rpm` run in the same image, since their output is
signed as built, and so does `dry-run`, so that a pull request with a new
digest tries it without the key.
The `rpm` and `smoke-test` matrix jobs keep
`registry.fedoraproject.org/fedora:<release>`, so that they build and test
against current Fedora releases; `rpm`'s packages are signed as built, so
the pinned `publish` image does not cover them. Dependabot's
`github-actions` updates cover the `uses:` lines only, not a job's
`container:` image, and this repository has no Dependabot or Renovate
configuration. Refresh the digest by hand, after a
Fedora update you want in the signing job and at least once a month:

```console
$ curl -fsSI -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json' \
    https://quay.io/v2/fedora/fedora/manifests/44 |
    grep -i '^docker-content-digest:'
```

That is the digest of the multi-architecture index. Put it after the tag in
the `image:` of `srpm`, `release-rpm`, `publish` and `dry-run`, and merge it
to `main` through a pull request.
