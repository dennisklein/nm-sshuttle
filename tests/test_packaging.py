# SPDX-License-Identifier: MIT
"""The release pipeline signs only tags on main."""

import http.server
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PACKAGING = ROOT / "packaging"
PUBKEY = PACKAGING / "nm-sshuttle-release" / "RPM-GPG-KEY-nm-sshuttle"

needs_tools = pytest.mark.skipif(
    not all(shutil.which(t) for t in ("git", "curl", "jq")), reason="needs git, curl and jq"
)


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


class NotFound(http.server.BaseHTTPRequestHandler):
    """An empty site: nothing is published yet."""

    def do_HEAD(self):
        self.send_response(404)
        self.end_headers()

    do_GET = do_HEAD

    def log_message(self, *args):
        pass


@pytest.fixture
def site():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NotFound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def repo(tmp_path):
    """A checkout with the planner, two commits on main (one tagged v1.0.0)
    and a v1.0.1 tag on a branch that never reached main."""
    repo = tmp_path / "repo"
    files = [
        "plan.sh",
        "make-srpm.sh",
        "fedora-releases",
        "rpm/nm-sshuttle-gnome.spec",
        "nm-sshuttle-release/nm-sshuttle-release.spec",
        "nm-sshuttle-release/RPM-GPG-KEY-nm-sshuttle",
    ]
    for f in files:
        (repo / "packaging" / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PACKAGING / f, repo / "packaging" / f)
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "test@invalid")
    git(repo, "config", "user.name", "test")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "tag.gpgsign", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "release")
    git(repo, "tag", "-a", "v1.0.0", "-m", "nm-sshuttle 1.0.0")
    git(repo, "checkout", "-q", "-b", "side")
    git(repo, "commit", "-q", "--allow-empty", "-m", "not reviewed")
    git(repo, "tag", "-a", "v1.0.1", "-m", "nm-sshuttle 1.0.1")
    git(repo, "checkout", "-q", "main")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    return repo


def plan(repo, site, **env):
    out = repo / "out"
    out.unlink(missing_ok=True)
    full = {
        k: v
        for k, v in os.environ.items()
        if not k.lower().endswith("_proxy") and not k.startswith("GITHUB_")
    }
    full.update(PAGES_URL=site, GITHUB_OUTPUT=str(out), GITHUB_REF="refs/heads/main")
    full.update(env)
    r = subprocess.run(
        [str(repo / "packaging" / "plan.sh")], env=full, capture_output=True, text=True
    )
    if not out.exists():
        return r, {}
    return r, dict(line.split("=", 1) for line in out.read_text().splitlines())


