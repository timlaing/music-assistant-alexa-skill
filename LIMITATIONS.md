## Technical Limitations

- Alexa requires publicly reachable HTTPS audio streams on both screenless and APL devices. A reverse proxy such as Nginx Proxy Manager can expose them on 443 while the MA stream server remains internal on 8097.
- Independent simultaneous streams remain an upstream limitation; the maintenance update does not add per-device stream storage.
- The maintained Home Assistant add-on's stable 1.2.0 has automated ARM64/AMD64 validation and maintainer-confirmed real-device playback through NPM. Coverage of other devices and optional features remains limited. See the [deployment and validation notes](README.md#2-home-assistant-add-on).

## Known Issues

### All devices: 
- Skill session does not persist on AlexaPy device commands
- Alexa groups (including stereo) are not supported at this time

### APL devices:
 - A follow up prompt will continously stay open because of the constant metadata refresh
