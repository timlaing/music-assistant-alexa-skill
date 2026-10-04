# Music Assistant Alexa Skill Prototype
This project provides the Alexa skill service for Music Assistant, with a Flask API and a personal-skill deployment wizard.

For Home Assistant Supervisor, use the maintained [Music Assistant Alexa API add-on](https://github.com/timlaing/music-assistant-alexa-api). This skill repository contains the application; the companion repository packages and configures it for Home Assistant.

## How to Run

#### Prerequisites

- An Amazon developer account: https://developer.amazon.com/en-US/docs/alexa/ask-overviews/create-developer-account.html
- Skill Access Management enabled for your developer account: [https://developer.amazon.com/alexa/console/ask/settings/access-management](https://developer.amazon.com/alexa/console/ask/settings/access-management)
    ![Skill Access Management](assets/screenshots/skill-access-management.png)
- Home Assistant Supervisor for add-on deployment, or Docker and Docker Compose for standalone deployment
- Public HTTPS endpoints for the skill and audio streams; Nginx Proxy Manager can provide both

### 1. Using Docker Compose (standalone)

For a standalone host, run the project with Docker Compose. This will build and start the Alexa skill container with required environment variables, secrets, and a persistent private deployment volume.

#### Steps:

1. Ensure `docker-compose.yml` is present and edit environment variables as needed (e.g., `SKILL_HOSTNAME`, `MA_HOSTNAME`, `PORT`). See the [Environment Variables](#environment-variables) section below for details on each variable.
2. Create `./secrets/app_username.txt` and `./secrets/app_password.txt` to provide `APP_USERNAME` and `APP_PASSWORD` for basic authentication of the web UI and API. Also create `./secrets/lwa_client_secret.txt`: put your Login with Amazon client secret in it, or create an empty file if you will enter the secret later through Setup. Compose requires all three declared secret files to exist, even when you do not use the deployment wizard.
3. Persist `./deployment_data:/data` for private deployment credentials/progress. Register Login with Amazon and configure the client ID and secret, then register the generated callback as described in the [deployment guide](docs/PERSONAL_SKILL_DEPLOYMENT.md).
4. Start the service:

    ```sh
    docker compose up -d
    ```

5. The service will be available at `http://localhost:5000` (or the IP/port you configured).
6. For standalone Docker, open `/setup` and authenticate with app credentials; for the maintained add-on use Home Assistant ingress with no second login. Connect Amazon, explicitly select your existing personal skill (or request creation), review the configuration and deploy. Amazon returns through `/ma-alexa-skill/setup/oauth/callback`, and the original wizard completes the connection automatically. See the [personal skill deployment guide](docs/PERSONAL_SKILL_DEPLOYMENT.md).

The wizard is available in stable **1.3.0**. The maintainer confirmed it was tested and working on 4 October 2026. Login with Amazon security-profile registration is a one-time manual prerequisite; the skill configuration pages are populated through the management API afterwards. The bundled Compose file builds this checkout so it includes this implementation rather than an upstream image.

### 2. Home Assistant add-on

Use [timlaing/music-assistant-alexa-api](https://github.com/timlaing/music-assistant-alexa-api) as the custom add-on repository. Install **Music Assistant Alexa API add-on** on an **aarch64** or **amd64** Home Assistant Supervisor installation.

1. Add `https://github.com/timlaing/music-assistant-alexa-api` in **Settings > Add-ons > Add-on Store > Repositories**.
2. Configure the lowercase Supervisor options `ma_hostname`, `skill_hostname`, `api_username`, `api_password` and `locale` in the add-on. These map to the standalone environment variables described below; do not enter uppercase variable names as Supervisor options. The default locale is `en-US`, and an empty API password is generated and saved on startup.
3. Configure the public HTTPS endpoints described below, start the add-on, open its Web UI and select **Setup**. The guided flow creates or updates the Alexa skill and interaction model for the selected locale.
4. In Music Assistant's Alexa provider, set **API URL** to the add-on LAN base URL, such as `http://<HA-LAN-IP>:5000`, without `/ma`. Use the add-on API username and password for Basic Auth.

The maintained add-on is a separate implementation from the development wrapper in [`addons/music-assistant-skill`](addons/music-assistant-skill/README.md). Use the companion repository's [setup guide](https://github.com/timlaing/music-assistant-alexa-api#configuration) and [option reference](https://github.com/timlaing/music-assistant-alexa-api/blob/main/music-assistant-alexa-api/DOCS.md) for Supervisor deployment.

#### Public HTTPS and Nginx Proxy Manager

| Address | Proxy upstream | Add-on option |
| --- | --- | --- |
| `https://alexa.example.com` | `http://<HA-LAN-IP>:5000` | `skill_hostname` |
| `https://streams.example.com` | `http://<MA-LAN-IP>:8097` | `ma_hostname` |

Expose NPM's HTTPS port **443** to the internet; keep add-on **5000** and Music Assistant stream **8097** internal. Alexa needs public HTTPS access to both the skill and the audio, including on APL devices. Direct internet forwarding of the application ports is unnecessary. Use publicly trusted TLS certificates, and allow signed Alexa POST requests through the public skill proxy without an additional NPM login or access list.

Separate proxy hosts simplify configuration. A skill location such as `/ma-alexa-skill/` must strip that prefix when proxying to the add-on root. Stream locations preserve their paths. Version 1.2.0 supports a public stream URL path prefix without duplicating it during URL rewriting.

If Music Assistant connects directly to the add-on LAN API URL above, no NPM locations are needed for `/alexa/` or `/ma/`. If it connects through a shared public hostname instead, set its Alexa provider **API URL** to that hostname's base URL (for example, `https://music.example.com`, without `/ma` or `/ma-alexa-skill`) and add these NPM custom locations:

| Location | Forward to | Path handling |
| --- | --- | --- |
| `/alexa/` | `http://<HA-LAN-IP>:5000` | Preserve `/alexa/`, including `/alexa/intents`. |
| `/ma/` | `http://<HA-LAN-IP>:5000` | Preserve `/ma/`, including `/ma/push-url`. |

Use the add-on's API username and password in Music Assistant's Basic Auth fields; avoid an additional NPM authentication layer on these API locations. These locations reach the add-on API, not the MA stream server on 8097 or the optional control API on 8095. A dedicated proxy host forwarding all paths to the add-on on 5000 already covers them and needs no custom API locations.

#### 1.2.0 features and validation

Stable **1.2.0** includes merged [add-on PR #28](https://github.com/timlaing/music-assistant-alexa-api/pull/28), application fixes in [skill PR #1](https://github.com/timlaing/music-assistant-alexa-skill/pull/1) and the [publication repair](https://github.com/timlaing/music-assistant-alexa-api/pull/29):

- Optional `ma_api_url` (normally `http://<MA-LAN-IP>:8095`) and `ma_api_token` for mapped voice controls. These control API credentials are separate from the public stream URL and are unnecessary for basic playback.
- `/devices` maps each opaque Alexa device ID to the actual MA `player_id`, rather than its display name. Next, previous and start-over route to MA; pause, stop and resume also synchronize mapped players, with one-shot suppression of commands echoed back by MA.
- Device mappings persist at `/data/device_players.json`, deployment credentials/progress at `/data/skill-deployment.json`, and legacy ASK credentials at `/data/.ask`.
- `enable_apl` defaults to `false`; enable it for Echo Show artwork and controls. `skip_url_validation` defaults to `false`; skipping the local check does not remove Alexa's need for a reachable HTTPS stream.
- Internal service port **5000** remains fixed when changing the host port mapping. `/health` provides unauthenticated process liveness; status pages and APIs use the configured credentials.

Version 1.2.0 passed 46 combined application/add-on tests and HTTP, concurrency and shutdown checks in ARM64 and AMD64 add-on containers, plus CI lint, CodeQL and image builds. On 3 October 2026 the maintainer confirmed playback on a real Alexa device with NPM. This covers the tested installation, not every device or optional feature; the standalone Docker image and bundled development wrapper were not validated by those checks.

### 3. Using `docker run`

Build this checkout to include the candidate wizard. This standalone Dockerfile has not been validated by the maintained add-on container checks. Set the Login with Amazon options and persist `/data` as described in the [deployment guide](docs/PERSONAL_SKILL_DEPLOYMENT.md).

```sh
docker build -t music-assistant-skill:local .
docker run --rm \
    -p 5000:5000 \
    -e SKILL_HOSTNAME=https://alexa.example.com/ \
    -e MA_HOSTNAME=ma.example.com \
    -e APP_USERNAME=/run/secrets/APP_USERNAME \
    -e APP_PASSWORD=/run/secrets/APP_PASSWORD \
    -e LWA_CLIENT_ID=YOUR_CLIENT_ID \
    -e LWA_CLIENT_SECRET=/run/secrets/LWA_CLIENT_SECRET \
    -e SKILL_DEPLOYMENT_PATH=/data/skill-deployment.json \
    -e PORT=5000 \
    -e LOCALE=en-US \
    -v "$(pwd)/ask_data:/root/.ask" \
    -v "$(pwd)/deployment_data:/data" \
    -v "$(pwd)/secrets/lwa_client_secret.txt:/run/secrets/LWA_CLIENT_SECRET:ro" \
    -v "$(pwd)/secrets/app_username.txt:/run/secrets/APP_USERNAME:ro" \
    -v "$(pwd)/secrets/app_password.txt:/run/secrets/APP_PASSWORD:ro" \
    music-assistant-skill:local
```

Notes:
- Adjust `SKILL_HOSTNAME` to the public HTTPS host you'll use in the skill manifest.
- The `ask_data` volume preserves legacy CLI credentials. The new wizard uses its own OAuth client and requires a persistent `/data` volume.
- Mounting files into `/run/secrets` is a simple way to provide secrets for local testing; for production use Docker secrets or your platform's secret manager.

### Environment Variables

For stable **1.3.0**, edit application settings on `/setup`; standalone environment variables are bootstrap defaults until the first web save. Persist `/data/app-settings.json` (or set `APP_SETTINGS_PATH`). Home Assistant add-on users use ingress Setup instead of the add-on Configuration tab. Existing available legacy values migrate once; saved web settings take precedence after restarts. Secrets stay masked, blank inputs retain them, and Credentials provides an explicit API-password reveal for connecting Music Assistant. The callback is derived from the skill endpoint origin; saving does not deploy to Amazon. The unused AWS region setting has been removed.

| Variable | Required | Default | Description |
|---|:---:|:---:|---|
| `SKILL_HOSTNAME` | No | `MA_HOSTNAME` + `/ma-alexa-skill/` | Must be a full publicly reachable HTTPS URL (example: `https://alexa.example.com/`). Should proxy to your open port on this container (port **5000** by default).  Public hostname used in the Alexa skill manifest and to validate the skill endpoint. |
| `MA_HOSTNAME` | Yes for LAN stream URLs | — | Public HTTPS hostname for streams (example: `streams.example.com`, without a scheme), proxied to Music Assistant stream port **8097**. Alexa needs public streams on both screenless and APL devices. The maintained add-on also accepts a full HTTPS base URL in its setup-page `ma_hostname` setting. |
| `APP_USERNAME` | Yes for setup | — | Username for the web UI and API basic authentication. In Docker Compose this is provided via a Docker secret (`/run/secrets/APP_USERNAME`) pointing to `./secrets/app_username.txt`, or as a plain env var when not using secrets. |
| `APP_PASSWORD` | Yes for setup | — | Password for the web UI and API basic authentication. Can be supplied as a Docker secret file or plain env var. |
| `PORT` | No | `5000` | Port the app lives at. Ensure the `ports` mapping in [docker-compose.yml](docker-compose.yml) matches this value. |
| `DEBUG_PORT` | No | `5678` | Remote debug port (if you enable remote debugging). |
| `LOCALE` | *No | `en-US` | ***REQUIRED** if your device is not configured for en-US. Skill locale used by the setup and interaction model operations (examples: `en-US`, `en-GB`, `de-DE`). |
| `TZ` | No | `UTC` | Container timezone (example: `America/Chicago`) to make logs/timestamps match your locale. |
| `SKIP_URL_VALIDATION` | No | `false` | Skip server-side HEAD/GET validation of the rewritten stream URL before sending it to the Echo. Useful when the skill container cannot reach the external stream URL due to Docker network routing (e.g., macvlan isolation, custom outbound firewall rules). |
| `ENABLE_APL` | No | `false` | Enable rich APL rendering (cover art, title, on-screen playback controls) on Echo Show and other APL-capable devices, instead of the plain AudioPlayer-only flow. Disabled by default for playback stability; set to `true` to opt back into screen rendering and live metadata refresh on supported devices. |
| `MA_API_URL` | *No | — | Needed for mapped MA voice controls. Base URL of the Music Assistant control API (normally `http://<MA-LAN-IP>:8095`), used to send commands to the MA player paired with the requesting Echo (see [Device Mapping](#device-mapping) below). |
| `MA_API_TOKEN` | *No | — | ***REQUIRED** alongside `MA_API_URL` if your MA server enforces authentication. A long-lived token created via MA's own auth flow (`auth/token/create`). Can be provided as a Docker secret the same way as `APP_PASSWORD`. |

**Secrets and persistence**

- The example [docker-compose.yml](docker-compose.yml) demonstrates using Docker secrets for `APP_USERNAME` and `APP_PASSWORD` (files in `./secrets`). When using Docker secrets, the container environment will contain the path to the secret file (for example `/run/secrets/APP_PASSWORD`) and the service reads the file content.
- Persist `/data` for the new setup wizard. Legacy ASK credentials in `/root/.ask` belong to another OAuth client and do not skip the new browser connection.

**Notes on values**

- `SKILL_HOSTNAME` must refer to a public HTTPS endpoint reachable by Amazon; it is embedded in the skill manifest and used for endpoint validation.
- If you override `PORT` or `DEBUG_PORT`, update the `ports` mapping in [docker-compose.yml](docker-compose.yml) accordingly (host:container).

## Basic Troubleshooting
### Status Page
`/status`

Returns local API health and the saved personal-skill deployment result, including verification time and selected ID. With no direct API connection, a legacy ASK CLI check is still available. The saved result is not a live Amazon status check. Open `/setup` to review and deploy again.

### Device Mapping
`/devices`

Alexa's Custom Skill API only exposes an opaque, per-skill device id in each request — there is no way to resolve it to a friendly device name or to a Music Assistant player. This page lets you pair each Echo's device id with the corresponding MA `player_id` so voice controls can be routed to the right player. To pair a new device: trigger any voice command from it (e.g. "next"), reload this page (it lists every device id seen in the current session), then enter the matching MA player_id.

Next, Previous and StartOver route to the mapped MA player. Pause, Stop and Resume retain Alexa-side playback control and also synchronize the mapped MA player when MA control credentials are configured. One-shot echo suppression prevents commands spoken back by the MA `alexa` player provider through `alexapy` from being sent to MA again. Basic playback does not require MA control credentials. Persist `DEVICE_MAPPING_PATH` for standalone deployments; the maintained add-on uses `/data/device_players.json`.

### TLS and proxy checks

Use a publicly trusted HTTPS certificate for the skill and stream hosts. Check that Amazon and the Echo can reach these endpoints from outside your LAN. If a proxy's TLS settings prevent connection, allow TLS 1.2 while investigating; the automated add-on checks do not establish compatibility with every TLS/proxy configuration.

---

See [COMPATIBILITY.md](COMPATIBILITY.md) for known supported devices, languages, and regions.

See [LIMITATIONS.md](LIMITATIONS.md) for known limitations.

See [DISCLAIMER.md](DISCLAIMER.md) for security concerns and development disclosures.

For `LWA_CLIENT_ID`, `LWA_CLIENT_SECRET`, `SKILL_CERTIFICATE_TYPE`, `APP_SETTINGS_PATH` and `SKILL_DEPLOYMENT_PATH`, see the [personal skill deployment options and guide](docs/PERSONAL_SKILL_DEPLOYMENT.md).

The maintained add-on exposes `/status` and `/setup` through Home Assistant ingress on private port 8099, with no app login. It blocks these pages on the external 5000 listener. Status also checks skill-health, stream-host and current-audio HTTPS reachability in the background; see the [ingress and callback guide](docs/PERSONAL_SKILL_DEPLOYMENT.md#home-assistant-ingress-and-nginx-proxy-manager).

### Migration release compatibility

Stable 1.3.0 keeps the legacy Supervisor schema with migration-only labels to preserve existing configuration during upgrade. Settings are imported once into `/data/app-settings.json`; all subsequent edits belong in ingress Setup. Later edits to the legacy fields are ignored. The obsolete AWS region option remains removed. Remove this temporary compatibility schema only in a later release once migration is verified.
