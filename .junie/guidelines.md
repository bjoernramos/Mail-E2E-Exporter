Project: Mail E2E Exporter

Scope
- This file records project-specific build, configuration, testing, and development notes to speed up future work.
- Audience: experienced developers; this omits generic FastAPI/Docker basics and focuses on repo-specific behavior and gotchas.

Build and Run (project-specific)
- Containerized runtime only is recommended. The service depends on network access to external SMTP/IMAP servers and exposes Prometheus metrics.
- Docker image: see Dockerfile (python:3.12-slim). Entrypoint runs uvicorn main:app at port 9782 inside /app.
- Required OS packages are installed in image: tzdata, ca-certificates.

Local build via Docker
1) Copy and adjust environment and config
   - cp .env.example .env
   - cp config.example.yaml config.yaml
   - Configure accounts and tests in config.yaml. Prefer referencing secrets via env vars, e.g. ${CUSTOMDOMAIN_TEST_IMAP_PASS}.
2) Build and run
   - docker compose up -d --build mail-e2e-exporter
   - The app binds 0.0.0.0:9782 in the container; host port is configurable via MAIL_E2E_EXPORTER_PORT (defaults to 9782).
3) Runtime endpoints
   - /health and /info can be protected by API_KEY (see below).
   - /metrics may be protected with basic auth via METRICS_USER/METRICS_PASS.
   - /reload reloads config.yaml on demand if API_KEY is set; otherwise it is disabled (returns 401 if called with wrong key).

Configuration details (repo-specific)
- Config file path: /app/config.yaml (mounted read-only by docker-compose.yaml).
- Hot reload: file mtime is checked on every background cycle. Explicit reload: POST /reload with X-API-Key.
- Defaults are in app/main.py DEFAULTS; any key present in config.yaml shallow-merges into these defaults.
- Exporter options of note:
  - exporter.metrics_prefix: Prefix for all Prometheus metric names (default "mail_"). Set to "" to remove prefix or customize per deployment.
  - exporter.check_interval_seconds: Sleep between test cycles (default 300s).
  - exporter.receive_timeout_seconds, exporter.receive_poll_seconds: IMAP polling behavior.
  - exporter.delete_testmail_after_verify: Delete matched messages after verification.
  - exporter.subject_prefix: Subject prefix for outbound test emails.
- Accounts: Define per logical account key. SMTP creds are read from accounts.<key>.smtp; IMAP creds from accounts.<key>.imap. Values support env expansion ($VAR or ${VAR}).
- Gmail specifics: IMAP search will try Gmail labels (All Mail/Spam/Important, DE/EN variants) and prefers X-GM-RAW where available.
- Error hints: If SMTP/IMAP passwords remain as ${VAR} (unresolved), log messages include the missing env var hint.

Auth behavior
- API key: set API_KEY to enforce authentication for /health, /info, /reload. If API_KEY is unset, these endpoints are open.
- Metrics basic auth: set METRICS_USER and METRICS_PASS to require HTTP Basic on /metrics. If either is unset, metrics are open.

Testing: how to run and how to add new tests
- Preferred local testing uses pytest against the FastAPI app object without starting the real E2E background loop.
- The app registers startup/shutdown handlers that spawn a background thread (run_tests_loop) which sends real emails. Tests should disable these to avoid side-effects and external dependencies.

Install test dependencies locally (outside the image)
- Python 3.11+ recommended (dev box has 3.11). Install runtime deps and pytest:
  - pip install -r app/requirements.txt pytest

Create and run a simple test (verified example)
- Below is a minimal pytest that validates /health without starting background workers and without requiring API key.
- This exact test was executed locally and passed (1 test).

  File: tests/test_health.py
  -------------------------------------------------
  import os
  import sys
  from pathlib import Path
  from fastapi.testclient import TestClient

  # Ensure repository root is on sys.path so `import app.main` works
  ROOT = Path(__file__).resolve().parents[1]
  if str(ROOT) not in sys.path:
      sys.path.insert(0, str(ROOT))

  # Ensure API_KEY is unset/empty for test or override dependency
  os.environ.pop("API_KEY", None)

  from app.main import app, require_api_key  # noqa: E402

  # Avoid background threads by disabling startup/shutdown events
  app.router.on_startup.clear()
  app.router.on_shutdown.clear()

  # Disable API key dependency for tests
  app.dependency_overrides[require_api_key] = lambda: None

  def test_health_endpoint_ok():
      with TestClient(app) as client:
          resp = client.get("/health")
          assert resp.status_code == 200
          data = resp.json()
          assert data.get("status") == "ok"
          assert isinstance(data.get("time"), int)
  -------------------------------------------------

Run tests
- From repository root: pytest -q
- Expectation: 1 passed, warnings about FastAPI on_event deprecation are benign.

Guidelines for adding more tests
- Use fastapi.testclient.TestClient against the app instance (from app.main import app).
- Always neutralize startup/shutdown hooks to prevent the background thread:
  - app.router.on_startup.clear(); app.router.on_shutdown.clear()
- Disable the API key requirement for the scope of the test when needed:
  - app.dependency_overrides[require_api_key] = lambda: None
- For endpoints requiring Basic Auth (/metrics), either disable auth by unsetting METRICS_USER/METRICS_PASS before importing app, or craft an Authorization header with those creds.
- Avoid exercising SMTP/IMAP in unit tests. Mock _smtp_send and _imap_wait_receive if you need to cover run_tests_loop logic.
- If you need to test configuration-driven behavior, create a temporary config file and set CONFIG_PATH env var before importing app.main to ensure it loads your test config.

Prometheus metrics notes
- Metric registry is created at import time; metric names honor exporter.metrics_prefix at that moment. If your tests set a custom prefix, ensure it is configured before importing app.main.
- Series labels include: route, from, to. Error counter also has step in {send, receive, config}.
- The app exposes gauges reflecting exporter config: config_delete_testmail_after_verify, config_receive_timeout_seconds, config_receive_poll_seconds, config_check_interval_seconds.

Development tips and gotchas
- Hot reload design: _reload_config_if_changed() compares st_mtime_ns; POST /reload forces reload. Background cycle also sets metrics each cycle and sleeps check_interval_seconds.
- IMAP search logic: tries UTF-8 search and Gmail-specific gmail_search. Extra folders can be configured via accounts.<key>.imap.extra_folders (string or list).
- Subject tokens: Each test generates a unique E2E-<uuid> token added to the subject; body includes timestamp for debugging.
- Error surfaces: g_last_error stores a hash of exception text per route to detect change; c_errors increments for send/receive/config failures.
- Logging: DEBUG is enabled via env DEBUG=true; otherwise INFO. Logs to stdout with a simple formatter.
- Example config auto-write: if WRITE_EXAMPLE_CONFIG=true, the app will create an example config.yaml at startup path (useful in scratch containers). Not used in production compose.

CI/CD and Compose specifics
- docker-compose.yaml attaches the container to an external network named monitoring_monitoring and mounts host config.yaml read-only.
- Make sure that network exists before compose up; otherwise create it: docker network create monitoring_monitoring (or adjust docker-compose.yaml accordingly).
- Exposed port is parameterized via MAIL_E2E_EXPORTER_PORT environment variable.

Cleanup note for this guideline task
- The example test file was used solely to validate the testing flow and has been removed after verification, as requested. To reproduce locally, copy the snippet above into tests/test_health.py, run pytest, and delete the file afterwards.
