import json
import os
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from dotenv import dotenv_values

CONFIG_FILE_NAME = "config.json"
DOTENV_FILE_NAME = ".env"
BASE_DIR = Path(__file__).resolve().parent.parent

def _load_env():
    env_path = BASE_DIR / DOTENV_FILE_NAME
    secrets = dotenv_values(env_path)
    for key in list(secrets):
        if key in os.environ:
            secrets[key] = os.environ[key]
    return SimpleNamespace(**secrets)

def _load_config():
    config_path = BASE_DIR / CONFIG_FILE_NAME
    with config_path.open("r", encoding="utf-8") as f:
        return json.load(f, object_hook=lambda d: SimpleNamespace(**d))

@lru_cache(maxsize=1)
def get_config():
    return _load_config()

@lru_cache(maxsize=1)
def get_env():
    return _load_env()


_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off", ""}

def env_get(name: str, default: str | None = None) -> str | None:
    """Read a setting from the OS environment first, then .env.

    get_env() only surfaces keys that already exist in .env, so a workflow-level
    `env:` value (e.g. USE_PROXY) would otherwise be invisible. An empty value
    counts as unset — the workflows echo `KEY=` when a secret isn't configured.
    """
    value = os.environ.get(name)
    if value is None:
        value = getattr(get_env(), name, None)
    return default if value in (None, "") else value

def env_bool(name: str, default: bool | None = None) -> bool | None:
    """Parse an env flag. Returns `default` when unset or unrecognised."""
    value = env_get(name)
    if value is None:
        return default
    value = value.strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default

def resolve_proxy(stage: str, source: str) -> str | None:
    """Return the proxy URL to fetch `source` through in `stage`, or None for a
    direct connection.

    Enabled by the `USE_PROXY` env var when set (that's the workflow-level
    switch), otherwise by `config.<stage>.<source>.use_proxy`. Default is off.
    The URL comes from `PROXY_URL__<SOURCE>`, falling back to a shared
    `PROXY_URL`. Enabled-but-no-URL logs a warning and goes direct rather than
    killing the run.
    """
    section = getattr(getattr(get_config(), stage, None), source, None)
    enabled = env_bool("USE_PROXY", getattr(section, "use_proxy", False))
    if not enabled:
        return None

    proxy_url = proxy_url_for(source)
    if not proxy_url:
        # Deliberately not fatal: a missing proxy secret shouldn't stop a run
        # that would otherwise work directly.
        from lib.logger import get_logger  # local import: logger imports config

        get_logger("config").warning(
            f"Proxy enabled for {stage}.{source} but neither "
            f"PROXY_URL__{source.upper()} nor PROXY_URL is set — going direct."
        )
        return None

    # Returned with any sticky port range intact — Fetcher expands it per fetch,
    # so each page gets its own session rather than the whole run sharing one IP.
    return proxy_url


def proxy_url_for(source: str) -> str | None:
    """The proxy URL for `source` whether or not the proxy is switched on:
    `PROXY_URL__<SOURCE>`, then the shared `PROXY_URL`. resolve_proxy asks this
    once the switch says yes; the sweep's fallback asks it after a block."""
    return env_get(f"PROXY_URL__{source.upper()}") or env_get("PROXY_URL")


# Example usage if the file is called directly
if __name__ == "__main__":
    config = get_config()
    print(config)

    env = get_env()
    print(env)