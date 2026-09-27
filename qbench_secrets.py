"""QBench credentials, resolved from a local store instead of the source tree.

Resolution order for each value:

1. ``QBENCH_CLIENT_ID`` / ``QBENCH_CLIENT_SECRET`` in the environment
   (default profile only -- a named profile always comes from the store, since
   one process may need two different QBench clients at once).
2. The local JSON store, whose location is ``QBENCH_STORE_PATH`` or
   :func:`default_store_path`.
3. Nothing -- raise :class:`QBenchSecretMissing`, never return ``None``.

Store shape::

    {
      "client_id": "...",           # the default pair
      "client_secret": "...",
      "profiles": {                 # optional, for apps on a different client
        "tools": {"client_id": "...", "client_secret": "..."}
      }
    }

The store is deliberately outside every repository. The only writer is
:func:`save_default`, used by the app's Settings screen after QBench has
accepted the pair.
"""
import json
import os
import tempfile
import time

__all__ = [
    "QBenchSecretMissing",
    "default_store_path",
    "describe",
    "get_client_id",
    "get_client_secret",
    "save_default",
]


class QBenchSecretMissing(RuntimeError):
    """Raised when a credential is in neither the environment nor the store."""


def default_store_path():
    """Where the credential store lives when nothing overrides it."""
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ASAPLabs", "qbench.json")
    home = os.environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".config", "asaplabs", "qbench.json")


def _store_path():
    return os.environ.get("QBENCH_STORE_PATH") or default_store_path()


def _load_store():
    path = _store_path()
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _resolve(key, env_var, profile=None):
    store = _load_store()
    if profile is None:
        value = os.environ.get(env_var) or store.get(key)
    else:
        profiles = store.get("profiles") or {}
        if profile not in profiles:
            raise QBenchSecretMissing(
                f"QBench profile {profile!r} is not defined in the local store "
                f"at {_store_path()}. Add it under \"profiles\" rather than "
                f"falling back to the default client."
            )
        value = profiles[profile].get(key)
    if not value:
        where = f"profile {profile!r}" if profile else "the default pair"
        raise QBenchSecretMissing(
            f"QBench {key} is not configured for {where}. Set the {env_var} "
            f"environment variable, or add {key!r} to the local store at "
            f"{_store_path()}."
        )
    return value


def get_client_secret(profile=None):
    """The QBench OAuth client secret. Never returns ``None``."""
    return _resolve("client_secret", "QBENCH_CLIENT_SECRET", profile)


def get_client_id(profile=None):
    """The QBench OAuth client id. Never returns ``None``."""
    return _resolve("client_id", "QBENCH_CLIENT_ID", profile)


def save_default(client_id, client_secret):
    """Write the default pair into the local store, keeping every other key
    and profile. Atomic (temp file + ``os.replace``), ``0600`` on POSIX, parent
    directory created. A store that is not valid JSON is kept aside as
    ``<store>.corrupt-<time>`` and replaced. Never logs or returns the secret.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise ValueError("Client ID and Client Secret are both required")
    path = _store_path()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    try:
        data = _load_store()
        if not isinstance(data, dict):
            raise ValueError("store is not a JSON object")
    except ValueError:  # includes json.JSONDecodeError
        os.replace(path, f"{path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        data = {}
    data["client_id"] = client_id
    data["client_secret"] = client_secret

    fd, tmp = tempfile.mkstemp(prefix=".qbench-", suffix=".tmp", dir=directory)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name != "nt":
        os.chmod(path, 0o600)


def describe():
    """Safe status for the UI: whether a default pair is configured, the last
    four characters of the client id, where it comes from, and the store path
    (on Windows that is the %APPDATA% of whichever account runs the app).
    Never includes the secret."""
    try:
        data = _load_store()
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    env_id = os.environ.get("QBENCH_CLIENT_ID") or ""
    env_secret = os.environ.get("QBENCH_CLIENT_SECRET") or ""
    cid = env_id or data.get("client_id") or ""
    secret = env_secret or data.get("client_secret") or ""
    configured = bool(cid and secret)
    if not configured:
        source = ""
    elif env_id or env_secret:
        source = "environment"
    else:
        source = "store"
    return {
        "configured": configured,
        "client_id_hint": ("\u2026" + cid[-4:]) if cid else "",
        "source": source,
        "store_path": _store_path(),
    }
