# Mail E2E Exporter

Ein Prometheus‑kompatibler Exporter, der den realen E2E‑Pfad von E‑Mails überwacht: Versand per SMTP und Empfang per IMAP. Für jede Route wird eine Test‑Mail mit Token im Betreff verschickt, der Eingang wird gesucht, die Roundtrip‑Zeit gemessen und als Metriken exponiert.

Projekt‑Spezifikation: `../advanced.json`

## Systemanforderungen
- Docker und Docker Compose v2
- Ausgehender Zugriff vom Container auf die Mail‑Provider (SMTP/IMAP‑Ports)
- Optional: Prometheus (zum Scrapen der Metriken) und Grafana (Visualisierung)
- Für Gmail: App‑Passwort und IMAP aktiviert

## Installation (Quick Start)

### 1) Projekt klonen und env anlegen

   ```
    cp mail-e2e-exporter/.env.example mail-e2e-exporter/.env
   ```

   - `METRICS_USER/METRICS_PASS` setzen (für geschützte /metrics)
   - Optional: `API_KEY` setzen (für /reload und zukünftige Endpunkte)


### 2) Konfiguration vorbereiten

   ```
   cp mail-e2e-exporter/config.example.yaml mail-e2e-exporter/config.yaml
   ```
   - Accounts (SMTP/IMAP) und Tests anpassen
   - Passwörter per Env‑Var referenzieren, z. B. ${CUSTOMDOMAIN_TEST_IMAP_PASS}

### 3) Container starten (im Repo‑Root)

   docker compose up -d --build mail-e2e-exporter

### 4) Funktion testen

   - Health
      ```
     curl -s http://localhost:9782/health | jq .
        ```

   - Info zur geladenen Konfiguration
   curl -s http://localhost:9782/info | jq .

   - Metriken (mit Basic Auth, wenn gesetzt)
      ```
     curl -s -u "$METRICS_USER:$METRICS_PASS" http://localhost:9782/metrics | head -n 30
        ```

### 5) Konfigurations‑Reload (optional, wenn config.yaml geändert wurde)

   - Falls API_KEY gesetzt ist
      ```
     curl -s -X POST -H "X-API-Key: $API_KEY" http://localhost:9782/reload | jq .
        ```

> Hinweis: In `docker-compose.yml` ist `mail-e2e-exporter/config.yaml` schreibgeschützt nach `/app/config.yaml` gemountet. Änderungen auf dem Host greifen automatisch im nächsten Testzyklus oder sofort nach `/reload`.

## Prometheus‑Integration
Der Exporter liefert seine Metriken unter `/metrics` im Prometheus‑Format aus.

### Ziel im gemeinsamen Docker‑Netz:

      scrape_configs:
        - job_name: 'mail-e2e-exporter'
          static_configs:
            - targets: ['mail-e2e-exporter:9782']

### Ziel via Host‑Port (lokal):

      scrape_configs:
        - job_name: 'mail-e2e-exporter-local'
          static_configs:
            - targets: ['localhost:9782']

### Mit Basic Auth (empfohlen, wenn Port extern erreichbar ist):

        scrape_configs:
            - job_name: 'mail-e2e-exporter-secure'
              static_configs:
                - targets: ['mail-e2e-exporter:9782']
              basic_auth:
                username: '${METRICS_USER}'
                password: '${METRICS_PASS}'


### Über Nginx/Reverse Proxy (Beispiel):
  - Nginx Upstream auf `mail-e2e-exporter:9782` legen und Location `/metrics` schützen.
    - Prometheus dann auf `prometheus_targets: ['exporter.domain.tld/metrics']` via `metrics_path: /metrics` scrapen.

          scrape_configs:
            - job_name: 'mail-e2e-exporter-proxy'
              metrics_path: /metrics
              static_configs:
                - targets: ['exporter.domain.tld']

### Beispiel‑PromQL und Alerting:
- Roundtrip Zeit pro Route: `mail_e2e_exporter_roundtrip_seconds{route!=""}`
- Empfangsfehler: `increase(mail_e2e_exporter_test_errors_total{step="receive"}[10m]) > 0`
- Alert (Einfach):

  - alert: MailRouteFailed
    expr: mail_e2e_exporter_receive_success == 0
    for: 10m
    labels:
      severity: critical
    annotations:
      summary: "Mail route failed"
      description: "Mailtest {{ $labels.route }} konnte nicht erfolgreich empfangen werden."

