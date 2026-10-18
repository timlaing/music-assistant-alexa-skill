FROM ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

# Install Python, Node.js and system dependencies.
# Python 3.14 ships natively in Ubuntu 26.04 (python3.14/python3.14-venv
# from universe), satisfying music-assistant-client's >=3.11 requirement
# without a third-party PPA.
# Node.js 18 via the official NodeSource apt repository (ASK CLI requires a
# modern Node version). The repository is added with a signed-by keyring
# instead of executing the upstream setup script, so no downloaded artifact
# is ever executed unverified.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg && \
    curl --proto '=https' --proto-redir '=https' -fsSL \
        https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --batch --yes --dearmor -o /usr/share/keyrings/nodesource.gpg && \
    printf '%s\n' \
        'deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_18.x nodistro main' \
        > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl-dev \
        nodejs \
        python3-pip \
        python3.14 \
        python3.14-venv && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the hash-locked requirements first, then install them with
# --require-hashes so every wheel is integrity-checked (docker:S8544)
COPY app/requirements.lock.txt /app/requirements.lock.txt

# Install ASK CLI (v2) globally so the container can run `ask configure`.
# Best-effort (--ignore-scripts keeps third-party lifecycle code from running;
# the || true keeps a registry hiccup from failing the image build).
# Then create the app venv with pinned, wheel-only pip tooling and apply the
# verifier.py patch in the same layer (the patch only depends on the venv).
RUN npm install -g --ignore-scripts ask-cli@2.30.7 || true && \
    python3.14 -m venv venv && \
    . venv/bin/activate && \
    pip install --upgrade --only-binary :all: pip==26.2.1 && \
    pip install --require-hashes --only-binary :all: -r requirements.lock.txt && \
    pip install --only-binary :all: debugpy==1.8.22 && \
    /app/venv/bin/python - <<'PY'
import sysconfig, os, sys
try:
        site = sysconfig.get_paths()['purelib']
except Exception:
        print('Could not determine site-packages path; skipping verifier patch')
        sys.exit(0)

verifier_path = os.path.join(site, 'ask_sdk_webservice_support', 'verifier.py')
if not os.path.exists(verifier_path):
        print('verifier.py not found at', verifier_path, '; skipping patch')
        sys.exit(0)

with open(verifier_path, 'r', encoding='utf-8') as f:
        src = f.read()

needle = (
    '        now = datetime.utcnow()\n'
    '        if not (x509_cert.not_valid_before <= now <=\n'
    '                x509_cert.not_valid_after):\n'
    '            raise VerificationException("Signing Certificate expired")'
)
patch = (
    '        from datetime import timezone\n'
    '        now = datetime.now(timezone.utc)\n'
    '        # Use timezone-aware UTC datetimes and updated cryptography properties\n'
    "        not_valid_before = getattr(x509_cert, 'not_valid_before_utc', None) or "
    "x509_cert.not_valid_before.replace(tzinfo=timezone.utc)\n"
    "        not_valid_after = getattr(x509_cert, 'not_valid_after_utc', None) or "
    "x509_cert.not_valid_after.replace(tzinfo=timezone.utc)\n"
    '        if not (not_valid_before <= now <= not_valid_after):\n'
    '            raise VerificationException("Signing Certificate expired")'
)

if needle in src:
    new_src = src.replace(needle, patch)
    backup = verifier_path + '.orig'
    try:
        if not os.path.exists(backup):
            with open(backup, 'w', encoding='utf-8') as b:
                b.write(src)
        with open(verifier_path, 'w', encoding='utf-8') as f:
            f.write(new_src)
        print('Patched', verifier_path, '(backup at', backup + ')')
    except Exception as e:
        print('Failed to write patch:', e)
else:
    print('No patch needed for verifier.py')
PY

# Now copy the rest of your source code (commented out for dynamic development)
COPY app /app/src
# Copy the skill manifest and related app files so runtime can find app/skill.json
# This ensures /app/app/skill.json exists inside the container for the create script.
COPY app /app/app
# Copy repository-level assets (icons, images) into the container so favicons are available
COPY assets /app/assets
# Copy top-level helper scripts so runtime can execute them (ask_create_skill.sh)
COPY scripts /app/scripts
RUN chmod +x /app/scripts/ask_create_skill.sh || true

# Amazon Skill & Host Configuration
ENV AWS_DEFAULT_REGION=us-east-1

# Timezone (defaults to UTC) — can be overridden at runtime via TZ env
ENV TZ=UTC

# Host configuration:
# MA_HOSTNAME: hostname for the Music Assistant stream
# SKILL_HOSTNAME: hostname used when creating the Alexa skill manifest and endpoints
ENV MA_HOSTNAME=""
ENV SKILL_HOSTNAME=""
ENV PORT=5000
ENV LOCALE=en-US
# When set to 1 (default) we reduce HTTP request log noise (werkzeug/urllib3)
ENV QUIET_HTTP=1

# Debugging Configuration
ARG DEBUG_PORT=0
 # default 0 (disabled); launch.json default 5678
ENV DEBUG_PORT=${DEBUG_PORT}

# Expose the port the app runs on
EXPOSE ${PORT}

# If DEBUG_PORT is empty or set to 0, run without debugpy. Otherwise start debugpy.
CMD ["/bin/sh", "-lc", "if [ -n \"${DEBUG_PORT}\" ] && [ \"${DEBUG_PORT}\" != \"0\" ]; then exec /app/venv/bin/python -m debugpy --listen 0.0.0.0:${DEBUG_PORT} src/app.py; else exec /app/venv/bin/python src/app.py; fi"]
