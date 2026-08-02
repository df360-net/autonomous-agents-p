#!/usr/bin/env python3
"""
ship_app — the agent's interface to GitHub. Installed on PATH inside the container.

Why a command instead of instructions. Everything here is exact and unforgiving: a repo
name, a remote URL with a token in it, an API path, a workflow file that must be byte-correct
or the build fails after the agent has stopped watching. That is harness work. The agent
decides WHAT to ship; this decides HOW, the same way every time.

    ship_app scaffold <app> <dir>   write .github/workflows/ci.yml + k8s/deployment.yaml
    ship_app push     <app> <dir>   create the repo if needed, commit, push main
    ship_app status   <app>         latest CI run: queued/in_progress/success/failure
    ship_app logs     <app>         the failing job's log
    ship_app list                   every agent-* repo and its latest CI result

Auth: GITHUB_TOKEN in the environment — a PAT with `repo` and `workflow` scope. The
`workflow` scope is not optional: without it GitHub rejects any push that touches
.github/workflows/, with a message that reads like a permissions bug rather than a missing
scope. The token is never printed, never written to disk, and never committed: it is passed
to git through an askpass helper rather than baked into the remote URL, so it cannot end up
in .git/config for the next reader to find.
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_delivery as d

API = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")
OWNER = d.GITHUB_OWNER
GIT_NAME = os.environ.get("GIT_AUTHOR_NAME", "agent1")
GIT_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "agent1@agents.local")


def die(msg, code=1):
    print(f"ship_app: {msg}", file=sys.stderr)
    sys.exit(code)


def api(path, method="GET", body=None, accept="application/vnd.github+json"):
    if not TOKEN:
        die("GITHUB_TOKEN is not set in this container — delivery is unavailable.")
    url = path if path.startswith("http") else f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return json.loads(raw) if raw and accept.endswith("json") else raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        if e.code == 404:
            return None
        die(f"GitHub {method} {url} -> HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        die(f"cannot reach GitHub: {e.reason}")


def run(args, cwd=None, check=True, quiet=False, env=None):
    p = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, env=env)
    out = (p.stdout or "") + (p.stderr or "")
    if not quiet and out.strip():
        print(out.rstrip())
    if check and p.returncode != 0:
        die(f"`{' '.join(args)}` failed with exit {p.returncode}")
    return p.returncode, out


def git_env():
    """Hand the token to git via an askpass script rather than the remote URL.

    A token embedded in `origin` survives in .git/config, gets echoed by `git remote -v`,
    and lands in any log that prints the push command. This keeps it in a temp file that
    exists only for the life of the process.
    """
    env = dict(os.environ)
    helper = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False)
    helper.write("#!/bin/sh\ncase \"$1\" in *Username*) echo \"$GH_USER\";; *) echo \"$GH_PASS\";; esac\n")
    helper.close()
    os.chmod(helper.name, 0o700)
    env.update({
        "GIT_ASKPASS": helper.name,
        "GH_USER": OWNER,
        "GH_PASS": TOKEN,
        "GIT_TERMINAL_PROMPT": "0",          # never hang waiting for a password
        "GIT_AUTHOR_NAME": GIT_NAME, "GIT_COMMITTER_NAME": GIT_NAME,
        "GIT_AUTHOR_EMAIL": GIT_EMAIL, "GIT_COMMITTER_EMAIL": GIT_EMAIL,
    })
    return env, helper.name


# ---- commands ---------------------------------------------------------------
def cmd_scaffold(app, directory):
    repo = d.repo_name(app)
    idx = _index_for(app)
    os.makedirs(os.path.join(directory, ".github", "workflows"), exist_ok=True)
    os.makedirs(os.path.join(directory, "k8s"), exist_ok=True)

    wf = os.path.join(directory, ".github", "workflows", "ci.yml")
    with open(wf, "w", encoding="utf-8", newline="\n") as f:
        f.write(d.ci_workflow(app))
    print(f"wrote {wf}")

    mf = os.path.join(directory, "k8s", "deployment.yaml")
    if os.path.exists(mf):
        print(f"kept  {mf} (already exists — not overwritten)")
    else:
        with open(mf, "w", encoding="utf-8", newline="\n") as f:
            f.write(d.manifest_example(app, idx))
        print(f"wrote {mf}   <-- EDIT THIS: containerPort, health path, replicas")

    p = d.ports_for(idx)
    print(f"\napp      {d.slug(app)}\nrepo     github.com/{OWNER}/{repo}\n"
          f"image    {d.image_name(app)}\napp slot {idx}\n"
          f"NodePort {p['node']}\nURL      {p['url']}   (only after a human approves the release)")
    print("\nStill yours to write: Dockerfile (must honour $PORT) and ci/test.sh.")


def _index_for(app):
    """Slot number. Explicit env wins; otherwise read it back out of an existing manifest so
    `scaffold` is idempotent; otherwise ask the agent's own notes for the next free one."""
    if os.environ.get("APP_SLOT"):
        return int(os.environ["APP_SLOT"])
    try:
        import agent_notes
        with open(agent_notes.assets_path(), encoding="utf-8", errors="replace") as f:
            idx = d.suggest_index(f.read())
        if idx is not None:
            return idx
    except Exception:
        pass
    return 0


