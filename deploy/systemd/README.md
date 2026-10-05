# systemd unit files for the MCP servers

Three [user-mode systemd](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)
service files that run the MCP servers as long-lived services speaking
MCP over streamable HTTP, for any HTTP-capable MCP client (and for the
autoscaler in `eval/auto/`):

- `juju-docs-mcp.service`     — `http://127.0.0.1:8001/mcp`, juju-docs (read-only docs search)
- `juju-ops-mcp.service`      — `http://127.0.0.1:8002/mcp`, juju-ops against your controller
- `cos-metrics-mcp.service`   — `http://127.0.0.1:8003/mcp`, cos-metrics against your Prometheus

They run as your user, so no root is needed. Each unit runs the package's
console script directly, e.g.
`.venv/bin/juju-ops-mcp --transport streamable-http --host 127.0.0.1 --port 8002`,
so the servers listen on loopback only.

## Install

The units expect the checkout at `~/juju-agent-mcps` (`%h/juju-agent-mcps`
in the unit files).

```bash
# 1. Clone and install the servers.
git clone https://github.com/Adithya-Rajendran/juju-agent-mcps ~/juju-agent-mcps
cd ~/juju-agent-mcps
uv sync --locked

# 2. Create the juju-ops environment file. It must exist (the unit refuses
#    to start without it) and should be readable only by you.
mkdir -p ~/.config
install -m 600 .env.example ~/.config/juju-ops-mcp.env
$EDITOR ~/.config/juju-ops-mcp.env

# 3. Install the units.
mkdir -p ~/.config/systemd/user
cp deploy/systemd/*.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now juju-docs-mcp juju-ops-mcp cos-metrics-mcp

# 4. Keep the units running after you log out.
sudo loginctl enable-linger "$USER"
```

### The juju-ops environment file

`~/.config/juju-ops-mcp.env` holds the controller connection. Fill in one
of the two options from `.env.example`:

- **Direct endpoint:** `JUJU_CONTROLLER_ENDPOINT`, `JUJU_USERNAME`,
  `JUJU_PASSWORD` and `JUJU_CACERT` (values from
  `juju show-controller --show-password <controller>`). Set
  `JUJU_CACERT` to the **path** of a PEM file; systemd's
  `EnvironmentFile=` can't hold a multi-line PEM literal.
- **Named controller:** `JUJU_CONTROLLER=<name>` from your local Juju
  CLI files. Model names are then resolved as `<controller>:<model>`.

If you leave every line commented out, juju-ops uses the current
controller from `~/.local/share/juju`. Those CLI credentials are only
used when `JUJU_CONTROLLER_ENDPOINT` is unset.

The model comes from `default_model` in `policy/juju-ops.yaml`, or from
`JUJU_OPS_DEFAULT_MODEL` in this file. It must be listed in
`allowed_models`, or the server refuses to start.

### Prometheus URL for cos-metrics

cos-metrics reads the Prometheus URL from `prometheus.url` in
`policy/cos-metrics.yaml`. To override it without editing the policy, add
a drop-in:

```bash
systemctl --user edit cos-metrics-mcp
```

```ini
[Service]
Environment=COS_PROM_URL=http://<cos-prometheus-host>/cos-prometheus-0
```

## Verify

```bash
# Status of all three.
systemctl --user status juju-docs-mcp juju-ops-mcp cos-metrics-mcp \
    --no-pager --lines=5

# Live logs for one of them.
journalctl --user -u juju-ops-mcp -f

# MCP handshake + tools/list against all three; exits 1 if any is down.
cd ~/juju-agent-mcps && .venv/bin/python -m eval.auto.mcp_client
```

A healthy set prints `http://127.0.0.1:8001/mcp: 1 tools` for juju-docs,
13 tools for juju-ops (port 8002) and 4 for cos-metrics (port 8003).

## Operate

```bash
# Pick up a config or policy change.
systemctl --user restart juju-ops-mcp

# Stop everything (e.g. before a maintenance window).
systemctl --user stop juju-docs-mcp juju-ops-mcp cos-metrics-mcp

# Disable autostart (units stay installed; they just won't start on boot).
systemctl --user disable juju-docs-mcp juju-ops-mcp cos-metrics-mcp

# After pulling new code.
cd ~/juju-agent-mcps && uv sync --locked
systemctl --user restart juju-docs-mcp juju-ops-mcp cos-metrics-mcp
```

## Porting notes

- **Checkout somewhere else.** Override `WorkingDirectory=`,
  `ExecStart=` and the `*_POLICY` path with
  `systemctl --user edit <unit>` (this writes a drop-in at
  `~/.config/systemd/user/<unit>.service.d/override.conf`). To replace
  `ExecStart=` in a drop-in, first clear it with an empty `ExecStart=`
  line.
- **Different policy files.** Set `JUJU_OPS_POLICY` or
  `COS_METRICS_POLICY` in a drop-in.
- **System-mode (root) install.** Move the units to
  `/etc/systemd/system/`, drop `--user` from every command, replace `%h`
  with an absolute path, and add `User=` and `Group=` to each
  `[Service]` section, pointing at a service account that can read the
  checkout and the env file.
- **Without uv.** `python3 -m venv .venv && .venv/bin/pip install -e .`
  produces the same `.venv/bin/<name>-mcp` console scripts the units
  call.
- **Different port or address.** Change `--host` / `--port` on the
  `ExecStart=` line in a drop-in. The autoscaler and the health check
  above assume ports 8001-8003 on 127.0.0.1; pass other URLs to
  `python -m eval.auto.mcp_client` as arguments.

## What this does **not** include

- **No authentication on the MCP ports.** The servers have no auth of
  their own, so any local process that can reach `127.0.0.1:8002` can
  drive your cluster within policy bounds. On loopback the servers also
  reject requests whose `Host` header isn't local (DNS-rebinding
  protection); a non-loopback `--host` turns that check off and logs a
  warning. Before exposing a port beyond localhost, put an
  authenticating reverse proxy (nginx + basic auth, WireGuard, Caddy +
  an OIDC plugin) in front. The autoscaler client in `eval/auto/` does
  not send credentials, so it only works against an unauthenticated
  endpoint.
- **No autoscaler.** The autoscaler driver (`eval/auto/autoscaler.py`)
  is a job, not a service. Run it with
  `eval/auto/run_autoscale_session.sh` or `eval/auto/demo_15min.sh`
  (see [`eval/auto/RUNNING.md`](../../eval/auto/RUNNING.md)). It calls
  tools through these three services, so they must be running.
