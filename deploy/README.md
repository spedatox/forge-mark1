# Deploying the Forge (placement plan, phase H4)

The Forge runs as a **systemd unit on the host**, not as a container. That is
forced by the Cell design: `DockerCell` bind-mounts the job workspace into each
throwaway container, so the paths the Forge hands to `docker run` must be *host*
paths. Running the Forge itself inside a container would mean giving that
container the Docker socket — handing host root to whatever the agent generates,
which is the exact thing the Cell exists to prevent.

```
systemd: forge.service                      (host, root — needs the docker socket)
  └─ python -m forge connect --agent optimus
       └─ docker run --rm --network none --cap-drop ALL --user 1000  ← one per job
            └─ /workspace  ⇄  /opt/hisar/vault/Forge/workspaces/<agent>
```

---

## 1. Install

```bash
git clone https://github.com/spedatox/forge-mark1.git /opt/forge-mk1
cd /opt/forge-mk1
python3 -m venv .venv
./.venv/bin/pip install -e .
docker pull python:3.12-slim          # the Cell image
```

## 2. Configure

`.env`, mode 600. `SPEDA_API_KEY` and `ANTHROPIC_API_KEY` are the same values
Igor uses — copy them from `packages/igor/.env`, and rotate them in both places
together.

```ini
ANTHROPIC_API_KEY=…
SPEDA_API_KEY=…
SPEDA_WS_URL=ws://127.0.0.1:8000/agents/ws/optimus
FORGE_CELL_BACKEND=docker
FORGE_CELL_IMAGE=python:3.12-slim
FORGE_WORKSPACE_ROOT=/opt/hisar/vault/Forge/workspaces
```

The workspace root inside the Hisar vault is the whole of the placement plan's
passive H4 layer: live Cell workspaces are browsable on the web desktop with no
code at all.

## 3. Vault permissions

Hisar's container runs as uid 10001 / **gid 999**. Group-own the Forge subtree to
that gid and set the setgid bit, so everything created under it stays manageable
from the file desktop:

```bash
chgrp -R 999 /opt/hisar/vault/Forge
chmod -R g+rwX /opt/hisar/vault/Forge
find /opt/hisar/vault/Forge -type d -exec chmod g+s {} +
```

The Cell runs as uid 1000 and drops every capability, so it cannot chown its own
bind mount — `DockerCell` does that from the host side at start, and probes
writability before reporting the Cell started. A job that cannot write its
workspace now fails loudly at startup instead of on its first write.

## 4. Service

```bash
cp deploy/forge.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now forge
journalctl -u forge -f          # expect: peer_registered
```

`EnvironmentFile` is load-bearing: `forge/__main__.py` reads `os.environ`
directly and loads no `.env` itself.

## 5. Verify

```bash
systemctl is-active forge                                   # active
journalctl -u forge -n 5 | grep peer_registered
curl -sS -H "X-API-Key: $SPEDA_API_KEY" localhost:8000/agents   # optimus: online
```

A restart should take well under a second. If it takes 20s and the journal says
`stop-sigterm timed out`, the peer is ignoring SIGTERM — that was a real bug
(the stop event went unobserved while the connection was healthy) and it is
covered by `test_stop_request_ends_an_idle_connection`.

## 6. Updating

There is no CI for this repo. After pushing:

```bash
cd /opt/forge-mk1 && git fetch origin main && git reset --hard origin/main
./.venv/bin/pip install -e .        # only when dependencies changed
systemctl restart forge
```
