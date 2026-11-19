# PC self-hosting (Mafutani backend)

This directory provides a practical way to host Mafutani entirely on this PC.

> **Status:** Mafutani is experimental/unaudited. This setup improves operability, not safety/compliance/correctness guarantees.

## Option A: Docker Compose (recommended for full stack)

Includes: **Mafutani** (backend+SPA), **Traefik** (reverse proxy), **Prometheus** + **node-exporter**, **Grafana**.

```sh
cd deploy
docker compose up -d --build
```

Endpoints (defaults):
- App (direct): http://localhost:8000
- App (via Traefik): http://localhost/ (or `http://mafutani.localhost/` if resolved)
- Traefik dashboard: http://localhost:8080
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3001 (admin/admin by default — **change immediately**)

Notes:
- Port 3001 is used for Grafana to avoid conflicts with existing services on 3000.
- Data persisted via mounts to `../data` and `../chain`.
- Mafutani has no native Prometheus `/metrics` endpoint yet; host metrics are collected via node-exporter.
- For public HTTPS, enable Traefik `websecure` + ACME in `docker-compose.yml` and open 443 with proper DNS.

## Option B: Native systemd service (backend only)

```sh
sudo cp deploy/mafutani.service /etc/systemd/system/mafutani.service
sudo systemctl daemon-reload
sudo systemctl enable --now mafutani
sudo systemctl status mafutani
```

Edit paths/user/env in the unit as needed. Access at `http://<pc-ip>:8000`.

## Option C: Direct run (dev)

```sh
cd /home/minima/Documents/Projects/Mafutani-main
PORT=8000 MAFUTANI_NO_FEED=1 python3 app.py
```

## Serving Netlify frontend from this PC backend

1. Run backend (above) and expose it via an HTTPS tunnel from this PC (e.g. Cloudflare Tunnel).
2. Update `web/_redirects` proxy targets to the tunnel URL and redeploy Netlify.
3. For cross-origin cookie auth set `MAFUTANI_COOKIE_SAMESITE_NONE=1` on the backend and ensure TLS/proxy headers are correct.

## Anything else commonly needed

- **Firewall:** allow required ports (80/443/8000 as used); restrict dashboards (8080/9090/3001) to trusted networks.
- **TLS:** terminate with Traefik+ACME or a trusted tunnel; avoid exposing plain HTTP publicly.
- **Backups:** regularly back up `data/trading.db`, `chain/mft.db` (if used), and wallet/seed/state files — **never restore stale PQC wallet state**.
- **Secrets:** keep `.env`, Daraja creds, and wallet seeds out of git and in secure stores.
- **Monitoring:** add app-level metrics/log shipping if needed; current stack provides infra visibility only.
- **Reverse proxy headers:** if behind Traefik/tunnel, configure trusted proxy handling as appropriate for your deployment.
- **Updates:** pull/review changes manually; do not auto-apply untrusted external code/config.
