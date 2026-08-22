"""
agent_delivery.py — how the agent SHIPS, as opposed to how it builds.

Until now "done" meant a `nohup node server.js &` on a port published by Compose. That is a
dev server: it dies with the container, nothing tests it but the agent, and nobody approved
it. Real delivery on this estate already exists — it was built for the calculator (see
../React_Typescript/github_ci_cd) — and this module teaches the agent to use it:

    agent builds and tests locally                     <- this is DEV, and dies with the pod
    agent writes Dockerfile + k8s manifest + CI file
    agent pushes to github.com/<owner>/agent-<app>
    GitHub Actions (GitHub-hosted)  : test -> build -> push ghcr.io/<owner>/agent-<app>:<sha>
    ship_app                        : REGISTERS the app with the fleet control plane
    control plane                   : assigns the box, the NodePort and the URL
    per-box daemon                  : renders Deployment + Service, runs the pod
    governance                      : emails the live address into the agent's own thread

WHAT THIS FILE STOPPED DOING, WHICH IS THE INTERESTING PART. Two pipelines have now been
retired underneath it. The first was Kafka -> ci-watcher -> approval -> Harness, which was
switched off; for a day the chain genuinely ended at the image and the prose here said so. The
second retirement is smaller and more useful: THE AGENT NO LONGER COMPUTES AN ADDRESS.

It used to. `http://{APP_HOST}:{PROXY_PORT_BASE + slot}` was arithmetic over two environment
variables, which means it produced a confident URL whether or not anything was listening on
it — and it did exactly that, in emails, for apps nothing had deployed. Nothing errored,
nothing was blank; the sentence was simply false. Both variables are gone from this module and
cannot come back: the control plane allocates the port and returns the address, so there is no
expression left here capable of inventing one.

WHERE THE STORY IS TOLD, AND WHY IT IS TOLD FOUR TIMES. `delivery_note` (the task notes),
`agent_brain`'s system prompt, `ship_app scaffold` and `ship_app status`. They must agree: an
agent handed two accounts of the same pipeline splits the difference and invents a third, and
`scaffold` is the FIRST thing it reads, so a stale line there outranks a correct one later.

THE ONE NUMBER LEFT. The manifest the agent commits still carries a NodePort, suggested by
`ports_for` from the app slot in its notes. It is a plausible description of the app, not an
allocation — the control plane assigns the real one from a range it tracks per box, which is
why a collision is now impossible rather than merely unlikely.

WHY THE CI FILE IS HARNESS-SUPPLIED AND THE MANIFEST IS NOT. The workflow is identical in
every repo and must be exactly right — a typo there fails after the push, in a log the agent
cannot see. So it is emitted verbatim from here. The manifest genuinely differs per app
(port, health path, resources), so it is offered as a worked example to adapt.
"""

import os
import re

import fleet_identity

GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "df360-net")
REGISTRY = os.environ.get("REGISTRY", "ghcr.io")
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "agent-apps")

# APP_HOST AND PROXY_PORT_BASE ARE GONE, AND THEIR ABSENCE IS THE POINT. They were the two
# halves of a computed URL — `http://{APP_HOST}:{PROXY_PORT_BASE + slot}` — which the agent
# could always produce whether or not anything was listening on it. The fleet control plane
# assigns the address now and reports it back, so there is nothing left here to compute and no
# way for this module to invent one. NODE_PORT_BASE survives only to SUGGEST a number for the
# manifest the agent writes; the plane allocates the real one.
NODE_PORT_BASE = int(os.environ.get("NODE_PORT_BASE", "30000"))
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


# The one definition of how long an image tag is. CI stamps the tag; anything that NAMES a tag
# is quoting CI, so both sides have to derive that length from here or they will disagree.
#
# They did disagree, and it broke every autonomous deploy: registration sent the full 40-char
# commit sha while CI had published the 7-char one, so the control plane pointed a Deployment at
# a tag that does not exist and the pod sat in ImagePullBackOff until a human retagged it by
# hand. Nothing failed loudly, because every individual step succeeded — CI went green, the
# registration was accepted, and the sha in the failing pull was a real commit.
IMAGE_TAG_LEN = 7