def _ensure_repo(repo):
    existing = api(f"/repos/{OWNER}/{repo}")
    if existing:
        print(f"repo github.com/{OWNER}/{repo} already exists")
        return existing
    print(f"creating github.com/{OWNER}/{repo} ...")
    created = api("/user/repos", "POST", {
        "name": repo,
        "description": "Built and maintained by agent1.",
        "private": True,
        "has_issues": False, "has_wiki": False, "has_projects": False,
        "auto_init": False,
    })
    if not created:
        die(f"could not create {repo}")
    return created


def cmd_push(app, directory, message=None):
    repo = d.repo_name(app)
    if not os.path.isdir(directory):
        die(f"{directory} is not a directory")
    if not os.path.exists(os.path.join(directory, "Dockerfile")):
        die("no Dockerfile in that directory — CI has nothing to build. Write one first.")
    if not os.path.exists(os.path.join(directory, ".github", "workflows", "ci.yml")):
        die("no .github/workflows/ci.yml — run `ship_app scaffold` first.")
    # Refuse a manifest that would deploy several pods over a database only one of them can
    # see. This is a property of the delivery system, not advice: two apps shipped that exact
    # bug while their author believed they had tested it. See agent_delivery.replica_state_conflict.
    conflict = d.replica_state_conflict(directory)
    if conflict:
        die(conflict)

    _ensure_repo(repo)
    env, helper = git_env()
    try:
        if not os.path.isdir(os.path.join(directory, ".git")):
            run(["git", "init", "-b", "main"], cwd=directory, env=env)
        _write_gitignore(directory)
        run(["git", "add", "-A"], cwd=directory, env=env)
        rc, _ = run(["git", "diff", "--cached", "--quiet"], cwd=directory,
                    check=False, quiet=True, env=env)
        if rc == 0:
            print("nothing to commit — the working tree matches the last commit")
        else:
            run(["git", "commit", "-m", message or f"agent1: update {d.slug(app)}"],
                cwd=directory, env=env)
        url = f"https://github.com/{OWNER}/{repo}.git"
        rc, _ = run(["git", "remote", "set-url", "origin", url], cwd=directory,
                    check=False, quiet=True, env=env)
        if rc != 0:
            run(["git", "remote", "add", "origin", url], cwd=directory, env=env)
        run(["git", "push", "-u", "origin", "main"], cwd=directory, env=env)
    finally:
        try:
            os.unlink(helper)
        except OSError:
            pass

    print(f"\npushed to github.com/{OWNER}/{repo}")
    print(f"actions: https://github.com/{OWNER}/{repo}/actions")
    print("CI has started. Poll it with:  ship_app status " + d.slug(app))


