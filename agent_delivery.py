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
    ci-watcher pod                  : polls the Actions API -> ArtifactReady -> Kafka
    governance pod                  : SCAN -> CR -> AWAITING_APPROVAL  <- a HUMAN approves
                                      -> creates/【re】uses a Harness service+pipeline
    Harness delegate                : rolling deploy into namespace `agent-apps`
    agent-app-proxy                 : 3100N -> kind node NodePort 3000N  -> a browser URL

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
APP_HOST = os.environ.get("APP_HOST", "192.168.0.21")

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
# The image is `{{{{.Values.image}}}}`, NOT a tag you write. Harness renders this manifest as a Go
# template against a values file holding the exact build being deployed, so a hardcoded tag
# here would be a lie the moment anything is released. Leave the expression alone.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {app}
  namespace: {ns}
  labels: {{ app: {app}, managed-by: agent1 }}
spec:
  replicas: 2
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
          image: {{{{.Values.image}}}}        # Harness renders this; do not hardcode a tag
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
    )


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
        "AFTER THAT IT IS OUT OF YOUR HANDS, and you must say so rather than implying the app "
        "is live. A human still has to approve the release in the governance dashboard, and "
        "only then does Harness deploy it. When it is approved it will answer at "
        f"{p['url']} — offer that as the eventual address, clearly marked as pending "
        "approval. Do not curl it and report it as down; it is not deployed yet.\n"
        "\n"
        f"Your repo will be github.com/{GITHUB_OWNER}/agent-<app-name> and your image "
        f"{REGISTRY}/{GITHUB_OWNER}/agent-<app-name>. Record all of it in AGENT-ASSETS.md: "
        f"repo, image, app slot {idx}, NodePort {p['node']}, URL {p['url']}, and the fact "
        "that the deploy is gated on human approval.\n"
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