def image_tag(sha):
    """The tag CI will publish for commit `sha`, from the full sha.

    TRUNCATED HERE, NOT BY `git rev-parse --short`. git's short sha is not fixed at 7: with the
    default core.abbrev=auto it grows with the object count, so a repo that gets big enough
    starts abbreviating to 8 and the caller silently goes back to naming a tag that was never
    pushed. CI uses `${GITHUB_SHA::7}`, which is a plain string slice with no repo-dependent
    behaviour at all, so the only way to be certain of agreeing with it is to slice the same way.
    """
    return (sha or "").strip()[:IMAGE_TAG_LEN]


def ports_for(index):
    """A SUGGESTED NodePort for app-slot `index`. Nothing more, now.

    It used to return three things: a NodePort, a proxy port, and a URL built from them. The
    URL is gone because the control plane assigns the address, and the proxy port is gone with
    it — both were arithmetic this module had no business doing, since neither number was
    checked against anything that existed.

    What is left is a suggestion for the manifest the agent writes. The plane allocates the
    real NodePort from a range it tracks per box, so a collision is now impossible rather than
    merely unlikely; this number exists so the agent's committed manifest is a plausible
    description of its app instead of a blank.

    It does NOT own a local preview port. That comes from agent_notes.free_port, which picks
    whatever is actually free when the task starts.
    """
    return {"node": NODE_PORT_BASE + index}


def index_of_node_port(node_port):
    return int(node_port) - NODE_PORT_BASE


