# Security policy

juju-ops gives an LLM write access to a Juju model, bounded by the
server-side policy in `policy/juju-ops.yaml`. A way around that policy
(a mutation outside an app's bounds or allowlist, a dry-run that
executes, a tool reaching a model outside `allowed_models`, and so on) is
a security bug.

The servers have no authentication of their own. Over stdio they are
reachable only by the host that launched them; with
`--transport streamable-http` they bind to `127.0.0.1` by default, and
anyone who can reach the port can call every tool within the policy.
Exposing that port without an authenticating proxy in front is a
deployment choice, not a vulnerability in this project (see
[`deploy/systemd/README.md`](deploy/systemd/README.md)).

## Reporting a vulnerability

Please report vulnerabilities privately through GitHub: **Security →
Report a vulnerability** on this repository. Do not open a public issue
or pull request for them.

Include what you ran (tool name and arguments, policy file), what
happened, and what you expected the policy to do.

## Supported versions

Only the latest `main` is supported. Fixes are not backported.
