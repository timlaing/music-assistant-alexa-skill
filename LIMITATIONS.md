## Technical Limitations

- Alexa requires publicly reachable HTTPS audio streams on both screenless and APL devices. A reverse proxy such as Nginx Proxy Manager can expose them on 443 while the MA stream server remains internal on 8097.
- Independent simultaneous streams remain an upstream limitation; the maintenance update does not add per-device stream storage.
- The maintained Home Assistant add-on's 1.2.0-beta.1 candidate has automated ARM64/AMD64 validation. Live HA/Echo/NPM acceptance remains required before stable release. See the [deployment and validation notes](README.md#2-home-assistant-add-on).

## Known Issues

### All devices: 
- Skill session does not persist on AlexaPy device commands
- Alexa groups (including stereo) are not supported at this time

### APL devices:
 - A follow up prompt will continously stay open because of the constant metadata refresh