def _write_gitignore(directory):
    path = os.path.join(directory, ".gitignore")
    if os.path.exists(path):
        return
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("node_modules/\n__pycache__/\n*.pyc\ndist/\nbuild/\n.env\n*.log\n*.db\n"
                "data.db\nserver.log\n.DS_Store\n")
    print(f"wrote {path}")


def _latest_run(repo):
    runs = api(f"/repos/{OWNER}/{repo}/actions/runs?per_page=1")
    if not runs or not runs.get("workflow_runs"):
        return None
    return runs["workflow_runs"][0]


def cmd_status(app):
    repo = d.repo_name(app)
    run_ = _latest_run(repo)
    if not run_:
        print(f"{repo}: no CI run yet (the push may still be registering — try again in ~10s)")
        return 2
    status, concl = run_.get("status"), run_.get("conclusion")
    sha = (run_.get("head_sha") or "")[:7]
    print(f"repo       {OWNER}/{repo}")
    print(f"run        #{run_.get('run_number')}  {run_.get('html_url')}")
    print(f"commit     {sha}")
    print(f"status     {status}" + (f" -> {concl}" if concl else ""))
    if status != "completed":
        print("\nStill running. Poll again in ~20s; a first build is usually 1-3 minutes.")
        return 3
    if concl == "success":
        print(f"image      {d.image_name(app)}:{sha}")
        print("\nCI PASSED. The image is in the registry.")
        print("It is NOT deployed: a human must approve the release in the governance")
        print("dashboard, and only then does Harness roll it out. Say that in your reply.")
        return 0
    print(f"\nCI FAILED ({concl}). Read the log:  ship_app logs {d.slug(app)}")
    return 1


def cmd_logs(app, tail=120):
    repo = d.repo_name(app)
    run_ = _latest_run(repo)
    if not run_:
        die("no CI run to read logs from")
    jobs = api(f"/repos/{OWNER}/{repo}/actions/runs/{run_['id']}/jobs") or {}
    for job in jobs.get("jobs", []):
        if job.get("conclusion") in (None, "success", "skipped"):
            continue
        print(f"===== failing job: {job['name']} =====")
        for step in job.get("steps", []):
            if step.get("conclusion") not in (None, "success", "skipped"):
                print(f"  failed at step: {step['name']}")
        text = api(f"/repos/{OWNER}/{repo}/actions/jobs/{job['id']}/logs",
                   accept="text/plain")
        if isinstance(text, str):
            lines = text.splitlines()
            print("\n".join(lines[-tail:]))
        return 1
    print("no failing job found — the run may still be going, or it passed")
    return 0


def cmd_list():
    repos = api("/user/repos?per_page=100&sort=updated") or []
    mine = [r for r in repos if r["name"].startswith("agent-")]
    if not mine:
        print("no agent-* repos yet")
        return 0
    for r in mine:
        run_ = _latest_run(r["name"])
        state = "no runs"
        if run_:
            state = run_.get("conclusion") or run_.get("status")
        print(f"  {r['name']:<34} {state:<12} {r['html_url']}")
    return 0


USAGE = __doc__


def main(argv):
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    cmd, rest = argv[1], argv[2:]
    if cmd == "scaffold":
        if len(rest) < 2:
            die("usage: ship_app scaffold <app> <dir>")
        return cmd_scaffold(rest[0], rest[1]) or 0
    if cmd == "push":
        if len(rest) < 2:
            die("usage: ship_app push <app> <dir> [message]")
        return cmd_push(rest[0], rest[1], rest[2] if len(rest) > 2 else None) or 0
    if cmd == "status":
        if not rest:
            die("usage: ship_app status <app>")
        return cmd_status(rest[0])
    if cmd == "logs":
        if not rest:
            die("usage: ship_app logs <app>")
        return cmd_logs(rest[0])
    if cmd == "list":
        return cmd_list()
    die(f"unknown command {cmd!r}. Run `ship_app --help`.")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
