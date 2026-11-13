# AlwaysOn: Autonomous Infrastructure Recovery Platform

> Autonomous infrastructure recovery and failover.

> 🔒 **Proprietary and confidential software.** All rights reserved.
> Access to this repository does not grant any right to copy, distribute, modify or use the code without prior written permission from the author.

---

## What it does

- **Continuous health checking** of every endpoint (HTTP, TCP or shell command), with failure/success thresholds to avoid reacting to single blips.
- **Autonomous recovery:** runs your recovery actions on unhealthy endpoints, with backoff, a maximum number of attempts and a cooldown.
- **Automatic failover:** when the active endpoint goes down, traffic is moved to the healthiest endpoint by priority.
- **Failback:** return to the preferred endpoint automatically (after it has been stable for a while) or manually.
- **Flap protection:** if too many switches happen in a short window, automation freezes and waits for an operator.
- **Switch hooks:** commands and webhooks that actually move the traffic (DNS, load balancer, ...), retried when they fail.
- **Admin API and metrics:** status, events, Prometheus metrics and token-protected manual operations.
- **Persistent state:** the active endpoint survives restarts.

AlwaysOn decides *when* to switch and runs your hooks. It does not talk to any cloud or load balancer by itself: the hooks do.

## How it works

```
 probes (http / tcp / command)
        │ every `interval`
        ▼
 ┌───────────────────────────────────────────────┐
 │ ServiceController (one per service)           │
 │  1. record results, apply thresholds          │
 │  2. active unhealthy?  → failover (by priority)│
 │     better endpoint stable? → failback        │
 │  3. unhealthy endpoints → recovery actions    │
 │  4. retry pending switch hooks                │
 │  5. publish state, emit events                │
 └───────────────────────────────────────────────┘
        │                      │
   on_failover hooks     state file / events log / API
```

Service status: `starting`, `healthy` (top-priority endpoint active), `degraded` (running on a lower-priority endpoint), `outage` (active endpoint down and nowhere to go).

## Install

```bash
pip install -e .
```

Or without installing: `PYTHONPATH=src python -m alwayson --help`

## Quick start

```bash
alwayson init        # writes alwayson.json
alwayson validate    # checks the configuration
alwayson check       # probes every endpoint once (no side effects)
alwayson run         # starts monitoring, recovery and failover
alwayson status      # last published state
alwayson events --tail 20
```

## Configuration

```json
{
  "state_file": "./.alwayson/state.json",
  "event_log": "./.alwayson/events.jsonl",
  "api": { "enabled": true, "host": "127.0.0.1", "port": 8480, "token_env": "ALWAYSON_TOKEN" },
  "defaults": { "interval": 5, "timeout": 3, "failure_threshold": 3, "success_threshold": 2 },
  "services": [
    {
      "name": "web",
      "failover_delay": 0,
      "failback": { "mode": "auto", "stable_seconds": 60 },
      "flap_protection": { "max_switches": 5, "window_seconds": 600 },
      "recovery": {
        "actions": [{ "type": "command", "command": "systemctl restart web@$ALWAYSON_ENDPOINT", "timeout": 30 }],
        "max_attempts": 3,
        "backoff_seconds": [5, 15, 45],
        "cooldown_seconds": 300
      },
      "on_failover": [
        { "type": "command", "command": "./scripts/switch-traffic.sh", "timeout": 30 },
        { "type": "webhook", "url": "https://hooks.example.com/alwayson", "timeout": 10 }
      ],
      "endpoints": [
        { "name": "primary", "priority": 1, "address": "10.0.0.10:80",
          "check": { "type": "http", "url": "http://10.0.0.10/healthz", "expect_status": 200 } },
        { "name": "standby", "priority": 2, "address": "10.0.1.10:80",
          "check": { "type": "tcp", "host": "10.0.1.10", "port": 80 } }
      ]
    }
  ]
}
```

Paths in `state_file` and `event_log` are relative to the configuration file.

### Service fields

