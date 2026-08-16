"""
agent_delivery.py — how the agent SHIPS, as opposed to how it builds.

Until now "done" meant a `nohup node server.js &` on a port published by Compose. That is a
dev server: it dies with the container, nothing tests it but the agent, and nobody approved
it. Real delivery on this estate already exists — it was built for the calculator (see
../React_Typescript/github_ci_cd) — and this module teaches the agent to use it:

    agent builds and tests locally on 3000-3009        <- unchanged; this is DEV
    agent writes Dockerfile + k8s manifest + CI file
    agent pushes to github.com/<owner>/agent-<app>
    GitHub Actions (GitHub-hosted)  : test -> build -> push ghcr.io/<owner>/agent-<app>:<sha>
    ─────────────────────────────── EVERYTHING BELOW THIS LINE IS GONE ───────────────────
    ci-watcher pod                  : polls the Actions API -> ArtifactReady -> Kafka
    governance pod                  : SCAN -> CR -> AWAITING_APPROVAL  <- a HUMAN approves
                                      -> creates/【re】uses a Harness service+pipeline
    Harness delegate                : rolling deploy into namespace `agent-apps`
    agent-app-proxy                 : 3100N -> kind node NodePort 3000N  -> a browser URL

THE DEPLOY LEG IS OFFLINE AS OF 2026-08-16, AND THE PIPELINE REALLY DOES STOP AT THE IMAGE.
Kafka/redpanda were stopped and github_ci_cd's governance app was repurposed, so the four
steps struck through above no longer run — an image is built and then nothing collects it.

Being replaced by a fleet control plane (registry on hp-tiger:8091, a per-box daemon, a
reconciler), where the whole middle collapses into one call: ship_app registers the app, the
control plane assigns the box, the NodePort and the URL, and emails the live address itself.
The agent will stop doing port arithmetic entirely — see `ports_for` below, which is the
thing that goes.

WHAT THAT MEANS FOR THIS FILE TODAY. The prose in `delivery_note` was changed to say the
deployment step is offline and to forbid reporting a cluster URL, and the same correction is
in agent_brain's system prompt and in ship_app's post-build output. All three are marked
TEMPORARY and all three must be reverted together when the new leg lands — they are one
story told in three places, and a pipeline described two different ways is how an agent ends
up inventing a third.

Why it was worth changing rather than waiting: the URL was pure arithmetic
(`PROXY_PORT_BASE + slot`), so it could be produced whether or not anything was listening.
An agent that emails a computed address for an app nothing deployed has written a plausible
false sentence a human will act on — the exact failure the no-token branch of
`delivery_note` was written to prevent.

THE NUMBERS. Each app owns ONE index N in 0..9 and gets three ports from it:

    local dev   3000 + N     published by Compose from the agent's own container
    NodePort   30000 + N     on the kind node, inside the cluster
    browser    31000 + N     published on Zeenie by agent-app-proxy   <- what a human opens

N belongs to the APP, not the task: it must survive a redeploy, so it lives in the agent's
own AGENT-ASSETS.md and in the committed manifest. The harness does not assign it — it has
no view of the cluster, and inventing a second source of truth is how the `.env` that
nothing reads got written. The agent picks the lowest N not already claimed in its notes,
and a collision surfaces as a Kubernetes error it can then fix.

WHY THE CI FILE IS HARNESS-SUPPLIED AND THE MANIFEST IS NOT. The workflow is identical in
every repo and must be exactly right — a typo there fails after the push, in a log the agent
cannot see. So it is emitted verbatim from here. The manifest genuinely differs per app
(port, health path, resources), so it is offered as a worked example to adapt.
"""

import os
import re

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "df360-net")
REGISTRY = os.environ.get("REGISTRY", "ghcr.io")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "agent-apps")
APP_HOST = os.environ.get("APP_HOST", "192.168.0.105")

