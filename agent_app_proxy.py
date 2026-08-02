"""
agent_app_proxy.py — make a pod in the kind cluster openable in a browser.

THE PROBLEM. kind fixes its published host ports at cluster-creation time via
`extraPortMappings`, and this cluster was created with none that survive — `docker inspect`
shows exactly one entry, `6443/tcp`, with an EMPTY binding (Windows reserved the port range
kind chose). Every Service in the cluster is ClusterIP. So an app Harness has just deployed
is running perfectly and is reachable from precisely nowhere. Recreating the cluster with new
mappings would fix it and throw away a month of state; that trade is not worth making.

THE FIX, already proven on this box by kube_api_proxy.py. A container attached to the `kind`
docker network can reach the kind node by name, and a NodePort Service is published on the
node's own interfaces — so `learn-control-plane:30000` is an ordinary TCP endpoint from a
sibling container. This proxy sits there and republishes a block of them on ports Docker
publishes normally.

    browser -> 192.168.0.21:3100N -> [this container] -> learn-control-plane:3000N -> pod

One process, one port block, N listeners. Adding an app needs no change here: the agent
picks NodePort 3000N in its manifest and the matching 3100N is already listening. A slot
with nothing behind it simply refuses connections, which is the honest answer.

    docker run -d --name agent-app-proxy --network kind -p 31000-31009:31000-31009 ...
"""

import asyncio
import os

TARGET_HOST = os.environ.get("TARGET_HOST", "learn-control-plane")
NODE_PORT_BASE = int(os.environ.get("NODE_PORT_BASE", "30000"))
PROXY_PORT_BASE = int(os.environ.get("PROXY_PORT_BASE", "31000"))
SLOTS = int(os.environ.get("APP_SLOT_COUNT", "20"))
# Anything else in the cluster worth reaching from the LAN, as "listen:nodeport,listen:nodeport".
# The governance dashboard lives here: it used to depend on a `kubectl port-forward` that died
# with every pod restart and had to be revived by hand. A NodePort behind this proxy just stays up.
EXTRA_PORTS = os.environ.get("EXTRA_PORTS", "8091:30090")


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


def handler_for(node_port):
    async def handle(client_reader, client_writer):
        try:
            server_reader, server_writer = await asyncio.open_connection(TARGET_HOST, node_port)
        except OSError as e:
            # Normal, not an error: the slot is allocated but nothing is deployed behind it,
            # or the pod is still rolling. Log once per attempt and drop the connection.
            print(f"slot {node_port}: upstream unreachable ({e})", flush=True)
            client_writer.close()
            return
        await asyncio.gather(pipe(client_reader, server_writer),
                             pipe(server_reader, client_writer))
    return handle


def mappings():
    pairs = [(PROXY_PORT_BASE + i, NODE_PORT_BASE + i) for i in range(SLOTS)]
    for item in filter(None, (p.strip() for p in EXTRA_PORTS.split(","))):
        listen, _, upstream = item.partition(":")
        pairs.append((int(listen), int(upstream)))
    return pairs


async def main():
    servers = []
    for listen, upstream in mappings():
        servers.append(await asyncio.start_server(handler_for(upstream), "0.0.0.0", listen))
        print(f"listening 0.0.0.0:{listen} -> {TARGET_HOST}:{upstream}", flush=True)
    async with asyncio.TaskGroup() as tg:
        for s in servers:
            tg.create_task(s.serve_forever())


asyncio.run(main())
