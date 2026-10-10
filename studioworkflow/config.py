"""Validate workspace configuration before touching mirrors or starting a server."""

import json
import math
from pathlib import Path
import re

CONFIG_NAME = "studio-workflow.json"
MAX_PLACE_ID = 2**53 - 1
RESERVED_NAMES = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                  *(f"lpt{i}" for i in range(1, 10))}


def validate_places(places):
    if not isinstance(places, dict) or not places:
        raise ValueError("Configure at least one place using alias=PLACE_ID")
    ids = set()
    for name, definition in places.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or name in RESERVED_NAMES:
            raise ValueError("Place aliases must be safe lowercase names, such as lobby or arena")
        if not isinstance(definition, dict):
            raise ValueError("Each place needs a place_id")
        place_id = definition.get("place_id")
        if type(place_id) is not int or not 0 < place_id <= MAX_PLACE_ID:
            raise ValueError("Place IDs must be positive safe integers; use Place ID, not Universe ID")
        if place_id in ids:
            raise ValueError("Duplicate Place ID")
        ids.add(place_id)
    return places


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    config = dict(config)
    if type(config.get("version")) is not int or config["version"] != 1:
        raise ValueError("Unsupported configuration version; expected version 1")
    validate_places(config.get("places"))
    port = config.get("port")
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("Port must be an integer between 1024 and 65535")
    identity = config.get("installation_id")
    if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{16}", identity):
        raise ValueError("Invalid installation_id; run init in a new workspace")
    defaults = {"heartbeat_timeout_seconds": 20, "auto_sync": True,
                "sync_debounce_seconds": 1.5, "minimum_observer_protocol": 3}
    for name, default in defaults.items():
        config.setdefault(name, default)
    if type(config["auto_sync"]) is not bool:
        raise ValueError("auto_sync must be true or false")
    for name, low, high in (("heartbeat_timeout_seconds", 5, 300), ("sync_debounce_seconds", 0, 60)):
        value = config[name]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{name} must be a finite number between {low} and {high}")
    protocol = config["minimum_observer_protocol"]
    if type(protocol) is not int or not 1 <= protocol <= 3:
        raise ValueError("minimum_observer_protocol must be between 1 and 3")
    return config


def read_config(root):
    path = Path(root) / CONFIG_NAME
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ValueError(f"No {CONFIG_NAME} in {root}; run studioworkflow init --place lobby=PLACE_ID") from error
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError(f"Invalid JSON in {path}: {error}") from error
    return validate_config(config)
