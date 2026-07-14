import os
import sqlite3
import tempfile
import unittest
from importlib.util import find_spec


class FakeRequest:
    def __init__(self):
        self.session = {}


@unittest.skipUnless(find_spec("fastapi"), "FastAPI dependencies are not installed in this Python environment")
class EmailAuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_db_path = os.environ.get("DB_PATH")
        os.environ["DB_PATH"] = os.path.join(self.tempdir.name, "test.db")

        from core import init_db
        from services.auth_service import AccountAuthService, AuthSettings

        init_db()
        self.service = AccountAuthService(AuthSettings(
            required=True,
            client_id="",
            client_secret="",
            redirect_uri="http://127.0.0.1:8765/auth/callback",
            session_secret="test-secret",
            cookie_secure=False,
            allowed_user_ids=set(),
            allowed_usernames=set(),
        ))

    def tearDown(self):
        if self.previous_db_path is None:
            os.environ.pop("DB_PATH", None)
        else:
            os.environ["DB_PATH"] = self.previous_db_path
        self.tempdir.cleanup()

    def test_register_then_login_uses_email_account_identity(self):
        request = FakeRequest()
        response = self.service.register_email(
            request, "staff@example.com", "password-123", "スタッフ"
        )

        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(request.session["auth_user"]["accountId"], "email:1")
        self.assertEqual(request.session["auth_user"]["provider"], "email")

        login_request = FakeRequest()
        response = self.service.login_email(
            login_request, "STAFF@example.com", "password-123"
        )
        self.assertEqual(response.headers["location"], "/")
        self.assertEqual(login_request.session["auth_user"]["accountId"], "email:1")

    def test_duplicate_or_invalid_password_returns_to_login(self):
        self.service.register_email(
            FakeRequest(), "staff@example.com", "password-123", "スタッフ"
        )

        duplicate = self.service.register_email(
            FakeRequest(), "staff@example.com", "password-456", "別ユーザー"
        )
        self.assertIn("error=", duplicate.headers["location"])

        invalid = self.service.login_email(
            FakeRequest(), "staff@example.com", "wrong-password"
        )
        self.assertIn("error=", invalid.headers["location"])

        conn = sqlite3.connect(os.environ["DB_PATH"])
        row = conn.execute(
            "SELECT password_hash, password_salt FROM email_auth_users"
        ).fetchone()
        conn.close()
        self.assertNotEqual(row[0], "password-123")
        self.assertTrue(row[1])


if __name__ == "__main__":
    unittest.main()
