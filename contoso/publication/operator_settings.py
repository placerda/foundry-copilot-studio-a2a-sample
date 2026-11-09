"""Trusted operator settings; never populated from publication request bodies."""

import json
from pathlib import Path
from uuid import UUID


def publication_settings(bindings, env, overrides=None):
    settings = dict(env)
    if overrides is None:
        return settings
    if (not isinstance(overrides, dict)
            or set(overrides) != {"project_id", "workspace_root", "apim_caller_id"}
            or not all(isinstance(value, str) for value in overrides.values())
            or overrides["project_id"].casefold() != bindings["project_id"].casefold()):
        raise ValueError("Publication overrides must match the approved project")
    values = {
        "CONTOSO_PUBLICATION_WORKSPACE_ROOT": overrides["workspace_root"],
        "CONTOSO_PUBLICATION_APIM_CALLER_ID": str(UUID(overrides["apim_caller_id"])),
    }
    for key, value in values.items():
        if key in settings and settings[key] != value:
            raise ValueError("Conflicting operator publication settings")
        settings[key] = value
    publication_workspace(settings)
    return settings


def publication_workspace(env):
    configured = env.get("CONTOSO_PUBLICATION_WORKSPACE_ROOT")
    root = Path(configured) if configured else (
        Path(env["HOME"]) / "data" / "contoso-publication"
        if env.get("HOME") else Path.cwd() / ".contoso-publication"
    )
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("An absolute owned publication workspace is required")
    return root


def publication_environment(bindings, env):
    values = dict(bindings["environment_values"])
    additional = env.get("CONTOSO_PUBLICATION_APIM_CALLER_ID")
    if additional is None:
        return tuple(values.items())
    additional = str(UUID(additional))
    key = "CONTOSO_ROUTER_PLATFORM_CALLER_IDS"
    callers = json.loads(values[key])
    if (not isinstance(callers, list) or not callers
            or any(not isinstance(value, str) or not value.strip() for value in callers)
            or len(set(callers)) != len(callers)):
        raise ValueError("Existing Router caller admission must be preserved")
    if additional not in callers:
        callers.append(additional)
    if len(callers) > 16:
        raise ValueError("Router caller admission exceeds its approved bound")
    values[key] = json.dumps(callers, separators=(",", ":"))
    return tuple(values.items())
