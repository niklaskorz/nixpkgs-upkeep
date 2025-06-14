#!/usr/bin/env nix-shell
#!nix-shell -i python3 -p "python3.withPackages(ps: with ps; [ semver githubkit ])"
import os
import subprocess
import json
import sys
import textwrap
from pathlib import Path

import semver
from githubkit import GitHub

# Note: for the time being all PRs target master, but this is configurable should the need arise to target eg staging.
TARGET_BRANCH = "master"
BOT_USER = "botnk"
BOT_EMAIL = "github-botnk@korz.dev"
TEMPLATE_MARKER = "<!-- BEGIN_TEMPLATE -->"

PACKAGE = os.environ["PACKAGE"]
PRE_VERSION = os.environ["PRE_VERSION"]
GH_TOKEN = os.environ["GH_TOKEN"]
GITHUB_WORKFLOW_URL = os.environ.get("GITHUB_WORKFLOW_URL", "")


def git(*args, **kwargs):
    ret = subprocess.call(["git", *args], **kwargs)
    if ret != 0:
        sys.exit(ret)


def git_diff_index_clean() -> bool:
    return subprocess.call(["git", "diff-index", "--quiet", "HEAD", "--"]) == 0


def nix_instantiate_eval(expr: str):
    out = subprocess.check_output(
        ["nix-instantiate", "--eval", "-E", "with import ./. {}; " + expr, "--json"]
    )
    return json.loads(out)


def semver_is_upgrade(old, new):
    return semver.compare(new, old) == 1


def search_existing_prs(gh: GitHub, package: str, new_version: str):
    resp = gh.rest.search.issues_and_pull_requests(
        q=f"{package} {new_version} org:NixOS repo:nixpkgs type:pr state:open in:title"
    )
    resp.raise_for_status()
    return resp.parsed_data


def search_base_prs(gh: GitHub, package: str, old_version: str):
    resp = gh.rest.search.issues_and_pull_requests(
        q=f"{package} {old_version} org:NixOS repo:nixpkgs type:pr state:open in:title author:{BOT_USER}"
    )
    resp.raise_for_status()
    return resp.parsed_data


def make_body(body: str, template: str) -> str:
    return body.strip() + "\n\n" + TEMPLATE_MARKER + "\n\n" + template.strip()


def extract_template(body: str) -> str | None:
    parts = body.split(TEMPLATE_MARKER, maxsplit=1)
    if len(parts) == 2:
        return parts[1]
    return None


def nix_build(package):
    try:
        output = subprocess.check_output(
            ["nix-build", "-A", package], stderr=subprocess.STDOUT
        ).decode()
        return True, output
    except subprocess.CalledProcessError as e:
        return False, e.output.decode()


