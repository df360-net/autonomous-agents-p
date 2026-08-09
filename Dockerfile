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
COPY *.py /app/
# The tests ship too. They are the only way to check a rebuilt image before trusting it, and
# copying them in by hand after every rebuild has the same "remember to" problem as above.
COPY tests/ /app/tests/

# `ship_app` is the agent's only sanctioned route to GitHub — a command on PATH rather than
# instructions to hand-roll git remotes and API calls. See ship_app.py for why.
RUN printf '#!/bin/sh\nexec python3 /app/ship_app.py "$@"\n' > /usr/local/bin/ship_app \
    && chmod +x /usr/local/bin/ship_app \
    && git config --global user.name  "agent1" \
    && git config --global user.email "agent1@agents.local" \
    && git config --global init.defaultBranch main \
    && git config --global --add safe.directory '*'

RUN mkdir -p /workspace
ENV WORKSPACE_ROOT=/workspace

# -u so docker logs stream live — watching a run is half the point.
CMD ["python", "-u", "agent_worker.py"]