# ---- The CI workflow: identical in every agent repo, so the harness owns it ----------
# Deliberately GitHub-HOSTED. The calculator's pipeline ends on a self-hosted runner because
# it has to reach the LAN Kafka broker — but that runner is repo-scoped and `df360-net` is a
# user account, not an org, so GitHub will not let a runner be shared across repos. Polling
# from inside the cluster (ci_watcher.py) removes the need entirely: CI stays in the cloud
# and nothing in GitHub needs a route to the LAN.
CI_WORKFLOW = """\
# Built by an agent. Identical in every agent app repo — the pipeline is not the deliverable.
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

      # THE TAG THE WHOLE PIPELINE IS NAMED BY. The length is substituted from
      # agent_delivery.IMAGE_TAG_LEN, because ship_app has to name this exact string when it
      # registers the app for deployment and it cannot ask GitHub what was published. Changing
      # the 7 here by hand would leave that caller pointing at a tag nobody pushed.
      - name: Compute image tag
        id: meta
        run: echo "version=${GITHUB_SHA::__TAGLEN__}" >> "$GITHUB_OUTPUT"

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
    return (CI_WORKFLOW.replace("__OWNER__", GITHUB_OWNER)
            .replace("__REPO__", repo_name(app))
            .replace("__TAGLEN__", str(IMAGE_TAG_LEN)))


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
  labels: {{ app: {app}, managed-by: {managed_by} }}
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
          # THE ONE NUMBER THAT MATTERS IN THIS FILE. The control plane reads containerPort out
          # of here when you register, renders the real Deployment itself, and sets PORT to
          # this value in the running container. So your server must take its port from $PORT
          # and this number must be the port it actually serves — get them out of step and the
          # pod goes Ready while answering nothing, which looks like a broken app and is not.
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
  labels: {{ app: {app}, managed-by: {managed_by} }}
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
        # The AGENT, not the GitHub org. `managed-by: df360-net` would be true of every app in
        # the fleet and therefore useless; the point of the label is which agent to ask about it.
        managed_by=fleet_identity.AGENT_ID.replace("/", "."),
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


# slot_deployed() USED TO LIVE HERE, AND ITS REMOVAL IS PART OF THE SAME CHANGE.
#
# It answered "is anything actually deployed in cluster slot N?" by opening a TCP connection to
# `APP_HOST:PROXY_PORT_BASE + N` and speaking enough HTTP to see whether bytes came back. That
# was a good check — ground truth over bookkeeping, and it correctly respected a slot claimed by
# something the agent had not deployed itself.
#
# It cannot survive the control plane, for a reason worth stating rather than quietly deleting:
# the address it probed no longer exists. There is no single host publishing a contiguous block
# of proxy ports, because an app can now land on either box and the plane allocates its port.
# The probe would have had to guess which box to ask, which is precisely the guessing this whole
# migration removed.
#
# What replaces it is better: the plane KNOWS what is deployed and will refuse a colliding
# allocation, so the collision this defended against cannot happen. What is lost is small and
# should be admitted — the agent can no longer notice a slot occupied by something absent from
# its own notes, so `suggest_index` is now pure bookkeeping. If that bites, the fix is
# `fleet_register.app_status()`, not a socket.


def suggest_index(assets_text, check_cluster=False):
    """The lowest app slot this agent has not already claimed in its notes.

    `check_cluster` is accepted and ignored, kept so an old caller does not break. It used to
    trigger a live probe of the cluster; see the note above for why that is gone.
    """
    taken = claimed_indexes(assets_text)
    for i in range(APP_SLOTS):
        if i not in taken:
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
        "environment variable, because the platform sets that, not you.\n"
        "  3. Write ci/test.sh — the command that proves it works, exiting non-zero on "
        "failure. CI runs it before building anything. A repo with no ci/test.sh ships "
        "untested and says so in the build log.\n"
        "\n"
        "     ONE PORT, READ FROM ONE PLACE. If ci/test.sh starts your server and then curls "
        "it, both halves must use the SAME port, and the way to guarantee that is to set it "
        "once at the top and let everything read it:\n"
        "         PORT=\"${PORT:-8080}\"; export PORT\n"
        "         node server.js & SERVER=$!\n"
        "         trap 'kill $SERVER' EXIT\n"
        "         curl -fsS \"http://localhost:$PORT/healthz\"\n"
        "     A literal port number written into the curl line is the bug to avoid: your "
        "server takes its port from $PORT, so the moment the two disagree the test fails "
        "against a port nothing is listening on, and the error it prints — connection refused "
        "— reads exactly like a server that crashed on startup. That has already cost one "
        "build a long detour into debugging a server that was working perfectly.\n"
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
        # The deploy leg has LANDED: the plane assigns the box, port and address, deploys when
        # the image finishes, and emails the live URL into this task's thread. The paragraph
        # that used to say the pipeline stopped at the image is gone with it.
        #
        # EXACTLY ONE NARRATOR EACH, and that is what the text below is arranging. The agent
        # reports what it did — built, tested, pushed, registered. Governance reports what it
        # observed — the address, once a pod is actually serving on it. Neither can report the
        # other's half honestly: the agent cannot know the app is live, and the plane cannot
        # know what the agent was trying to build. The old wording had the agent computing
        # `PROXY_PORT_BASE + slot`, which produced a confident address whether or not anything
        # was listening — the failure this split exists to make impossible.
        "AFTER THAT IT IS OUT OF YOUR HANDS, AND THAT IS THE DESIGN. `ship_app push` also "
        "REGISTERS the app with the fleet control plane, which decides which machine runs it, "
        "which port it gets and what its address will be. You do not choose any of those and "
        "you cannot compute them — an agent working out its own URL is how an app gets "
        "announced at an address nothing is listening on.\n"
        "\n"
        "The control plane deploys the app once CI finishes building the image, and then "
        "EMAILS THE LIVE ADDRESS ITSELF, as a reply to the same conversation you are "
        "answering. So the follow-up reaches the person who asked, without you.\n"
        "\n"
        "SO DO NOT PUT A CLUSTER URL IN YOUR REPLY. `ship_app` will print the address the "
        "fleet assigned, and it is not live yet — the pod does not exist until the image "
        "does. A URL in an email is a promise someone will click. Say the app is built and "
        "registered and that the live address will follow; that is the complete and honest "
        "answer. Your local preview URL is still worth offering, clearly labelled as a "
        "preview that dies with this container.\n"
        "\n"
        f"Your repo will be github.com/{GITHUB_OWNER}/agent-<app-name> and your image "
        f"{REGISTRY}/{GITHUB_OWNER}/agent-<app-name>. Record in AGENT-ASSETS.md: repo, image, "
        "and that it is registered with the fleet. Do not record a URL — the fleet owns the "
        "address, it can change when an app moves box, and a URL in your notes is one you "
        "will repeat as fact in a later task long after it stopped being true.\n"
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