def main():
    # Check that there's a diff from the updater script. See https://stackoverflow.com/questions/3878624/how-do-i-programmatically-determine-if-there-are-uncommitted-changes.
    if git_diff_index_clean():
        print("No diff after running updater.")
        return

    new_version = nix_instantiate_eval(f"lib.getVersion {PACKAGE}")
    changelog = nix_instantiate_eval(f"{PACKAGE}.meta.changelog")

    # Sometimes there's a diff but the version is still the same. For example this
    # happens when the hash has been changed to be in SRI format. In other cases you
    # can even get downgrade suggestions as in https://github.com/NixOS/nixpkgs/pull/197638.
    if not semver_is_upgrade(PRE_VERSION, new_version):
        print(f"{new_version} does not appear to be an upgrade from {PRE_VERSION}")
        return

    print(f"Updating {PACKAGE} from {PRE_VERSION} to {new_version}")

    gh = GitHub(GH_TOKEN)

    # Search to see if someone already created a PR for this version of the package.
    prs = search_existing_prs(gh, PACKAGE, new_version)
    prs_count = prs.total_count
    if prs_count > 0:
        print("There seems to be an existing PR for this change already:")
        for item in prs.items:
            print(item.pull_request.html_url)
        return

    # We need to set up our git user config in order to commit.
    git("config", "--global", "user.email", BOT_EMAIL)
    git("config", "--global", "user.name", BOT_USER)

    # We need to get a complete unshallow checkout if we're going to push to another
    # repo. See https://github.community/t/automating-push-to-public-repo/17742/11?u=samuela
    # and https://stackoverflow.com/questions/28983842/remote-rejected-shallow-update-not-allowed-after-changing-git-remote-url.
    # We start with only a shallow clone because it's far, far faster and it most
    # cases we don't ever need to push anything.
    git("fetch", "--refetch", "--filter=tree:0", "origin", TARGET_BRANCH)

    remote_url = f"https://{BOT_USER}:{GH_TOKEN}@github.com/{BOT_USER}/nixpkgs.git"
    git("remote", "add", "fork", remote_url)

    # Checkout the target branch first so that our commit has the right parent.
    git("switch", TARGET_BRANCH)
    branch = f"{PACKAGE}-{new_version}"
    git("switch", "-c", branch)

    commit_msg = f"{PACKAGE}: {PRE_VERSION} -> {new_version}\n\nChangelog: {changelog}"
    git("add", ".")
    git("commit", "-m", commit_msg)

    # Compose PR body
    template_path = Path(".github/PULL_REQUEST_TEMPLATE.md")
    pr_template = template_path.read_text()
    body = textwrap.dedent(f"""
        Upgrades {PACKAGE} from {PRE_VERSION} to {new_version}

        This PR was automatically generated by [nixpkgs-upkeep](https://github.com/niklaskorz/nixpkgs-upkeep).

        - Changelog: {changelog}
        - [CI workflow]({GITHUB_WORKFLOW_URL}) that created this PR.

        cc @niklaskorz
    """)

    # If there is a PR already updating from the same version, push to it
    prs = search_base_prs(gh, PACKAGE, PRE_VERSION)
    base_pr = next(
        (pr for pr in prs.items if pr.title.startswith(f"{PACKAGE}: {PRE_VERSION} -> ")),
        None,
    )
    if base_pr:
        print("Updating existing PR branch...")
        resp = gh.rest.pulls.get(
            owner="NixOS", repo="nixpkgs", pull_number=base_pr.number
        )
        resp.raise_for_status()
        pr = resp.parsed_data
        base_branch = pr.head.ref
        git("fetch", "--filter=tree:0", "fork", base_branch)
        git("switch", base_branch)
        base_version = nix_instantiate_eval(f"lib.getVersion {PACKAGE}")
        git("cherry-pick", "--strategy-option=theirs", branch)
        # cherry-pick has `--edit` but no way to specify the new message inline
        git(
            "commit",
            "--amend",
            "-m",
            f"{PACKAGE}: {base_version} -> {new_version}\n\nChangelog: {changelog}",
        )
        git("push", "--set-upstream", "fork", base_branch)
        print("Updating existing PR on NixOS/nixpkgs...")
        resp = gh.rest.pulls.update(
            owner="NixOS",
            repo="nixpkgs",
            pull_number=base_pr.number,
            title=f"{PACKAGE}: {PRE_VERSION} -> {new_version}",
            body=make_body(body, extract_template(pr.body) or pr_template),
        )
        resp.raise_for_status()
        pr_url = pr.html_url
        print(f"Updated PR: {pr_url}")
    else:
        print("Pushing new PR branch...")
        git("push", "--set-upstream", "fork", branch)
        print("Creating a new draft PR on NixOS/nixpkgs...")
        resp = gh.rest.pulls.create(
            owner="NixOS",
            repo="nixpkgs",
            head=f"{BOT_USER}:{branch}",
            base=TARGET_BRANCH,
            title=f"{PACKAGE}: {PRE_VERSION} -> {new_version}",
            body=make_body(body, pr_template),
            maintainer_can_modify=True,
            draft=True,
        )
        resp.raise_for_status()
        pr = resp.parsed_data
        pr_url = pr.html_url
        print(f"Created PR: {pr_url}")

    print("Running nix-build...")
    build_succeeded, build_log = nix_build(PACKAGE)

    if build_succeeded:
        body = (
            "nix-build was successful! Marking this PR as ready for review.\n\n"
            "<details>\n<summary>Complete build log</summary>\n\n"
            "```\n"
            f"> nix-build -A {PACKAGE}\n{build_log}\n"
            "```\n"
            "</details>\n"
        )
        resp = gh.rest.issues.create_comment(
            owner="NixOS",
            repo="nixpkgs",
            issue_number=pr.number,
            body=body,
        )
        resp.raise_for_status()
        gh.graphql(
            """
            mutation MarkPullRequestReadyForReview($pr: ID!) {
                markPullRequestReadyForReview(input: { pullRequestId: $pr }) {
                    clientMutationId
                }
            }
            """,
            {"pr": pr.node_id},
        )

        # TODO: run nixpkgs-review as well if nix-build succeeds
    else:
        abbreviated = "\n".join(build_log.splitlines()[-15:])
        body = (
            "nix-build failed. Leaving this PR as a draft for now. Push commits "
            'to this branch and mark as "ready for review" once the build issues have been resolved.\n\n'
            "Abbreviated log:\n"
            "```\n"
            f"> nix-build -A {PACKAGE}\n...\n{abbreviated}\n"
            "```\n\n"
            "<details>\n<summary>Complete build log</summary>\n\n"
            "```\n"
            f"> nix-build -A {PACKAGE}\n{build_log}\n"
            "```\n"
            "</details>\n"
        )
        resp = gh.rest.issues.create_comment(
            owner="NixOS",
            repo="nixpkgs",
            issue_number=pr.number,
            body=body,
        )
        resp.raise_for_status()


if __name__ == "__main__":
    main()
