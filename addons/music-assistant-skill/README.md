# Bundled Home Assistant development wrapper

For Home Assistant Supervisor deployment, install the maintained [Music Assistant Alexa API add-on](https://github.com/timlaing/music-assistant-alexa-api) from that companion repository. Follow its [setup guide](https://github.com/timlaing/music-assistant-alexa-api#configuration) and [option reference](https://github.com/timlaing/music-assistant-alexa-api/blob/main/music-assistant-alexa-api/DOCS.md).

This directory contains the original development wrapper. It uses a different add-on definition and upstream image; it is not the maintained `ma_alexa_api` add-on, and its standalone packaging was not included in the companion add-on's validation.

The maintained add-on supports aarch64 and amd64. Its experimental 1.2.0-beta.1 update includes threaded playback fixes, optional MA voice controls, persistent device mappings and APL settings. Track availability and remaining live-device acceptance in [add-on PR #28](https://github.com/timlaing/music-assistant-alexa-api/pull/28).

For networking and configuration details, see the [Home Assistant section of the skill README](../../README.md#2-home-assistant-add-on). Nginx Proxy Manager can expose the skill and streams over public HTTPS on 443 while application ports 5000 and 8097 remain internal. Use the lowercase Supervisor options from the maintained add-on; this development wrapper's uppercase options are a separate configuration.
