# Fallback Trading — Android app

A native Android client for the Fallback web trading platform (the Flask
dashboard in the repository root). Built with **Kotlin + Jetpack Compose
(Material 3)**, targeting **Android 14+** (`minSdk 34`, `targetSdk 36`).

It talks to the existing backend's JSON API — no backend changes are required.

## Features

| Tab | Backend endpoint | What it does |
|-----|------------------|--------------|
| **Portfolio** | `GET /api/account` + `/open_positions` + `/closed_orders` | Animated equity hero, cumulative realized-P/L curve, allocation donut, quick stats (unrealized/realized P/L, win rate) |
| **Positions** | `GET /api/open_positions`, `POST /api/proxy_trade` | Live open positions with unrealized P/L; per-row **Trade** and one-tap **Close** |
| **Trade** (slide-up sheet) | `POST /api/proxy_trade`, `GET /api/tradable_symbols` | Raised center FAB opens a Buy/Sell order sheet: market or limit, time-in-force, extended hours, symbol validation + autocomplete |
| **History** | `GET /api/closed_orders` | Closed trades with realized P/L, %, strategy label |
| **Insights** | `POST /api/stock_intelligence/ask` & `/analysis` | Ask the news-aware LLM about symbols; view the standing per-symbol analysis |

## Admin mode

The app detects a superuser at login (by reading the admin routing control the
dashboard renders) and adapts automatically:

- **Normal user** — sees only their own account, positions, history and trades.
- **Admin** — gets a **scope selector** in the top bar to switch between
  **All users** (aggregated equity/positions/history), the **Pooled account**, or
  **any individual user**. Switching scope re-loads every tab. The Portfolio adds
  a **per-user summary** (equity, open P/L, open trades), positions show which
  user owns them, closes are routed to that user, and new orders follow the
  selected scope (single / all users / pool) — the same routing the web dashboard
  offers. No backend changes are required.

## Releasing & auto-updates

The app self-updates from **GitHub Releases** (`UpdateManager` + `UpdateHost`):
it checks the repo's latest release on launch and via **⋮ → Check for updates**,
and if a newer version exists it downloads the signed APK and launches the
installer (needs the one-time "install unknown apps" permission).

To ship a new version:

1. Bump `versionCode` / `versionName` in `app/build.gradle.kts`.
2. Commit, then tag and push:
   ```bash
   git tag v1.1.0 && git push origin v1.1.0
   ```
3. `.github/workflows/android-release.yml` builds + **signs** the APK and
   publishes a GitHub Release. Installed apps pick it up automatically.

> CI signing requires four repository secrets (Settings → Secrets and variables →
> Actions): `KEYSTORE_BASE64`, `KEYSTORE_PASSWORD`, `KEY_ALIAS`, `KEY_PASSWORD`.
> Every release **must** be signed with the same key as the installed app, or the
> update won't install over it.

## Design

A modern, eToro-style dark UI built entirely in Compose:

- Custom brand dark theme (deep navy surfaces, green/red P/L semantics, rounded shapes).
- **Canvas-drawn charts** — an animated area/line chart for the portfolio curve and an
  animated allocation donut — with **no charting dependency**.
- Animated money counter, green/red change pills, animated tab transitions.
- A bottom bar with a **raised gradient center "Trade" button** that opens the order
  entry as a `ModalBottomSheet` (also reachable by tapping **Trade** on any position).

## How authentication works

The Flask app uses **session-cookie auth plus a CSRF token** embedded in every
page as `<meta name="csrf-token">`. The app mirrors the browser flow:

1. `GET /login` → scrape the CSRF token, receive the session cookie.
2. `POST /login` (form-encoded `username`/`password`/`csrf_token`).
3. On the redirect to the dashboard, re-scrape the refreshed CSRF token.
4. Every state-changing request carries the `X-CSRF-Token` header
   (`CsrfInterceptor`); the token is re-fetched and the request retried once if
   the server reports `csrf_failed`.

The session cookie is stored in **`EncryptedSharedPreferences`** so you stay
logged in across restarts (the server session lasts ~12h). Automatic redirects
are disabled in OkHttp so a `302 → /login` is cleanly detected as an expired
session and bounces you back to the login screen.

## Architecture

```
ui/            Jetpack Compose screens + ViewModels (MVVM, StateFlow)
  navigation/  Outer nav: splash → server setup → login → main (bottom nav)
  main/        Scaffold with bottom navigation + inner NavHost (5 tabs)
  server, login, account, positions, trade, history, intelligence
data/          Repository, Retrofit API, DTOs, cookie jar, secure/settings stores
```

- **No DI framework** — a small `AppContainer` (in `TradingApp.kt`) wires
  everything; ViewModels are created via `viewModelFactory`.
- **Networking** — Retrofit + OkHttp + Moshi. The base URL is user-configured
  at runtime, so the Retrofit instance is rebuilt whenever it changes.

## Building & running

1. Open the `android/` folder in Android Studio (it already has a Gradle
   wrapper; first sync downloads Gradle 8.13 + the SDK pieces it needs).
2. Run the `app` configuration on an emulator or device running **Android 14+**.

From the command line:

```bash
cd android
./gradlew assembleDebug          # app/build/outputs/apk/debug/app-debug.apk
./gradlew assembleRelease        # minified + signed APK (see below)
```

The SDK location is read from `local.properties` (`sdk.dir`). Android Studio
rewrites this automatically; it is intentionally **not** committed.

## Release signing

The release build is signed from a keystore referenced by `keystore.properties`
(at the `android/` root). Both files are **gitignored** — secrets never enter
version control:

```
android/fallback-release.jks      # the keystore
android/keystore.properties       # storeFile / storePassword / keyAlias / keyPassword
```

`./gradlew assembleRelease` produces a signed
`app/build/outputs/apk/release/app-release.apk` (APK Signature Scheme v2,
sufficient for `minSdk 34`). If `keystore.properties` is missing, the same
command still builds an *unsigned* release instead of failing.

> ⚠️ **Back up the keystore and its password.** If you lose them you cannot ship
> updates to an app already published under this key. To create a fresh key:
>
> ```bash
> keytool -genkeypair -v -keystore fallback-release.jks -alias fallback \
>   -keyalg RSA -keysize 2048 -validity 10000
> ```
> then update `keystore.properties` to match.

For a Play Store upload, build an **App Bundle** instead with
`./gradlew bundleRelease` (`app/build/outputs/bundle/release/app-release.aab`).

## First-run setup

On first launch the app asks for your **server URL** (e.g.
`https://trading.example.com`, including any sub-path the dashboard is mounted
under). Then sign in with the same username/password you use on the web.

### HTTPS / cleartext note

The backend ships with `SESSION_COOKIE_SECURE=true`, i.e. it expects TLS, and
the app **enforces HTTPS** by default (`res/xml/network_security_config.xml`).
Plain HTTP is allowed only for local dev hosts (`localhost`, `127.0.0.1`,
`10.0.2.2` for the emulator). To test against a LAN server over HTTP, add its
exact host there, or serve the dashboard over HTTPS.

## Notes / scope

- Built for the **regular-user** view. Admin/superuser-only screens
  (multi-user routing, optimizer/strategy lab, server controls) are not
  included.
- `versionName 1.0.0`, `versionCode 1` — bump in `app/build.gradle.kts` for
  store releases, and add a signing config for a signed release APK/AAB.
