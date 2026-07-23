# -*- coding: utf-8 -*-
"""Single source of truth for reading config values from the environment
or a project-local .env file.

Both data_sources.py (FINMIND_TOKEN) and alerts.py (TELEGRAM_*) used to
carry their own near-identical copy of this loader; they now share
load_env_value() here. No python-dotenv dependency -- a hand-rolled parser
keeps the project's zero-extra-dependency policy (see docs/optimization_plan.md).
"""
import os


def load_env_value(key, base_dir):
    """Return the value of `key`, preferring a real environment variable and
    falling back to a `KEY=value` line in `base_dir/.env`.

    Parsing rules (kept identical to the original loaders so behaviour is
    unchanged): skip blank lines, `#` comments and lines without `=`; split
    on the first `=`; strip surrounding whitespace and one matching pair of
    single or double quotes. Never raises -- a missing or unreadable .env
    yields "".
    """
    env_val = os.environ.get(key)
    if env_val:
        return env_val
    path = os.path.join(base_dir, ".env")
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() != key:
                    continue
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                return v
    except OSError:
        pass
    return ""