NODE_PORT_BASE = int(os.environ.get("NODE_PORT_BASE", "30000"))
PROXY_PORT_BASE = int(os.environ.get("PROXY_PORT_BASE", "31000"))
# How many apps can live in the cluster at once. Deliberately NOT the same number as
# APP_PORT_COUNT, which counts local preview ports: those are capped at ten by what Compose
# publishes from the agent's container, whereas a cluster slot costs only a NodePort (the
# range runs to 32767) and a listener in agent-app-proxy. Conflating them meant raising one
# silently promised the other. Raising this needs the proxy republished over the wider range.
APP_SLOTS = int(os.environ.get("APP_SLOT_COUNT", "20"))


def slug(name, limit=30):
    """A name that is legal as a GitHub repo, a Docker tag AND a Kubernetes object at once.
    Kubernetes is the strictest: RFC 1123, lowercase alphanumeric and '-', must start and end
    alphanumeric. Satisfy that and the other two follow."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower()
    s = s[:limit].strip("-")
    return s or "app"


def repo_name(app):
    return f"agent-{slug(app)}"


def image_name(app):
    return f"{REGISTRY}/{GITHUB_OWNER}/{repo_name(app)}"


def ports_for(index):
    """The ports app-slot `index` owns — in the CLUSTER only.

    It does NOT own a local preview port. That number comes from agent_notes.free_port, which
    picks whatever is actually free in the published range at the moment the task starts, and
    the task notes state it. An earlier version of this function also returned `dev` =
    3000+index, so a task could be told "listen on 3000" by the machine notes and "test on
    3001" by the delivery note in the same message. One number, one source: the slot is about
    where the app lands in Kubernetes, not where the agent runs it while building.
    """
    return {
        "node": NODE_PORT_BASE + index,
        "proxy": PROXY_PORT_BASE + index,
        "url": f"http://{APP_HOST}:{PROXY_PORT_BASE + index}",
    }


def index_of_node_port(node_port):
    return int(node_port) - NODE_PORT_BASE


# ---- The CI workflow: identical in every agent repo, so the harness owns it ----------
# Deliberately GitHub-HOSTED. The calculator's pipeline ends on a self-hosted runner because
# it has to reach the LAN Kafka broker — but that runner is repo-scoped and `df360-net` is a
# user account, not an org, so GitHub will not let a runner be shared across repos. Polling
# from inside the cluster (ci_watcher.py) removes the need entirely: CI stays in the cloud
# and nothing in GitHub needs a route to the LAN.
CI_WORKFLOW = """\
# Built by agent1. Identical in every agent app repo — the pipeline is not the deliverable.
#
# Runs on GitHub-hosted runners only: nothing here needs LAN access. A watcher pod inside the
# kind cluster polls this workflow's result and starts the CD half (governance -> approval ->
# Harness). See docs/Autonomous-Agents-Design.md.
name: CI

on:
  push:
    branches: [main]
  workflow_dispatch:

env:
  IMAGE: ghcr.io/__OWNER__/__REPO__

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      # ci/test.sh is the app's own test command. Absent = nothing to run, which is honest
      # for a static page and a failure for anything else — the reviewer checks that.
      - name: Run tests
        run: |
          if [ -x ci/test.sh ]; then
            bash ci/test.sh
          elif [ -f ci/test.sh ]; then
            bash ci/test.sh
          else
            echo "::warning::no ci/test.sh in this repo - nothing was tested"
          fi

  build-and-push:
    needs: test
    runs-on: ubuntu-latest
    permissions:
      contents: read
      packages: write
    steps:
      - uses: actions/checkout@v4

      - name: Compute image tag
        id: meta
        run: echo "version=${GITHUB_SHA::7}" >> "$GITHUB_OUTPUT"

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}

      - name: Build and push
        uses: docker/build-push-action@v6
        with:
          context: .
          push: true
          tags: |
            ${{ env.IMAGE }}:${{ steps.meta.outputs.version }}
            ${{ env.IMAGE }}:latest

      - name: Summary
        run: |
          echo "Pushed ${IMAGE}:${{ steps.meta.outputs.version }}" >> "$GITHUB_STEP_SUMMARY"
