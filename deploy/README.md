# Deploying the Forge (placement plan, phase H4)

The Forge runs as a **systemd unit on the host**, not as a container. That is
forced by the Cell design: `DockerCell` bind-mounts the job workspace into each
throwaway container, so the paths the Forge hands to `docker run` must be *host*
paths. Running the Forge itself inside a container would mean giving that
container the Docker socket — handing host root to whatever the agent generates,
which is the exact thing the Cell exists to prevent.

```
systemd: forge@<agent>.service              (host, root — needs the docker socket)
  └─ python -m forge connect --agent <agent>
       └─ docker run --rm --network none --cap-drop ALL --user 1000  ← one per job
            └─ /workspace  ⇄  /opt/hisar/vault/Forge/workspaces/<agent>
```

---

## 1. Install

```bash
git clone https://github.com/spedatox/forge-mark1.git /opt/forge-mk1
cd /opt/forge-mk1
python3 -m venv .venv
./.venv/bin/pip install -e ".[providers]"
docker pull python:3.12-slim          # the Cell image
```

The `providers` extra is **not** optional in practice on this deployment. It
pulls the OpenAI client, which every non-Anthropic provider shares (OpenAI,
Gemini, z.ai, DeepSeek, Ollama). Install without it and a plain `pip install -e .`
looks fine until a model pin resolves to one of them, then dies with
`ModuleNotFoundError: No module named 'openai'` mid-job. Anthropic-only
deployments can skip it; this one cannot, because Igor's model pins can route to
any configured provider.

Providers verified against Igor's env: Anthropic, OpenAI, z.ai. Gemini has no
`GEMINI_API_KEY` anywhere on the box — add it to Igor's `.env` before pinning
anything to Gemini.

## 2. Configure

**Credentials are not duplicated here.** The provider keys and `SPEDA_API_KEY`
are the same values Igor uses, so the unit loads Igor's `.env` first and this
file second. Copying them instead produced exactly the failure you would expect:
a model pin resolved to `openai:…` and the peer died with *"openai model
requested but its API key is not set"*, because only the Anthropic key had been
copied across. Nothing is widened by reading Igor's file — the Forge runs as
root on the same host and could always read it.

`/opt/forge-mk1/.env` therefore holds only Forge-specific settings, mode 600:

```ini
FORGE_CELL_BACKEND=docker
FORGE_CELL_IMAGE=python:3.12-slim
FORGE_WORKSPACE_ROOT=/opt/hisar/vault/Forge/workspaces
```

`SPEDA_WS_URL` is not set here either — the unit derives it per agent.

Per-agent overrides go in `.env.<agent>` (optional, loaded last). That is the
only way to vary the Cell image per agent, since `FORGE_CELL_IMAGE` is global
settings rather than a profile field:

```ini
# .env.centurion — its profile wants security tooling in the Cell
FORGE_CELL_IMAGE=ghcr.io/…/forge-cell-security:latest
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

The unit is **templated on the agent id**, so each agent is one more instance
rather than one more file:

```bash
cp deploy/forge@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now forge@optimus forge@centurion
journalctl -u 'forge@*' -f      # expect: peer_registered, once per agent
```

`EnvironmentFile` is load-bearing: `forge/__main__.py` reads `os.environ`
directly and loads no `.env` itself.

## 5. Verify

```bash
systemctl is-active forge@optimus forge@centurion
journalctl -u 'forge@*' -n 10 | grep peer_registered
curl -sS -H "X-API-Key: $SPEDA_API_KEY" localhost:8000/agents   # both online
```

A restart should take well under a second. If it takes 20s and the journal says
`stop-sigterm timed out`, the peer is ignoring SIGTERM — that was a real bug
(the stop event went unobserved while the connection was healthy) and it is
covered by `test_stop_request_ends_an_idle_connection`.

## 6. Agents

| Agent | Cell network | Notes |
|---|---|---|
| `optimus` | **off** (`--network none`) | coding; the default posture |
| `centurion` | **on** | recon and scanning need it — declared in its `profile.toml`, not in any env file |

Centurion's cells reach the internet. That is deliberate and profile-declared,
but it is the one agent whose sandbox is not network-isolated, so it is worth
knowing before dispatching to it. Its profile also expects security tooling
(nmap, nikto, …) in the Cell image; until one is built it shares Optimus's
`python:3.12-slim`, which carries none of it — see the `.env.centurion` seam
in §2.

## 7. Updating

There is no CI for this repo. After pushing:

```bash
cd /opt/forge-mk1 && git fetch origin main && git reset --hard origin/main
./.venv/bin/pip install -e ".[providers]"   # only when dependencies changed
systemctl restart forge@optimus forge@centurion
```
