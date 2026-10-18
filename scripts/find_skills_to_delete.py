#!/usr/bin/env python3
import sys
import json
from pathlib import Path

SKILL_NAME = "music assistant"

if len(sys.argv) < 2:
    print("")
    sys.exit(0)
# Only ever read an existing .json file, so a stray argument can never
# redirect the read elsewhere (path traversal).
path = Path(sys.argv[1]).resolve()
if path.suffix != ".json" or not path.is_file():
    print("")
    sys.exit(0)
try:
    data = json.loads(path.read_text())
except Exception:
    print("")
    sys.exit(0)
ids = []
for item in data.get("skills", []) if isinstance(data, dict) else []:
    if isinstance(item, dict):
        name = item.get("skillName") or item.get("name") or item.get("skillNameLocal")
        # also check nameByLocale mapping
        if not name and isinstance(item.get("nameByLocale"), dict):
            # prefer en-US if available
            name = item["nameByLocale"].get("en-US") or next(
                iter(item["nameByLocale"].values()), None
            )
        sid = item.get("skillId") or item.get("id")
        if sid and name and name.strip().lower() == SKILL_NAME:
            ids.append(sid)


def walk(obj):
    if isinstance(obj, dict):
        if ("skillId" in obj) and (
            obj.get("name", "").lower() == SKILL_NAME
            or obj.get("skillName", "").lower() == SKILL_NAME
        ):
            ids.append(obj.get("skillId"))
        for v in obj.values():
            walk(v)
    elif isinstance(obj, list):
        for v in obj:
            walk(v)


walk(data)
print(" ".join([i for i in ids if i]))
