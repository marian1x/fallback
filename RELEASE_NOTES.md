# Release Notes

## Version 2.1.0 - 2026-06-02

### Strategy Research / Optimization
- Added `misc/pine_optimizer.py` to run local backtests and parameter search directly from your Pine strategy logic.
- Optimizer can:
  - read defaults from `misc/keltner.pine`,
  - use TradingView export metadata from XLSX as a baseline,
  - fetch bars from Alpaca (`iex` / `sip`) or local CSV,
  - rank best parameter combinations and export JSON + CSV reports.

### Documentation / Repository Hygiene
- Added optimizer usage documentation:
  - `misc/README_optimizer.md`
  - README section `Local Pine Optimizer`
- Added optimizer/report artifacts to `.gitignore` to keep the repo clean.
- Added required optimizer dependencies to `requirements.txt`:
  - `numpy`
  - `pandas`
  - `openpyxl`

## Version 2.0.1 - 2026-06-01

### Deployment / Runtime
- Migrated both services from Flask dev server to Gunicorn under `systemd`:
  - `fallback.service` -> `bot:app` on `127.0.0.1:5000`
  - `fallback_dashboard.service` -> `dashboard:app` on `127.0.0.1:5050`
- Added `gunicorn` to `requirements.txt`.

### Security / User Management
- Added self-service password change for any logged-in user from **Configuration** page.
- Kept admin password reset flow and added frontend `minlength` validation in admin user management forms.

### UI / Trading UX
- Fixed close-trade notice rendering (`[object Object]`) by parsing nested webhook/proxy responses correctly before display.

## Version 2.0.0 - 2026-06-01

### Security
- Added CSRF protection for all state-changing dashboard endpoints (forms + AJAX `X-CSRF-Token`).
- Hardened session cookies (`HttpOnly`, `SameSite=Lax`, configurable `Secure`) and session lifetime.
- Added Flask security response headers (CSP, Referrer Policy, Frame Options, Permissions Policy, HSTS when HTTPS).
- Added login brute-force protection with configurable in-memory rate limiting per client IP.
- Fixed open redirect risk on login `next` parameter by validating target URL.
- Moved logout to `POST` only.
- Switched internal API key validation to constant-time comparison.

### Trading Bot / Strategy Safety
- Added webhook payload size checks and strict JSON validation.
- Added optional webhook secret validation (`WEBHOOK_SECRET`) via header or payload passphrase.
- Added duplicate signal suppression window to avoid repeated executions from retries.
- Added configurable risk gates:
  - minimum / maximum trade amount,
  - maximum account allocation percent per order,
  - maximum open positions per account.

### UI / Mobile / UX
- Improved login and register screens for mobile and desktop (cleaner auth cards, better spacing, stronger form defaults).
- Added `SweetAlert2` globally (fixes missing `Swal` runtime usage in manual trading flow).
- Refined dashboard layout for responsiveness and readability on smaller screens.
- Improved global visual polish (card depth, navbar interactions, mobile spacing and touch targets).

### Repository Hygiene
- Cleaned `.gitignore` to exclude runtime artifacts and local environments.
- Removed unused templates:
  - `templates/backup_restore.html`
  - `templates/config.html`
  - `templates/logs_list.html`
  - `templates/reinit_db_confirm.html`
- Removed tracked runtime logs from Git index.

### Operational Notes
- Existing reverse-proxy HTTPS setup remains valid.
- For best security, keep dashboard and bot bound to localhost and expose only through Nginx TLS.
- Configure these new env vars as needed:
  - `PASSWORD_MIN_LENGTH`
  - `SESSION_LIFETIME_MINUTES`
  - `LOGIN_RATE_LIMIT_WINDOW_SEC`
  - `LOGIN_RATE_LIMIT_MAX_ATTEMPTS`
  - `SESSION_COOKIE_SECURE`
  - `WEBHOOK_SECRET`
  - `WEBHOOK_SECRET_HEADER`
  - `MIN_TRADE_AMOUNT`
  - `MAX_TRADE_AMOUNT`
  - `MAX_ACCOUNT_ALLOCATION_PCT`
  - `MAX_OPEN_POSITIONS_PER_ACCOUNT`
  - `SIGNAL_DEDUP_WINDOW_SEC`
  - `MAX_WEBHOOK_CONTENT_LENGTH`