@needs_tools
def test_plan_publishes_a_tag_on_main(repo, site):
    r, out = plan(repo, site, INPUT_TAG="v1.0.0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out["tag"] == "v1.0.0"
    assert out["commit"] == git(repo, "rev-parse", "main")
    assert out["publish"] == "real"


@needs_tools
def test_plan_refuses_a_tag_off_main(repo, site):
    r, out = plan(repo, site, INPUT_TAG="v1.0.1")
    assert r.returncode == 1
    assert "not on main" in r.stdout
    assert out == {}


@needs_tools
def test_plan_refuses_a_newest_tag_off_main(repo, site):
    # Without an input, the newest tag is v1.0.1, which is not on main.
    r, out = plan(repo, site, INPUT_TAG="")
    assert r.returncode == 1
    assert "v1.0.1" in r.stdout and "not on main" in r.stdout


@needs_tools
def test_plan_accepts_a_tag_once_main_has_it(repo, site):
    git(repo, "merge", "-q", "--ff-only", "side")
    git(repo, "update-ref", "refs/remotes/origin/main", "main")
    r, out = plan(repo, site, INPUT_TAG="v1.0.1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out["commit"] == git(repo, "rev-parse", "v1.0.1^{commit}")


@needs_tools
def test_plan_needs_main_to_check_a_tag(repo, site):
    git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    r, _ = plan(repo, site, INPUT_TAG="v1.0.0")
    assert r.returncode == 1
    assert "origin/main is missing" in r.stdout


@needs_tools
def test_plan_dry_run_names_no_tag_or_commit(repo, site):
    r, out = plan(repo, site, INPUT_TAG="v1.0.0", GITHUB_REF="refs/pull/1/merge")
    assert r.returncode == 0, r.stdout + r.stderr
    assert out["tag"] == out["commit"] == ""
    assert out["publish"] == "dry-run"


def make_srpm(repo, *args):
    return subprocess.run(
        [str(repo / "packaging" / "make-srpm.sh"), str(repo / "out"), *args],
        capture_output=True,
        text=True,
    )


@needs_tools
def test_srpm_refuses_a_tag_moved_since_the_plan(repo):
    # The plan checked v1.0.0's commit; the tag now names another one.
    checked = git(repo, "rev-parse", "v1.0.0^{commit}")
    git(repo, "tag", "-f", "-a", "v1.0.0", "-m", "moved", "side")
    r = make_srpm(repo, "v1.0.0", checked)
    assert r.returncode == 1
    assert "has moved" in r.stderr
    assert not (repo / "out").exists()


@needs_tools
def test_srpm_refuses_an_empty_commit(repo):
    r = make_srpm(repo, "v1.0.0", "")
    assert r.returncode == 2


@pytest.mark.skipif(
    not all(shutil.which(t) for t in ("git", "rpmbuild")), reason="needs git and rpmbuild"
)
def test_srpm_builds_the_checked_commit(repo):
    for f in PACKAGING.glob("rpm/*.spec"):
        shutil.copy2(f, repo / "packaging" / "rpm")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "specs")
    r = make_srpm(repo, "v1.0.0", git(repo, "rev-parse", "v1.0.0^{commit}"))
    assert r.returncode == 0, r.stdout + r.stderr
    names = [p.name for p in (repo / "out").iterdir()]
    assert any(n.startswith("nm-sshuttle-1.0.0-") for n in names), names


def test_srpm_job_passes_the_checked_commit():
    wf = (ROOT / ".github/workflows/packages.yaml").read_text()
    assert "commit: ${{ steps.plan.outputs.commit }}" in wf
    assert "COMMIT: ${{ needs.plan.outputs.commit }}" in wf
    assert 'packaging/make-srpm.sh out ${TAG:+"$TAG" "$COMMIT"}' in wf


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
call = {"args": args}
if "--input" in args:
    call["input"] = json.load(sys.stdin)
with open(os.environ["GH_LOG"], "a") as f:
    f.write(json.dumps(call) + "\n")
if args[:2] == ["auth", "status"] or "--method" in args:
    sys.exit(0)
path = [a for a in args[1:] if a.startswith("repos/")][0]
env = path.split("/")[4]
if path.endswith("/deployment-branch-policies"):
    if env == "rpm-signing":
        print("1\tbranch\tmain\n2\ttag\tv*\n3\tbranch\tdev")
    sys.exit(0)
if env == "github-pages":
    print("gh: Not Found (HTTP 404)", file=sys.stderr)
    sys.exit(1)
print(json.dumps({"name": env, "protection_rules": [
    {"type": "required_reviewers", "prevent_self_review": True,
     "reviewers": [{"type": "User", "reviewer": {"id": 42, "login": "maintainer"}}]},
    {"type": "wait_timer", "wait_timer": 5},
    {"type": "branch_policy"}]}))
"""


@pytest.mark.skipif(
    not all(shutil.which(t) for t in ("git", "gpg", "jq")), reason="needs git, gpg and jq"
)
def test_signing_key_restricts_both_environments_to_main(tmp_path):
    # In a checkout of its own: the tests also run from a source tarball.
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(ROOT / "scripts/signing-key.sh", repo / "scripts")
    git(tmp_path, "init", "-q", str(repo))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    log = tmp_path / "gh.log"
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GH_LOG": str(log)}
    r = subprocess.run(
        [str(repo / "scripts/signing-key.sh"), "--restrict"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    puts = {c["args"][-3].split("/")[-1]: c["input"] for c in calls if "PUT" in c["args"]}
    branch_policy = {"protected_branches": False, "custom_branch_policies": True}
    # The reviewers and wait timer of an existing environment stay.
    assert puts["rpm-signing"] == {
        "wait_timer": 5,
        "prevent_self_review": True,
        "reviewers": [{"type": "User", "id": 42}],
        "deployment_branch_policy": branch_policy,
    }
    assert puts["github-pages"] == {"deployment_branch_policy": branch_policy}
    # Every policy but main's goes, and main gets one.
    changes = [c["args"][3:] for c in calls if "DELETE" in c["args"] or "POST" in c["args"]]
    policies = "repos/dennisklein/nm-sshuttle/environments/{}/deployment-branch-policies"
    assert changes == [
        ["DELETE", policies.format("rpm-signing") + "/2"],
        ["DELETE", policies.format("rpm-signing") + "/3"],
        ["POST", policies.format("github-pages"), "-f", "name=main", "-f", "type=branch"],
    ]
