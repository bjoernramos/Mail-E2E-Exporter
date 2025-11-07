<p align="center">
  <img src="https://raw.githubusercontent.com/bjoernramos/Mail-E2E-Exporter/main/assets/icon.png" width="160" alt="Mail E2E Exporter Icon">
</p>

![License: CC BY-NC 4.0](https://img.shields.io/badge/license-CC--BY--NC--4.0-blue) ![GitHub Stars](https://img.shields.io/github/stars/bjoernramos/Mail-E2E-Exporter?style=social)

![Docker Pulls](https://img.shields.io/docker/pulls/bjoernramos/mail-e2e-exporter)

# Mail E2E Exporter

A Prometheus-compatible exporter that continuously verifies real end-to-end email delivery: send via SMTP and receive via IMAP. For every configured route a test mail is sent with a unique token in the subject; the exporter polls the inbox, measures round-trip latency, and exposes results as Prometheus metrics.

- Protocols: SMTP (send), IMAP (receive)
- Targets: any provider with SMTP/IMAP (e.g., Gmail, custom domains)
- Metrics: success flags, round-trip seconds, last timestamps, error counters, and config gauges

## Requirements
- Docker and Docker Compose v2
- Outbound network access to your mail providers (SMTP/IMAP ports) from the container
- Optional: Prometheus (to scrape metrics) and Grafana (for visualization)
- For Gmail: IMAP enabled and an app password

## Quick start (Docker Compose)
1) Copy environment and config templates

   ```
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```

   - Set METRICS_USER/METRICS_PASS to protect /metrics (optional but recommended)
   - Optionally set API_KEY to protect /health, /info, /reload
   - Edit config.yaml: define accounts and tests. Prefer referencing secrets via env vars, e.g. ${CUSTOMDOMAIN_TEST_IMAP_PASS}

2) Start the service (from repo root)

   ```
   docker compose up -d --build mail-e2e-exporter
   ```

   - The app listens on 0.0.0.0:9782 inside the container; host port is ${MAIL_E2E_EXPORTER_PORT:-9782}

3) Smoketest the endpoints

   - Health
     ```
     curl -s http://localhost:9782/health | jq .
     ```
   - Info (shows config state and discovered tests)
     ```
     curl -s http://localhost:9782/info | jq .
     ```
   - Metrics (with Basic Auth if configured)
     ```
     curl -s -u "$METRICS_USER:$METRICS_PASS" http://localhost:9782/metrics | head -n 30
     ```

4) Reload config on demand (if API_KEY is set)

   ```
   curl -s -X POST -H "X-API-Key: $API_KEY" http://localhost:9782/reload | jq .
   ```

Note: docker-compose.yaml mounts ./config.yaml read-only to /app/config.yaml. File changes on the host are picked up automatically at the next background cycle, or immediately after /reload.

## Configuration

- Config file path: /app/config.yaml (override with CONFIG_PATH)
- Hot reload: the file mtime is checked every background cycle; POST /reload forces an immediate reload (requires API_KEY)
- Defaults live in app/main.py (DEFAULTS). Config shallow-merges on top of these defaults.

### Environment variables (.env)
- METRICS_USER, METRICS_PASS: optional HTTP Basic auth for /metrics
- API_KEY: optional API key protecting /health, /info, /reload
- CONFIG_PATH: path to YAML config (default /app/config.yaml)
- WRITE_EXAMPLE_CONFIG: true|false — write an example config.yaml at first start (not used in production)
- DEBUG: true|false — verbose logs (SMTP/IMAP details) to stdout

### YAML config (config.yaml)
- exporter
  - listen_addr: default 0.0.0.0
  - listen_port: default 9782 (container internal)
  - check_interval_seconds: sleep between test cycles (default 300)
  - receive_timeout_seconds: IMAP search timeout per cycle
  - receive_poll_seconds: IMAP poll interval while waiting
  - delete_testmail_after_verify: delete matched messages after verification (default true)
  - subject_prefix: subject prefix for outbound test messages (default "[MAIL-E2E]")
  - metrics_prefix: prefix for Prometheus metric names (default "mail_"). IMPORTANT: the registry and names are created at import time; adjust before app import/container start.
- accounts: map of logical account keys. For each key provide smtp and/or imap blocks. Values support environment expansion ($VAR or ${VAR}).
  - smtp: host, port, starttls (default true), username, password
  - imap: host, port, ssl (default true), username, password, folder (default INBOX), extra_folders (string or list)
- tests: list of routes, each with name (optional), from (account key), to (account key)

Gmail specifics: the IMAP search will try common Gmail labels (All Mail/Spam/Important in EN/DE variants) and prefers X-GM-RAW when available. You can also add imap.extra_folders for custom labels and adjust receive_timeout_seconds if needed.

## Endpoints and authentication
- GET /health — returns {status: ok, time: <unix>}; requires API_KEY if set
- GET /info — config introspection and version metadata; requires API_KEY if set
- GET /version — returns version metadata only; requires API_KEY if set
- GET /metrics — Prometheus metrics; can be protected with Basic Auth via METRICS_USER/METRICS_PASS
- POST /reload — force config reload; requires API_KEY if set

## Prometheus integration
Scrape /metrics from Prometheus. Examples:

