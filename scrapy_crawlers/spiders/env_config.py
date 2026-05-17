import os
from pathlib import Path
from urllib.parse import quote


def _settings_env_path():
    return Path(__file__).resolve().parents[1] / "settings" / ".env"


def _parse_env_file(path):
    values = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        values[key] = value
    return values


_FILE_ENV = _parse_env_file(_settings_env_path())


def get_env(*keys, default=None):
    for key in keys:
        value = os.getenv(key)
        if value:
            return value
    for key in keys:
        value = _FILE_ENV.get(key)
        if value:
            return value
    return default


def _split_proxy_params(raw_params):
    value = str(raw_params or "").strip()
    if not value:
        return []
    return [part.strip() for part in value.split(";") if part and part.strip()]


def build_proxy_url(extra_params=None, port_override=None):
    username = get_env("PROXY_USERNAME", "LOGIN")
    password = get_env("PROXY_PASSWORD", "PASSWORD")
    host = get_env("PROXY_HOST")
    port = get_env("PROXY_PORT", default="823")
    params = get_env("PROXY_PARAMS", "DATAIMPULSE_PROXY_PARAMS")

    if not all([username, password, host, port]):
        return None

    merged_params = _split_proxy_params(params)
    for candidate in (extra_params or []):
        for part in _split_proxy_params(candidate):
            if part not in merged_params:
                merged_params.append(part)

    proxy_port = str(port_override or port).strip() or port
    merged_param_text = ";".join(merged_params)
    auth_username = f"{username}__{merged_param_text}" if merged_param_text else username
    return (
        f"http://{quote(auth_username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{proxy_port}"
    )