"""


def ci_workflow(app):
    return CI_WORKFLOW.replace("__OWNER__", GITHUB_OWNER).replace("__REPO__", repo_name(app))


# ---- The manifest: a worked example to adapt, not a form to fill ---------------------
MANIFEST_EXAMPLE = """\
# k8s/deployment.yaml — Deployment + NodePort Service.
#
# WRITE A REAL IMAGE TAG HERE. This manifest used to carry a Go template expression instead of
# a tag, because a deployer rendered it against the exact build being released. That deployer
# is gone (see the module docstring), so nothing renders anything now: a template expression
# left in this file is not "filled in later", it is a literal string that no cluster can pull.
# Its replacement takes the image from a registration call, not from this file, which makes
# this manifest a description of what you built rather than the instruction that deploys it.
# Put the tag `ship_app status` printed on a green build.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {app}
  namespace: {ns}
  labels: {{ app: {app}, managed-by: agent1 }}
spec:
  # ONE replica by default, on purpose. Two pods do not share a filesystem, a SQLite file or
  # an in-process WebSocket hub — they are two separate copies of the application, and a user
  # gets whichever one their connection happens to land on. Raise this to 2 only if every
  # request can be served by any pod with no shared state; `ship_app push` blocks the
  # combination of replicas > 1 and a local database, because that has shipped broken twice.
  replicas: 1
  selector:
    matchLabels: {{ app: {app} }}
  template:
    metadata:
      labels: {{ app: {app} }}
    spec:
      # The image is in a PRIVATE registry, so the cluster needs credentials to pull it.
      # Leave this in — without it every pod stops at ImagePullBackOff and the failure looks
      # like a broken image rather than a missing secret.
      imagePullSecrets:
        - name: ghcr-cred
      containers:
        - name: {app}
          image: {registry}/{owner}/agent-{app}:<the-sha-from-ship_app-status>
          ports:
            - containerPort: {container_port}
          env:
            - name: PORT
              value: "{container_port}"
          readinessProbe:
            httpGet: {{ path: {health}, port: {container_port} }}
            initialDelaySeconds: 3
            periodSeconds: 5
          livenessProbe:
            httpGet: {{ path: {health}, port: {container_port} }}
            initialDelaySeconds: 10
            periodSeconds: 10
          resources:
            requests: {{ cpu: 50m, memory: 64Mi }}
            limits: {{ cpu: 500m, memory: 256Mi }}
---
apiVersion: v1
kind: Service
metadata:
  name: {app}
  namespace: {ns}
  labels: {{ app: {app}, managed-by: agent1 }}
spec:
  type: NodePort
  selector: {{ app: {app} }}
  ports:
    - port: 80
      targetPort: {container_port}
      nodePort: {node_port}        # THIS is the number that must be unique across your apps
