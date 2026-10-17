# AGENTS.md — Music Assistant Alexa skill

## Layout

- `app/` — the skill/API application (Python 3.12, served by Flask; the
  add-on serves the same code under gunicorn). `tests/` — pytest suite.
- `Dockerfile` — Ubuntu 22.04 image: Python 3.12 venv at `/app/venv` (with
  the verifier.py + certvalidator/registry.py patches applied), Node + ASK
  CLI, code at `/app/src` (+ `/app/app`), assets, scripts.

## Releasing (skill + image + add-on chain)

1. Cuts go through release-drafter: the draft auto-bumps to `vX.Y.(Z+1)`;
   retarget and publish with:
   `gh release edit <draft> --tag vX.Y.Z --title 'Music Assistant Alexa Skill X.Y.Z' --target main`
   then `gh release edit <draft> --draft=false`.
2. Publishing the release triggers `docker-ghcr.yml` (`release: published`):
   image `ghcr.io/timlaing/music-assistant-skill:vX.Y.Z` (plus `X.Y.Z` and
   `latest`) is built multi-arch and cosign-signed keyless. Pushes to `main`
   publish only `edge`/`sha-<short>` and never move release tags.
   - Repair/backfill a tag: `gh workflow run docker-ghcr.yml -f image_tag=vX.Y.Z`.
   - Publishing a release does NOT fire a tag-push build (the release creates
     the tag); the release trigger is what makes the image tag reliable.
3. After a release that should reach the add-on, bump the image reference in
   the `music-assistant-alexa-api` repo: `music-assistant-alexa-api/build.yaml`
   (`build_from`, both arches) and the Dockerfile `ARG BUILD_FROM=` default,
   tag + digest — see that repo's AGENTS.md for the exact recipe.

## Tests and lint

- `python -m pytest tests/ -q`
- `ruff check .` (no format gate; do not mass-reformat).
- CI also builds the image on PRs (`pull_request` path — build, no push, so
  the GHCR token is never exposed to PR code).

## Rules

- PRs only; squash-merge; never push to `main`. If `gh pr merge` reports
  `BLOCKED` with all checks green, use the REST merge:
  `gh api -X PUT repos/timlaing/music-assistant-alexa-skill/pulls/N/merge -f merge_method=squash`.
- Commits are GPG-signed; never commit or log credentials (Amazon/LWA keys
  live in `/data` at runtime only).