## Konfiguration
- `.env` (siehe `../.env.example`)
  - METRICS_USER / METRICS_PASS: Basic Auth für `/metrics`
  - API_KEY: Optionaler API‑Key für geschützte Endpunkte (z. B. /reload)
  - CONFIG_PATH: Pfad zur YAML‑Konfiguration (Default `/app/config.yaml`)
  - WRITE_EXAMPLE_CONFIG: `true|false` – erzeugt beim ersten Start eine Beispiel‑`config.yaml`
  - DEBUG: `true|false` – detaillierte SMTP/IMAP‑Logs in den Docker‑Logs
- `config.yaml` (siehe `../config.example.yaml`)
  - exporter: Ports, Intervalle, Präfixe
    - metrics_prefix: Prefix für alle Prometheus-Metriken (Default: "mail_"). Änderung erfordert Neustart des Containers.
  - accounts: SMTP/IMAP‑Zugänge (Passwörter via Env‑Vars referenzieren)
  - tests: Liste der Routen (from/to Account‑Keys)

### Live‑Reload der Konfiguration
- Automatischer Reload bei Datei‑Änderung (zu Beginn jedes Testzyklus)
- Manuell: `POST /reload` (mit gültigem API_KEY)
- Diagnose: `GET /info` zeigt Pfad, mtime_ns, Größe und erkannte Tests

## Exponierte Metriken (Auszug)
Hinweis: Alle Kernmetriken sind ab v0.2.1 zusätzlich mit Labels `from` und `to` versehen, damit die zugrunde liegenden Accounts pro Test sichtbar sind.

- mail_e2e_exporter_send_success{route,from,to}
- mail_e2e_exporter_receive_success{route,from,to}
- mail_e2e_exporter_roundtrip_seconds{route,from,to}
- mail_e2e_exporter_last_send_timestamp{route,from,to}
- mail_e2e_exporter_last_receive_timestamp{route,from,to}
- mail_e2e_exporter_test_errors_total{route,from,to,step}
- mail_e2e_exporter_last_error_info{route,from,to}

Zusätzliche Config- und Info‑Metriken:
- mail_e2e_exporter_test_info{route,from,to} = 1 (Mapping der konfigurierten Tests)
- mail_e2e_exporter_config_delete_testmail_after_verify 0|1
- mail_e2e_exporter_config_receive_timeout_seconds
- mail_e2e_exporter_config_receive_poll_seconds
- mail_e2e_exporter_config_check_interval_seconds

Weitere (optional, falls aktiviert):
- mail_e2e_exporter_consecutive_failures{route,phase}
- mail_e2e_exporter_mx_records_total{route}
- mail_e2e_exporter_tls_cert_expiry_days_remaining{route,endpoint}
- mail_e2e_exporter_queue_depth{type}

## Tests & Diagnose (Copy‑Paste)
- Docker‑Logs (laufend):
   ```
  docker compose logs -f mail-e2e-exporter
   ```
- Einmalige letzte 200 Zeilen:
   ```
  docker compose logs --tail 200 mail-e2e-exporter
   ```
- Manuelle Abfrage der wichtigsten Endpunkte:
   ```
  curl -s http://localhost:9782/health | jq .
     ```
     ```
  curl -s http://localhost:9782/info | jq .
     ```
     ```
  curl -s -u "$METRICS_USER:$METRICS_PASS" http://localhost:9782/metrics | head -n 30
   ```
## Sicherheit
- `/metrics` kann per Basic Auth geschützt werden (env: METRICS_USER/METRICS_PASS)
- Rate Limits und IP‑Filter am Reverse Proxy empfohlen