"""


def manifest_example(app, index, container_port=8080, health="/healthz"):
    return MANIFEST_EXAMPLE.format(
        app=slug(app), ns=K8S_NAMESPACE, container_port=container_port,
        health=health, node_port=ports_for(index)["node"],
        registry=REGISTRY, owner=GITHUB_OWNER,
    )


# ---- The multi-replica guard: a safety property in Python, not in the prompt ----------
# Two apps in a row shipped `replicas: 2` over a SQLite file in the pod's own filesystem
# (url-shortener, then sprintflow). Both were "working" when the agent tested them, because a
# single client with one keep-alive connection stays pinned to one pod. Both were broken for
# real users: two pods, two databases, two in-process WebSocket hubs, and the Service hands
# out whichever one the next connection lands on. The url-shortener returned 404 for half the
# links it had just minted; sprintflow served two entirely different boards.
#
# The agent cannot be trusted to remember this and neither can a prompt line — it had already
# written the SQLite lesson into its own AGENT-AVOID.md and still did it. So the check runs
# here, at the one point of no return, and it reads the app's real dependencies rather than
# asking anybody's opinion.

# Only dependency declarations and imports are searched, never arbitrary source text: a
# pattern like r"\.db\b" would match `this.db` in every ORM in existence.
SQLITE_DEPS = (
    ("better-sqlite3", "better-sqlite3"),
    ("@libsql/client", "@libsql/client"),
    ("drizzle-orm/libsql", "drizzle-orm on libsql"),
    ("node:sqlite", "node:sqlite"),
    ("sqlite3", "sqlite3"),
    ("aiosqlite", "aiosqlite"),
    ("sqlalchemy", "SQLAlchemy (check its URL — sqlite:/// is local)"),
)
DEP_FILES = ("package.json", "requirements.txt", "pyproject.toml", "Pipfile", "go.mod", "Gemfile")
SQLITE_IN_CODE = re.compile(r"import\s+sqlite3\b|sqlite3\.connect\(|sqlite:///|file:[^\s\"']*\.(?:db|sqlite3?)\b")
DB_FILE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SKIP_DIRS = {"node_modules", ".git", "dist", "build", "__pycache__", ".venv", "venv"}


def _read(path, cap=200_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(cap)
    except OSError:
        return ""


def local_state_evidence(directory, max_files=400):
    """Signs that this app keeps its data inside its own container.

    Deliberately narrow. A false positive blocks a legitimate push, so this looks only at
    declared dependencies, unambiguous connect calls, and database files actually sitting on
    disk — not at prose, comments or variable names.
    """
    found = []
    for name in DEP_FILES:
        text = _read(os.path.join(directory, name))
        if not text:
            continue
        low = text.lower()
        for needle, label in SQLITE_DEPS:
            if needle.lower() in low:
                found.append(f"{label} (declared in {name})")
    seen = 0
    for root, dirs, files in os.walk(directory):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
        for fn in files:
            if fn.lower().endswith(DB_FILE_SUFFIXES):
                rel = os.path.relpath(os.path.join(root, fn), directory)
                found.append(f"{rel} — a database file in the image's own filesystem")
                continue
            if seen >= max_files or not fn.endswith((".py", ".js", ".ts", ".mjs", ".cjs")):
                continue
            seen += 1
            if SQLITE_IN_CODE.search(_read(os.path.join(root, fn), 60_000)):
                rel = os.path.relpath(os.path.join(root, fn), directory)
                found.append(f"a local SQLite connection in {rel}")
    # Order-stable dedupe: the message reads better without repeats, and every walk of the
    # same tree must produce the same text or the agent sees phantom changes between pushes.
    out = []
    for item in found:
        if item not in out:
            out.append(item)
    return out


def manifest_paths(directory):
    k8s = os.path.join(directory, "k8s")
    if not os.path.isdir(k8s):
        return []
    return sorted(os.path.join(k8s, f) for f in os.listdir(k8s)
                  if f.endswith((".yaml", ".yml")))


def declared_replicas(text):
    """The largest `replicas:` in a manifest, or None. Comments are stripped first so the
    explanatory note above the field cannot be mistaken for the field."""
    best = None
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        m = re.match(r"\s*replicas:\s*(\d+)\s*$", line)
        if m:
            n = int(m.group(1))
            best = n if best is None else max(best, n)
    return best


def has_shared_or_owned_storage(text):
    """True when the manifest already answers the question honestly — either every pod talks
    to storage outside itself, or it is a StatefulSet where owning a private volume is the
    point. Either way this guard has nothing to say."""
    return bool(re.search(r"kind:\s*StatefulSet|persistentVolumeClaim|volumeClaimTemplates",
                          text))


def replica_state_conflict(directory):
    """The guard. Returns an explanatory string to refuse the push with, or None to allow it."""
    manifests = manifest_paths(directory)
    if not manifests:
        return None
    for path in manifests:
        text = _read(path)
        n = declared_replicas(text)
        if n is None or n <= 1 or has_shared_or_owned_storage(text):
            continue
        evidence = local_state_evidence(directory)
        if not evidence:
            continue
        rel = os.path.relpath(path, directory)
        bullets = "\n".join(f"    - {e}" for e in evidence)
        return (
            f"refusing to push: {rel} asks for {n} replicas, but this app keeps its state "
            f"inside its own container.\n\n"
            f"  local state found:\n{bullets}\n\n"
            f"  Kubernetes will run {n} pods from that manifest. They do not share a "
            "filesystem, so they do not share that database, and they do not share "
            "in-process state such as a WebSocket hub. The Service hands each new connection "
            "to whichever pod it likes, so users are silently served different copies of the "
            "application with different data. Your own testing will not show it: one client "
            "holding one keep-alive connection stays pinned to one pod, which is exactly why "
            "this has shipped broken before.\n\n"
            "  Fix it one of these ways, then push again:\n"
            "    replicas: 1            correct for anything backed by SQLite or a local "
            "file. Choose this unless you have a specific reason not to.\n"
            "    a shared database      Postgres or similar, reached over the network by "
            "every pod, so there is one copy of the data.\n"
            "    StatefulSet + PVC      only when each pod is genuinely meant to own its own "
            "slice of the data.\n\n"
            "  Then write down what you chose and why in AGENT-AVOID.md."
        )
    return None


def claimed_indexes(assets_text):
    """Every app-slot index already claimed, read out of the agent's own notes.

    Looks for NodePort numbers rather than a field we impose — the agent owns the file's
    format (see agent_notes) and the one thing guaranteed to appear in it is the port. Used
    only to SUGGEST the next free slot; the cluster remains the authority on collisions.
    """
    # Match any five-digit NodePort-shaped number, then range-check. An earlier version
    # hardcoded `3000\d`, which silently stopped seeing claimed slots the moment the block
    # grew past ten — the agent would have been handed a slot already in its own notes.
    found = set()
    for m in re.finditer(r"\b(3\d{4})\b", assets_text or ""):
        idx = int(m.group(1)) - NODE_PORT_BASE
        if 0 <= idx < APP_SLOTS:
            found.add(idx)
    return found


def slot_deployed(index, timeout=1.5):
    """Is something actually deployed in cluster slot `index`? Asks the cluster, not the notes.

    A plain TCP connect is NOT enough here, unlike the local-port check in agent_notes:
    agent-app-proxy listens on all ten of its ports whether or not anything sits behind them,
    so every slot accepts a connection. What distinguishes them is whether any BYTES come
    back — the proxy closes the connection unanswered when the upstream NodePort is dead.
    So speak just enough HTTP to force a reply.

    Ground truth over bookkeeping, same rule as §8: the notes say what a slot is FOR, the
    machine says whether it is TAKEN. This is how a slot claimed by something the agent did
    not deploy (the reference probe on slot 0) still gets respected.
    """
    import socket
    try:
        with socket.create_connection((APP_HOST, PROXY_PORT_BASE + index), timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"GET / HTTP/1.0\r\nHost: slotcheck\r\n\r\n")
            return bool(s.recv(1))
    except OSError:
        return False


def suggest_index(assets_text, check_cluster=True):
    taken = claimed_indexes(assets_text)
    for i in range(APP_SLOTS):
        if i in taken:
            continue
        if check_cluster and slot_deployed(i):
            continue
        return i
    return None


def delivery_note(assets_text, has_token):
    """The delivery half of the task notes. Only appended when the agent can actually push —
    telling it to ship with no credentials would produce a confident lie about having done so.
    """
    if not has_token:
        return (
            "DELIVERY IS UNAVAILABLE for this task: no GitHub token is configured in this "
            "container, so you cannot push. Build and run the app locally as usual, and say "
            "plainly in your reply that it was not shipped to the cluster and why. Do not "
            "claim to have pushed anything."
        )
    idx = suggest_index(assets_text)
    if idx is None:
        return (
            f"DELIVERY IS UNAVAILABLE for this task: all {APP_SLOTS} app slots "
            f"({NODE_PORT_BASE}-{NODE_PORT_BASE + APP_SLOTS - 1}) are already claimed in your "
            "notes. Build and test locally, say so in your reply, and tell the reader which "
            "existing app would have to be retired to free a slot."
        )
    p = ports_for(idx)
    return (
        "HOW WORK GETS SHIPPED HERE. Running a server yourself is how you TEST something. It "
        "is not how you deliver it: that server dies with this container, nobody reviewed it "
        "and nobody approved it. If this task produced an application that should outlive the "
        "task, ship it down the real pipeline:\n"
        "\n"
        "  1. Build and test it locally first, on the port the machine notes above gave you. "
        "Do not skip this — it is much cheaper to find a bug here than after a build.\n"
        f"  2. Write a Dockerfile that runs it. It must listen on the port given by the PORT "
        "environment variable, because Kubernetes sets that, not you.\n"
        "  3. Write ci/test.sh — the command that proves it works, exiting non-zero on "
        "failure. CI runs it before building anything. A repo with no ci/test.sh ships "
        "untested and says so in the build log.\n"
        f"  4. Write k8s/deployment.yaml. Your app slot is {idx}, so your NodePort is "
        f"{p['node']} — that number must not collide with an app already in your notes.\n"
        "\n"
        "Use the `ship_app` command for the GitHub half — do not hand-roll git remotes, "
        "tokens or the workflow file. It is on your PATH:\n"
        f"     ship_app scaffold <app-name> <dir>   writes .github/workflows/ci.yml (correct, "
        f"verbatim) and a k8s/deployment.yaml skeleton for app slot {idx}\n"
        "     ship_app push <app-name> <dir>       creates the repo if needed, commits, "
        "pushes main, prints the Actions run URL\n"
        "     ship_app status <app-name>           the latest CI run: queued / in_progress / "
        "success / failure, with the image tag on success\n"
        "     ship_app logs <app-name>             the failing job's log, when it fails\n"
        "   Run `ship_app --help` before the first one. Scaffold first, then write your own "
        "Dockerfile and ci/test.sh, then push, then poll status until it settles.\n"
        "\n"
        "Do not report success until `ship_app status` actually says success. If it fails, "
        "read the log, fix it, and push again — a red build is not delivery.\n"
        "\n"
        # TEMPORARY — REMOVE WHEN THE FLEET CONTROL PLANE'S DEPLOY LEG LANDS.
        # The chain this paragraph used to describe (approve in the dashboard, Harness rolls
        # it out, answer at a computed URL) no longer exists: Kafka/ci-watcher/Harness were
        # retired and the app that drove them was repurposed. The replacement — register with
        # the fleet control plane, which assigns the box, the NodePort and the URL, and emails
        # the live address itself — is being built. Until it does, the pipeline genuinely
        # stops at the image, and the agent must be told so.
        #
        # Saying nothing was not an option. The URL was arithmetic, `PROXY_PORT_BASE + slot`,
        # so it could always be produced whether or not anything was listening on it — and an
        # agent that offers a computed address for an app nothing deployed has written a
        # plausible sentence that is false, in an email a human will act on. Same reason the
        # no-token branch above exists at all.
        "AFTER THAT IT IS OUT OF YOUR HANDS, AND RIGHT NOW IT STOPS THERE. The deployment "
        "step is offline: the system that used to take a built image and run it in the "
        "cluster is being replaced, and its replacement is not finished. CI still builds and "
        "publishes your image, and that is real work that will be deployed once the new "
        "pipeline lands — but nothing deploys it today.\n"
        "\n"
        "So: DO NOT GIVE ANYONE A CLUSTER URL for this app. Not as a live address, not as a "
        "pending one. There is no address to give, and a URL in an email is a promise someone "
        "will click. Say plainly that the image is built and waiting for deployment, which "
        "is the honest and complete answer. The local preview URL is still worth offering, "
        "clearly labelled as a preview that dies with this container.\n"
        "\n"
        f"Your repo will be github.com/{GITHUB_OWNER}/agent-<app-name> and your image "
        f"{REGISTRY}/{GITHUB_OWNER}/agent-<app-name>. Record in AGENT-ASSETS.md: repo, image, "
        f"app slot {idx}, NodePort {p['node']}, and that it is BUILT BUT NOT DEPLOYED. Do not "
        "record a URL for it — a URL in your notes is one you will repeat as fact in a later "
        "task, long after you have forgotten it was never true.\n"
        "\n"
        "--- .github/workflows/ci.yml (copy verbatim, replacing __REPO__ handling is already "
        "done for you once you know the app name) ---\n"
        "(the exact file is written for you by `ship_app --scaffold <app-name>`; run that "
        "rather than typing it out)\n"
        "\n"
        "--- k8s/deployment.yaml — a worked example for app slot "
        f"{idx}; adapt the port and health path to your app ---\n"
        f"{manifest_example('your-app-name', idx)}"
    )
