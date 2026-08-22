# The agent's container == the agent's sandbox. Everything it needs to BUILD software has
# to be in here, because there is no host to fall back on and no human to install things.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NODE_MAJOR=22

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg git bash procps lsof unzip zip jq sqlite3 \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && npm install -g typescript \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# The brain writes files as the agent; keep it off root's home but don't fight permissions
# on a single-tenant container — the blast radius is the container itself.
WORKDIR /app
# Every module, by wildcard. An explicit list was silently wrong for days: five new modules
# were added and hot-patched into the running container, so everything worked — right up until
# the first rebuild, which produced an image missing agent_budget and crash-looped on import.
# A list you have to remember to update is a list that will be out of date, and this one could
# only fail at the moment you were relying on it most.
# FLATTENED ON PURPOSE: agent/*.py lands directly in /app, not in /app/agent.
# The modules import each other as top-level names (`import agent_brain`), which is what makes
# them runnable, testable and hot-patchable without a package on sys.path. Copying the folder
# as a folder would mean either an __init__.py and a rewrite of every import in the fleet, or
# a PYTHONPATH that has to be right in the image, in compose, in the pod spec and in CI. The
# directory exists to organise the SOURCE tree; the container layout is unchanged by it.
COPY agent/*.py /app/
# The tests ship too. They are the only way to check a rebuilt image before trusting it, and
# copying them in by hand after every rebuild has the same "remember to" problem as above.
COPY tests/ /app/tests/

# `ship_app` is the agent's only sanctioned route to GitHub — a command on PATH rather than
# instructions to hand-roll git remotes and API calls. See ship_app.py for why.
# NO GLOBAL GIT IDENTITY IS BAKED IN. It used to say agent1, which was invisible while one
# machine built one image for one agent and becomes wrong the moment this single image runs as
# agent1 through agent4: every commit not made through agent_memory or ship_app — both of
# which pass identity per-invocation — would be attributed to agent1 regardless of who made
# it, in the one place that cannot be corrected afterwards. An image shared by four identities
# must not carry one of them.
RUN printf '#!/bin/sh\nexec python3 /app/ship_app.py "$@"\n' > /usr/local/bin/ship_app \
    && chmod +x /usr/local/bin/ship_app \
    && git config --global init.defaultBranch main \
    && git config --global --add safe.directory '*'

RUN mkdir -p /workspace /memory
# /workspace is SCRATCH now (D5): task folders and running apps, none of it precious.
# /memory holds the git clones of the agent's actual memory, and is rebuilt from the remote on
# every start — which is what makes the container disposable.
ENV WORKSPACE_ROOT=/workspace \
    MEMORY_ROOT=/memory

# -u so docker logs stream live — watching a run is half the point.
CMD ["python", "-u", "agent_worker.py"]
