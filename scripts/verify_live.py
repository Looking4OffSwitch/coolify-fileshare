"""Exercise the deployed service and retain a byte-for-byte verification artifact."""

import hashlib
import http.cookiejar
import io
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CREDS = json.loads((Path.home() / ".config/coolify-fileshare/credentials.json").read_text())
DEPLOYMENT = json.loads((Path.home() / ".config/coolify-fileshare/deployment.json").read_text())
BASE = DEPLOYMENT["public_base_url"]
PROOF = ROOT / "proof"
PROOF.mkdir(exist_ok=True)


def multipart(name, data):
    boundary = "----fileshare" + secrets.token_hex(12)
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def request(opener, path, method="GET", data=None, headers=None):
    req = urllib.request.Request(BASE + path, data=data, headers=headers or {}, method=method)
    try:
        with opener.open(req, timeout=60) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def main():
    fixture = PROOF / "original-code-fixture.zip"
    with zipfile.ZipFile(fixture, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("plugin/main.py", "print('fileshare integrity check')\n")
        archive.writestr("plugin/ui.js", "console.log('fileshare integrity check');\n")
        archive.writestr("plugin/payload.bin", os.urandom(128 * 1024))
    original = fixture.read_bytes()
    sha = hashlib.sha256(original).hexdigest()
    anonymous = urllib.request.build_opener()
    status, _, _ = request(anonymous, "/healthz")
    assert status == 200, f"health {status}"
    body, content_type = multipart("unauthorized.txt", b"blocked")
    anonymous_upload, _, _ = request(anonymous, "/api/uploads", "POST", body, {"Content-Type": content_type})
    assert anonymous_upload == 401, f"anonymous upload {anonymous_upload}"

    jar = http.cookiejar.CookieJar()
    browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    login = urllib.parse.urlencode({"password": CREDS["owner_password"]}).encode()
    login_status, page, _ = request(browser, "/login", "POST", login,
                                     {"Content-Type": "application/x-www-form-urlencoded"})
    assert login_status == 200 and b"Active shares" in page, f"browser login {login_status}"
    csrf = re.search(rb'name=csrf_token value=\'([^\']+)\'', page)
    assert csrf, "CSRF token missing"
    body, content_type = multipart(fixture.name, original)
    upload_status, data, _ = request(browser, "/api/uploads", "POST", body,
                                      {"Content-Type": content_type, "X-CSRF-Token": csrf.group(1).decode()})
    assert upload_status == 201, f"browser upload {upload_status}: {data[:300]!r}"
    share = json.loads(data)
    assert share["sha256"] == sha and share["size"] == len(original)
    download_status, downloaded, headers = request(anonymous, "/s/" + share["id"])
    assert download_status == 200 and downloaded == original
    assert headers["Content-Type"] == "application/octet-stream"
    assert "attachment" in headers["Content-Disposition"]
    downloaded_path = PROOF / "downloaded-code-fixture.zip"
    downloaded_path.write_bytes(downloaded)

    text = b"Fileshare API revocation check\n"
    body, content_type = multipart("revocation.txt", text)
    auth = {"Authorization": "Bearer " + CREDS["upload_token"], "Content-Type": content_type,
            "X-Content-SHA256": hashlib.sha256(text).hexdigest()}
    api_status, api_data, _ = request(anonymous, "/api/uploads", "POST", body, auth)
    assert api_status == 201, f"API upload {api_status}: {api_data[:300]!r}"
    api_share = json.loads(api_data)
    assert api_share["size"] == len(text) and api_share["sha256"] == hashlib.sha256(text).hexdigest()
    assert request(anonymous, "/s/" + api_share["id"])[1] == text
    delete_status, _, _ = request(anonymous, "/api/shares/" + api_share["id"], "DELETE", b"", auth)
    assert delete_status == 204
    revoked_status, _, _ = request(anonymous, "/s/" + api_share["id"])
    assert revoked_status == 404

    result = {"base_url": BASE, "share_url": share["url"], "original_file": str(fixture),
              "downloaded_file": str(downloaded_path), "original_size": len(original),
              "downloaded_size": len(downloaded), "original_sha256": sha,
              "downloaded_sha256": hashlib.sha256(downloaded).hexdigest(),
              "anonymous_upload_status": anonymous_upload, "browser_upload_status": upload_status,
              "api_upload_status": api_status, "revoked_download_status": revoked_status}
    (PROOF / "results.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
