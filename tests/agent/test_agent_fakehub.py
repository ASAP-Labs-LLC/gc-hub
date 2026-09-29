"""The fake hub itself: auth and the default ingest behaviour (sanity for the suite)."""
import hashlib
import json
import urllib.error
import urllib.request


def _post(url, body, headers):
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_fakehub_auth_and_ingest(hub):
    body = b"CDF\x01"
    sha = hashlib.sha256(body).hexdigest()
    h = {"X-GC-SHA256": sha}
    assert _post(hub.url + "/api/ingest", body, h)[0] == 401
    h["Authorization"] = "Bearer " + hub.token
    s, j = _post(hub.url + "/api/ingest", body, h)
    assert s == 201 and j["sha256"] == sha
    s, j = _post(hub.url + "/api/ingest", body, h)
    assert s == 200 and j["duplicate"] is True