- Same Docker network
  ```yaml
  scrape_configs:
    - job_name: 'mail-e2e-exporter'
      static_configs:
        - targets: ['mail-e2e-exporter:9782']
  ```

- Via host port (local)
  ```yaml
  scrape_configs:
    - job_name: 'mail-e2e-exporter-local'
      static_configs:
        - targets: ['localhost:9782']
  ```

- With Basic Auth
  ```yaml
  scrape_configs:
    - job_name: 'mail-e2e-exporter-secure'
      static_configs:
        - targets: ['mail-e2e-exporter:9782']
      basic_auth:
        username: '${METRICS_USER}'
        password: '${METRICS_PASS}'
  ```

Reverse proxy: place an upstream to mail-e2e-exporter:9782 and protect /metrics; set metrics_path: /metrics in Prometheus if scraping via hostname.

## Exposed metrics
Assuming metrics_prefix = "mail_e2e_exporter_" (default). All core series have labels route, from, to. The error counter also has label step in {send, receive, config}.

# Mail E2E Exporter – Exposed Metrics

| Metric | Type | Description |
|--------|------|-------------|
| `mail_e2e_exporter_send_success{from,to,route}` | `gauge` | `1` if SMTP send succeeded, else `0`. |
| `mail_e2e_exporter_receive_success{from,to,route}` | `gauge` | `1` if IMAP receive succeeded, else `0`. |
| `mail_e2e_exporter_roundtrip_seconds{from,to,route}` | `gauge` | Time in seconds from send to receive. |
| `mail_e2e_exporter_last_send_timestamp{from,to,route}` | `gauge` | Unix timestamp of the last send attempt. |
| `mail_e2e_exporter_last_receive_timestamp{from,to,route}` | `gauge` | Unix timestamp of the last received test mail. |
| `mail_e2e_exporter_test_errors_total{from,to,route,step}` | `counter` | Total errors, labeled by step (`send`, `receive`). |
| `mail_e2e_exporter_test_errors_created{from,to,route,step}` | `gauge` | Timestamp when each error counter was created. |
| `mail_e2e_exporter_last_error_info{from,to,route}` | `gauge` | Encoded hash of last error (0 = no error). |
| `mail_e2e_exporter_build_info{version,revision,build_date}` | `gauge` | Version and build metadata (`= 1`). |
| `mail_e2e_exporter_config_delete_testmail_after_verify` | `gauge` | `1` if test mails are deleted after success. |
| `mail_e2e_exporter_config_receive_timeout_seconds` | `gauge` | Configured receive timeout. |
| `mail_e2e_exporter_config_receive_poll_seconds` | `gauge` | Configured IMAP polling interval. |
| `mail_e2e_exporter_config_check_interval_seconds` | `gauge` | Configured full check interval. |
| `mail_e2e_exporter_test_info{from,to,route}` | `gauge` | Always `1`; maps configured routes for observability. |


Note: The actual metric names will use whatever exporter.metrics_prefix is set to at import time (can be "" for no prefix).

## Troubleshooting
- IMAP AUTHENTICATIONFAILED
  - IMAP login failed (wrong/empty credentials or app password required)
  - Often caused by unresolved env vars in config.yaml, e.g. password: ${BRAMOS_TEST_IMAP_PASS}. Define it in .env (or environment) and restart the container or call POST /reload with API_KEY.
  - Set DEBUG=true for detailed hints (account/host and missing env var key)

- Gmail message not found (timeout)
  - Gmail uses labels rather than folders. The exporter automatically searches common Gmail folders and uses X-GM-RAW with fallback to SUBJECT search.
  - If needed, add imap.extra_folders and/or increase receive_timeout_seconds.

- No tests configured
  - The exporter still exposes a placeholder route (no-tests-configured) so you can see it is running, but no real mail is sent.

## Grafana dashboard
A ready-to-import dashboard JSON is provided under grafana/:

- grafana/mail-e2e-all-in-one.json — shows send/receive success, round-trip, and error overview.

Import in Grafana:
1. In Grafana: Dashboards → New → Import
2. Select the JSON file
3. Choose the Prometheus datasource
4. Save

## Docker/Compose notes
- docker-compose.yaml attaches the container to an external network named monitoring_monitoring and mounts ./config.yaml read-only to /app/config.yaml. Ensure the network exists before bringing the stack up:
  ```
  docker network create monitoring_monitoring
  ```
- Exposed port is parameterized via MAIL_E2E_EXPORTER_PORT (defaults to 9782).

## Links
- [DockerHub](https://hub.docker.com/r/bjoernramos/mail-e2e-exporter)
- [GitHub](https://github.com/bjoernramos/Mail-E2E-Exporter)
- [Twitter](https://x.com/bjoern_rms)
- [Gravatar](https://gravatar.com/blueada89f5864b)
- [BlueSky](https://bsky.app/profile/bjoernramos.bsky.social)
- [LinkedIn](https://www.linkedin.com/in/björn-ramos-166b59167/)
- [Instagram](https://www.instagram.com/bjoern.rms/)

## License
Licensed under CC BY-NC 4.0. Free for private and internal (non-commercial) use. Attribution required: © 2025 Bjørn Ramos.