import hashlib
import io
import os
import tempfile
import unittest

from werkzeug.security import generate_password_hash

os.environ.update(
    SESSION_SECRET="test-session-secret-that-is-long-enough",
    OWNER_PASSWORD_HASH=generate_password_hash("owner-password"),
    UPLOAD_TOKEN_SHA256=hashlib.sha256(b"upload-token").hexdigest(),
    PUBLIC_BASE_URL="https://files.example.test",
    DATA_DIR=tempfile.mkdtemp(),
)

from app import create_app  # noqa: E402


class FileshareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({"TESTING": True, "DATA_DIR": self.temp.name, "MAX_UPLOAD_BYTES": 1024})
        self.client = self.app.test_client()
        self.auth = {"Authorization": "Bearer upload-token"}

    def tearDown(self):
        self.temp.cleanup()

    def upload(self, data, headers=None):
        return self.client.post(
            "/api/uploads",
            data={"file": (io.BytesIO(data), "build.zip")},
            headers=self.auth if headers is None else headers,
            content_type="multipart/form-data",
        )

    def test_upload_download_and_revocation(self):
        original = b"PK\x03\x04" + bytes(range(256))
        response = self.upload(original)
        self.assertEqual(response.status_code, 201)
        body = response.json
        self.assertEqual(body["size"], len(original))
        self.assertEqual(body["sha256"], hashlib.sha256(original).hexdigest())
        self.assertTrue(body["url"].startswith("https://files.example.test/s/"))
        downloaded = self.client.get("/s/" + body["id"])
        self.assertEqual(downloaded.data, original)
        self.assertEqual(downloaded.headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", downloaded.headers["Content-Disposition"])
        self.assertEqual(self.client.delete("/api/shares/" + body["id"], headers=self.auth).status_code, 204)
        self.assertEqual(self.client.get("/s/" + body["id"]).status_code, 404)

    def test_auth_checksum_and_size_failures(self):
        self.assertEqual(self.upload(b"x", headers={}).status_code, 401)
        self.assertEqual(self.client.get("/api/uploads").status_code, 405)
        self.assertEqual(self.upload(b"x", headers={**self.auth, "X-Content-SHA256": "0" * 64}).status_code, 422)
        self.assertEqual(self.upload(b"x" * 1025).status_code, 413)
        with self.app.app_context():
            from pathlib import Path
            self.assertFalse(list((Path(self.temp.name) / "files").iterdir()))

    def test_owner_browser_sign_in(self):
        self.assertEqual(self.client.post("/login", data={"password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/login", data={"password": "owner-password"}).status_code, 302)
        page = self.client.get("/")
        self.assertIn(b"Active shares", page.data)


if __name__ == "__main__":
    unittest.main()