## Troubleshooting
- IMAP: [AUTHENTICATIONFAILED] Authentication failed.
  - Bedeutet: IMAP‑Login fehlgeschlagen (Benutzer/Passwort falsch/leer oder App‑Passwort nötig)
  - Häufig: In `config.yaml` ist `${VAR}` referenziert, aber die Env‑Variable fehlt. Beispiel: `password: ${BRAMOS_TEST_IMAP_PASS}` → in `.env` setzen
  - Abhilfe: `.env` ergänzen → Container neu starten oder `POST /reload` mit API_KEY
  - Mit `DEBUG=true` erscheinen detaillierte Hinweise mit Account/Host und Env‑Key

- Gmail: Mail versendet, aber nicht im INBOX gefunden (Timeout)
  - Gmail nutzt Labels statt echte Ordner. Der Exporter durchsucht automatisch typische Gmail‑Ordner (All Mail/Spam, Lokalisierungen) und nutzt X‑GM‑RAW, mit Fallback auf SUBJECT‑Suche
  - Bei Bedarf `imap.extra_folders` ergänzen und `receive_timeout_seconds` erhöhen



## Grafana Dashboards
Fertige Dashboard-JSONs liegen unter `mail-e2e-exporter/grafana/` und können direkt in Grafana importiert werden.

- mail-e2e-all-in-one.json
  - Ein einziges Dashboard mit allen wichtigen Visualisierungen: Send/Receive Success, Roundtrip, DNS/SMTP Latenzen (p50/p95), Reply Codes, MX-Infos, Queue-Depth und Fehlerübersicht.
  - Zeitbereich: 24h (änderbar in Grafana)
- mail-e2e-overview.json
  - Übersicht über Erfolgsraten (als Stat/Bar‑Gauge), Roundtrip‑Zeiten (Time Series) und Fehler‑Tabelle
- mail-e2e-route-detail.json
  - Detailansicht je Route mit Variable `route` (Multi‑Select, All‑Option)
  - Gauges für Versand/Empfang‑Erfolgsrate (24h), Roundtrip‑Zeit als Zeitreihe, Erfolg (1/0) und Fehlerdetails (7d)
- mail-e2e-errors.json
  - Fehlerfokus: Top fehlerhafte Routen/Schritte als Bar‑Gauge, Fehler je Route/Step als Tabelle
  - Letzte Sende/Empfangs‑Zeitpunkte als Tabelle (ISO‑Zeit)
- mail-e2e-slo.json
  - SLO‑/Verfügbarkeits‑Sicht: Zeitfenster‑Variable `window` (1h…30d) und `slo` (0.99/0.995/0.999)
  - Gesamt‑Verfügbarkeit, Burn‑Rate und pro‑Route‑Sicht sowie Roundtrip‑Durchschnitt/95‑Perzentil

Import in Grafana:
1. In Grafana: Dashboards → New → Import.
2. JSON Datei auswählen (z. B. `mail-e2e-overview.json`).
3. Datasource setzen: Prometheus (Variable `DS_PROMETHEUS`).
4. Dashboard speichern.

Hinweise zu PromQL:
- Verfügbarkeit (Empfang) über Mittelwert des 1/0‑Signals: `avg_over_time(mail_e2e_exporter_receive_success[$window])`
- Erfolgsrate in %: `100 * avg(avg_over_time(mail_e2e_exporter_receive_success[24h]))`
- Fehler pro Route/Step: `sum by (route, step) (increase(mail_e2e_exporter_test_errors_total[24h]))`
- Roundtrip (Durchschnitt): `avg by (route) (avg_over_time(mail_e2e_exporter_roundtrip_seconds[$window]))`
- DNS p95 nach Route/Type: `histogram_quantile(0.95, sum by (le, route, type) (rate(mail_e2e_exporter_dns_lookup_seconds_bucket[$__rate_interval])))`
- SMTP Connect p95: `histogram_quantile(0.95, sum by (le, route) (rate(mail_e2e_exporter_smtp_connect_seconds_bucket[$__rate_interval])))`

Sicherheit:
- Wenn `/metrics` via Basic Auth geschützt ist, muss der Prometheus‑Datasource Benutzer/Passwort kennen (oder Zugriff via interner Netzwerkpfade erfolgen).


## License
Licensed under [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/).  
Free for private and internal (non-commercial) use.  
Attribution required: © 2025 Bjørn Ramos.