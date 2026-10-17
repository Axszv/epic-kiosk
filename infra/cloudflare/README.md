# Optional VLESS relay

`vless-relay.mjs` is the source of the recovery Worker used by the existing
dae VLESS/TLS/WebSocket node and the opt-in Actions browser proxy. It uses
Cloudflare's documented `cloudflare:sockets` interface. It does not need a
management panel, KV namespace, or third-party JavaScript dependency.

Configure `UUID` as a Worker secret and `PROXYIP` as the public IPv4 address
of the chosen TLS reverse proxy. Keep the client's VLESS identity unchanged
and set its WebSocket Host and TLS SNI to the deployed Worker hostname.
Never commit a VLESS URL or generated client configuration. A WebSocket
`proxyip` query parameter can override the public reverse-proxy address for
an individual diagnostic run.

The relay supports VLESS TCP and length-prefixed DNS UDP on port 53. Other
UDP commands and authentication failures are rejected before opening a
connection. Each WebSocket owns its queue, credentials, socket and fallback
state. Only a TLS connection that has returned no data may be retried through
the selected reverse proxy; established application traffic is not replayed.
The incoming queue and handshake have size/time limits.

Run the offline protocol tests with:

```sh
node infra/cloudflare/test_vless_relay.mjs
```

Deploy the module with compatibility date `2026-06-12` or later through an
authenticated Cloudflare CLI/API, preserving its secret bindings. Test the
authenticated `/version?uuid=...` endpoint without logging its URL. Then run
the existing Actions workflow with `use_browser_proxy=true` and
`proxy_check_only=true`. A passing network diagnostic does not establish
Epic login or ownership; those require a separate account run.

The October 17 recovery replaced an edgetunnel instance that returned
Cloudflare 1101 and `This script has been killed.` even after rollback.
The earlier instance and its bindings were retained for recovery. The
platform-level termination cause remains unresolved. Keep operational
credentials and VM configuration backups outside this repository.
