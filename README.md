# Music Assistant Alexa Skill Prototype
This project provides the Alexa skill service for Music Assistant, with a Flask API and guided ASK CLI setup.

For Home Assistant Supervisor, use the maintained [Music Assistant Alexa API add-on](https://github.com/timlaing/music-assistant-alexa-api). This skill repository contains the application; the companion repository packages and configures it for Home Assistant.

## How to Run

#### Prerequisites

- An Amazon developer account: https://developer.amazon.com/en-US/docs/alexa/ask-overviews/create-developer-account.html
- Skill Access Management enabled for your developer account: [https://developer.amazon.com/alexa/console/ask/settings/access-management](https://developer.amazon.com/alexa/console/ask/settings/access-management)
    ![Skill Access Management](assets/screenshots/skill-access-management.png)
- Home Assistant Supervisor for add-on deployment, or Docker and Docker Compose for standalone deployment
- Public HTTPS endpoints for the skill and audio streams; Nginx Proxy Manager can provide both

### 1. Using Docker Compose (standalone)

For a standalone host, run the project with Docker Compose. This will build and start the Alexa skill container with required environment variables, secrets, and an optional persistent ASK credential volume.

#### Steps:

1. Ensure `docker-compose.yml` is present and edit environment variables as needed (e.g., `SKILL_HOSTNAME`, `MA_HOSTNAME`, `PORT`). See the [Environment Variables](#environment-variables) section below for details on each variable.
2. (Optional) Create `./secrets/app_username.txt` and `./secrets/app_password.txt` to provide `APP_USERNAME` and `APP_PASSWORD` for basic authentication of the web UI and API.
3. (Optional) To persist ASK CLI credentials across container restarts, mount a volume to `./<host directory>:/root/.ask`, `./ask_data` is used by default.
4. Start the service:

    ```sh
    docker compose up -d
    ```

5. The service will be available at `http://localhost:5000` (or the IP/port you configured).
6. In your browser, open the setup UI at `http://localhost:5000/setup`. The setup page will:
   - detect existing persistent ASK credentials (if present) and skip the browser-based auth flow
   - guide you through the ASK CLI authorization flow if credentials are not present
   - run the automated skill creation/update, interaction model upload, model build polling, and testing enablement.

Note: manual creation of the skill in the Alexa Developer Console is no longer required — the `/setup` flow automates creation and enablement when possible.

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

Separate proxy hosts simplify configuration. A skill location such as `/ma-alexa-skill/` must strip that prefix when proxying to the add-on root. Stream locations preserve their paths. The 1.2.0 candidate supports a public stream URL path prefix without duplicating it during URL rewriting.

#### 1.2.0 candidate features and validation

The experimental **1.2.0-beta.1** update is tracked in [add-on PR #28](https://github.com/timlaing/music-assistant-alexa-api/pull/28), with application fixes in [skill PR #1](https://github.com/timlaing/music-assistant-alexa-skill/pull/1). These features require that candidate; they are not a claim that it has already been released:

- Optional `ma_api_url` (normally `http://<MA-LAN-IP>:8095`) and `ma_api_token` for mapped voice controls. These control API credentials are separate from the public stream URL and are unnecessary for basic playback.
- `/devices` maps each opaque Alexa device ID to the actual MA `player_id`, rather than its display name. Next, previous and start-over route to MA; pause, stop and resume also synchronize mapped players, with one-shot suppression of commands echoed back by MA.
- Device mappings persist at `/data/device_players.json`, and ASK credentials at `/data/.ask`.
- `enable_apl` defaults to `false`; enable it for Echo Show artwork and controls. `skip_url_validation` defaults to `false`; skipping the local check does not remove Alexa's need for a reachable HTTPS stream.
- Internal service port **5000** remains fixed when changing the host port mapping. `/health` provides unauthenticated process liveness; status pages and APIs use the configured credentials.

The candidate passed 46 combined application/add-on tests and HTTP, concurrency and shutdown checks in ARM64 and AMD64 add-on containers, plus CI lint, CodeQL and image builds. Live Home Assistant/Echo/NPM acceptance is still required before stable release; the standalone Docker image and bundled development wrapper were not validated by those checks.

### 3. Using `docker run`

The following example runs the **upstream standalone image**. It does not install the maintained Home Assistant add-on or guarantee inclusion of this fork's stability fixes. Replace the image tag with the release or digest you intend to run.

```sh
docker run --rm \
    -p 5000:5000 \
    -e SKILL_HOSTNAME=alexa.example.com \
    -e MA_HOSTNAME=ma.example.com \
    -e PORT=5000 \
    -e LOCALE=en-US \
    -e AWS_DEFAULT_REGION=us-east-1 \
    -v "$(pwd)/ask_data:/root/.ask" \
    -v "$(pwd)/secrets/app_username.txt:/run/secrets/APP_USERNAME:ro" \
    -v "$(pwd)/secrets/app_password.txt:/run/secrets/APP_PASSWORD:ro" \
    ghcr.io/alams154/music-assistant-alexa-skill-prototype:latest
```

Notes:
- Adjust `SKILL_HOSTNAME` to the public HTTPS host you'll use in the skill manifest.
- The `ask_data` volume persists ASK CLI credentials so the setup flow can reuse them.
- Mounting files into `/run/secrets` is a simple way to provide secrets for local testing; for production use Docker secrets or your platform's secret manager.

### Environment Variables

| Variable | Required | Default | Description |
|---|:---:|:---:|---|
| `SKILL_HOSTNAME` | Yes | — | Must be a publicly reachable HTTPS host (example: `alexa.example.com`). Should proxy to your open port on this container (port **5000** by default).  Public hostname used in the Alexa skill manifest and to validate the skill endpoint. |
| `MA_HOSTNAME` | Yes for LAN stream URLs | — | Public HTTPS hostname for streams (example: `streams.example.com`, without a scheme), proxied to Music Assistant stream port **8097**. Alexa needs public streams on both screenless and APL devices. The maintained add-on candidate also accepts a full HTTPS base URL in its separate `ma_hostname` option. |
| `APP_USERNAME` | No | — | Username for the web UI and API basic authentication. In Docker Compose this is provided via a Docker secret (`/run/secrets/APP_USERNAME`) pointing to `./secrets/app_username.txt`, or as a plain env var when not using secrets. |
| `APP_PASSWORD` | No | — | Password for the web UI and API basic authentication. Can be supplied as a Docker secret file or plain env var. |
| `PORT` | No | `5000` | Port the app lives at. Ensure the `ports` mapping in [docker-compose.yml](docker-compose.yml) matches this value. |
| `DEBUG_PORT` | No | `5678` | Remote debug port (if you enable remote debugging). |
| `LOCALE` | *No | `en-US` | ***REQUIRED** if your device is not configured for en-US. Skill locale used by the setup and interaction model operations (examples: `en-US`, `en-GB`, `de-DE`). |
| `AWS_DEFAULT_REGION` | No | `us-east-1` | AWS region used by ASK CLI operations when applicable. |
| `TZ` | No | `UTC` | Container timezone (example: `America/Chicago`) to make logs/timestamps match your locale. |
| `SKIP_URL_VALIDATION` | No | `false` | Skip server-side HEAD/GET validation of the rewritten stream URL before sending it to the Echo. Useful when the skill container cannot reach the external stream URL due to Docker network routing (e.g., macvlan isolation, custom outbound firewall rules). |
| `ENABLE_APL` | No | `false` | Enable rich APL rendering (cover art, title, on-screen playback controls) on Echo Show and other APL-capable devices, instead of the plain AudioPlayer-only flow. Disabled by default for playback stability; set to `true` to opt back into screen rendering and live metadata refresh on supported devices. |
| `MA_API_URL` | *No | — | Needed for mapped MA voice controls. Base URL of the Music Assistant control API (normally `http://<MA-LAN-IP>:8095`), used to send commands to the MA player paired with the requesting Echo (see [Device Mapping](#device-mapping) below). |
| `MA_API_TOKEN` | *No | — | ***REQUIRED** alongside `MA_API_URL` if your MA server enforces authentication. A long-lived token created via MA's own auth flow (`auth/token/create`). Can be provided as a Docker secret the same way as `APP_PASSWORD`. |

**Secrets and persistence**

- The example [docker-compose.yml](docker-compose.yml) demonstrates using Docker secrets for `APP_USERNAME` and `APP_PASSWORD` (files in `./secrets`). When using Docker secrets, the container environment will contain the path to the secret file (for example `/run/secrets/APP_PASSWORD`) and the service reads the file content.
- To persist ASK CLI credentials between container runs, mount a host directory as `/root/.ask` (the example uses `./ask_data:/root/.ask`). This allows the setup flow to reuse existing ASK credentials and skip the browser auth flow when present.

**Notes on values**

- `SKILL_HOSTNAME` must refer to a public HTTPS endpoint reachable by Amazon; it is embedded in the skill manifest and used for endpoint validation.
- If you override `PORT` or `DEBUG_PORT`, update the `ports` mapping in [docker-compose.yml](docker-compose.yml) accordingly (host:container).

## Basic Troubleshooting
### Status Page
`/status`

Returns a simple status page showing the local API health and an ASK CLI driven check for whether the Music Assistant skill exists, whether its endpoint matches `SKILL_HOSTNAME`, and whether testing is enabled. When the check is not green, the status page provides a quick link to `/setup`.

### Device Mapping
`/devices`

Alexa's Custom Skill API only exposes an opaque, per-skill device id in each request — there is no way to resolve it to a friendly device name or to a Music Assistant player. This page lets you pair each Echo's device id with the corresponding MA `player_id` so voice controls can be routed to the right player. To pair a new device: trigger any voice command from it (e.g. "next"), reload this page (it lists every device id seen in the current session), then enter the matching MA player_id.

Next, previous and start-over route to MA. Pause, stop and resume also synchronize mapped players when MA control credentials are configured, while retaining Alexa AudioPlayer handling and one-shot suppression of commands echoed back by MA. Basic playback does not require MA control credentials. Persist `DEVICE_MAPPING_PATH` for standalone deployments; the maintained add-on uses `/data/device_players.json`.

### TLS and proxy checks

Use a publicly trusted HTTPS certificate for the skill and stream hosts. Check that Amazon and the Echo can reach these endpoints from outside your LAN. If a proxy's TLS settings prevent connection, allow TLS 1.2 while investigating; the automated add-on checks do not establish compatibility with every TLS/proxy configuration.

---

See [COMPATIBILITY.md](COMPATIBILITY.md) for known supported devices, languages, and regions.

See [LIMITATIONS.md](LIMITATIONS.md) for known limitations.

See [DISCLAIMER.md](DISCLAIMER.md) for security concerns and development disclosures.
