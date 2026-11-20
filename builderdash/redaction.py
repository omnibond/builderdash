"""Keep secrets out of builderdash logs.

Build options, SSH connection parameters and proxy configs are logged for
debugging. Any key that looks like a secret (password, passphrase, token,
private key, ...) is logged as <redacted>; empty values stay visible so a
missing setting is still obvious.
"""

REDACTED = "<redacted>"

_SECRET_MARKERS = ("passw", "passphrase", "secret", "token", "private_key", "api_key", "credential")


def is_secret_key(key):
    text = str(key).lower()
    return any(marker in text for marker in _SECRET_MARKERS)


def redact_value(key, value):
    if value in (None, "") or not is_secret_key(key):
        return value
    return REDACTED


def redact_mapping(mapping):
    """Shallow copy of a mapping with secret-looking values redacted (nested mappings too)."""
    if not isinstance(mapping, dict):
        return mapping
    out = {}
    for key, value in mapping.items():
        if isinstance(value, dict):
            out[key] = redact_mapping(value)
        else:
            out[key] = redact_value(key, value)
    return out