| Field | Default | Description |
|-------|---------|-------------|
| `name` | required | Letters, digits, `_`, `.`, `-`. |
| `interval` | 5 | Seconds between probe rounds. |
| `timeout` | 3 | Default probe timeout (seconds). |
| `failure_threshold` | 3 | Consecutive failures before an endpoint is `unhealthy`. |
| `success_threshold` | 2 | Consecutive successes before an endpoint is `healthy`. |
| `failover_delay` | 0 | Seconds the active endpoint must stay down before failing over. |
| `failback.mode` | `auto` | `auto` or `manual`. |
| `failback.stable_seconds` | 60 | How long a better endpoint must be healthy before failing back. |
| `flap_protection.max_switches` | 5 | Automatic switches allowed in the window before freezing. |
| `flap_protection.window_seconds` | 600 | Size of that window. |
| `recovery` | none | Service-wide recovery policy (an endpoint can override it with its own `recovery`). |
| `on_failover` | `[]` | Actions run whenever the active endpoint changes. |
| `hook_max_attempts` | 5 | Attempts for failing hooks before giving up. |
| `hook_retry_seconds` | 10 | Delay between hook attempts. |

`defaults` supplies `interval`, `timeout`, `failure_threshold` and `success_threshold` to every service.

### Checks

| Type | Fields |
|------|--------|
| `http` | `url`, `expect_status` (number or list, default 200), `expect_body` (substring), `headers`, `verify_tls` (default true), `method` (`GET`/`HEAD`), `timeout` |
| `tcp` | `host`, `port`, `timeout` |
| `command` | `command` (healthy when it exits 0), `timeout` |

### Recovery policy

`actions` (list, run in order, stops at the first failure), `max_attempts` (3), `backoff_seconds` ([5, 15, 45]), `cooldown_seconds` (300). After `max_attempts` AlwaysOn waits for the cooldown and starts over.

### Action environment

Commands receive these variables; webhooks receive the same data as a JSON body plus `timestamp`.

| Variable | Meaning |
|----------|---------|
| `ALWAYSON_SERVICE` | Service name |
| `ALWAYSON_EVENT` | `election`, `failover`, `failback`, `manual_failover` or `recovery` |
| `ALWAYSON_FROM` / `ALWAYSON_TO` | Previous and new active endpoint (switch hooks) |
| `ALWAYSON_ENDPOINT` | The endpoint being switched to or recovered |
| `ALWAYSON_ADDRESS` | That endpoint's `address` |
| `ALWAYSON_REASON` | Why the action runs |
| `ALWAYSON_ATTEMPT` | Attempt number (recovery actions) |

Switch hooks may run more than once when a later hook fails, so **make them idempotent**.

## HTTP API

Enabled with `"api": {"enabled": true}`. It binds to `127.0.0.1` by default.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/healthz` | none | Liveness |
| GET | `/api/v1/status` | none | State of all services |
| GET | `/api/v1/events?limit=100&service=web` | none | Recent events |
| GET | `/metrics` | none | Prometheus metrics |
| POST | `/api/v1/services/<name>/failover` | token | Body `{"to": "standby", "force": false}` |
| POST | `/api/v1/services/<name>/freeze` | token | Pause automation for the service |
| POST | `/api/v1/services/<name>/unfreeze` | token | Resume automation |

The admin token is read from the environment variable named by `api.token_env`. Without a token the POST endpoints are disabled. Send it as `Authorization: Bearer <token>`.

A manual failover pins the service: automatic failback is suppressed until the pinned endpoint fails or you switch again.

## Security notes

- The configuration file is trusted: commands run through the shell with the privileges of the AlwaysOn process. Restrict who can edit it and run AlwaysOn as an unprivileged user.
- Keep the API on localhost, or put it behind a TLS-terminating reverse proxy with network controls.
- The admin token is read only from the environment, never from the configuration file.

## Limitations

- A single controller process: run it under a supervisor (systemd, container restart policy). It does not replicate itself.
- Hooks and recovery commands block that service's loop until they finish or time out.
- Traffic switching is the responsibility of your hooks.
- Windows is only partially supported (process-group timeouts are POSIX-only).

## Tests

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

## License

**All rights reserved.** See [LICENSE](./LICENSE).
