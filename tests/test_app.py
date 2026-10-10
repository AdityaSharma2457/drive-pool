from types import SimpleNamespace

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import main, store


def test_dashboard_health_and_static_assets(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")

    with TestClient(main.app) as client:
        response = client.get("/")

        assert response.status_code == 200
        assert "No drives connected" in response.text
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/static/style.css").status_code == 200


def test_oauth_callback_reuses_pkce_verifier(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")
    monkeypatch.setattr(main, "CLIENT_SECRETS_PATH", tmp_path / "client.json")
    main.CLIENT_SECRETS_PATH.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    verifiers = ["test-verifier-one", "test-verifier-two"]
    states = ["test-state-one", "test-state-two"]
    flow_calls = []
    flow_index = 0

    class FakeFlow:
        credentials = SimpleNamespace(
            scopes=main.SCOPES,
            to_json=lambda: '{"access_token":"test"}',
        )

        def __init__(self, index):
            self.code_verifier = verifiers[index]
            self.state = states[index]

        def authorization_url(self, **_kwargs):
            return "https://accounts.google.com/authorize", self.state

        def fetch_token(self, **_kwargs):
            assert main.os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"

    def create_flow(*_args, **kwargs):
        nonlocal flow_index
        flow_calls.append(kwargs)
        if "code_verifier" in kwargs:
            return FakeFlow(verifiers.index(kwargs["code_verifier"]))
        flow = FakeFlow(flow_index)
        flow_index += 1
        return flow

    class FakeAbout:
        def get(self, **_kwargs):
            return self

        def execute(self):
            return {"user": {"emailAddress": "test@example.com"}}

    service = SimpleNamespace(about=lambda: FakeAbout())
    monkeypatch.setattr(main.Flow, "from_client_secrets_file", create_flow)
    monkeypatch.setattr(main, "build", lambda *_args, **_kwargs: service)

    with TestClient(main.app) as client:
        first_login = client.get("/auth/google", follow_redirects=False)
        second_login = client.get("/auth/google", follow_redirects=False)
        client.cookies.clear()
        first_callback = client.get(
            "/auth/google/callback?state=test-state-one&code=test-code-one",
            follow_redirects=False,
        )
        second_callback = client.get(
            "/auth/google/callback?state=test-state-two&code=test-code-two",
            follow_redirects=False,
        )

    assert first_login.status_code == second_login.status_code == 303
    assert first_callback.status_code == second_callback.status_code == 303
    assert "Google+Drive+account+connected" in first_callback.headers["location"]
    assert "Google+Drive+account+connected" in second_callback.headers["location"]
    assert flow_calls[2]["code_verifier"] == verifiers[0]
    assert flow_calls[3]["code_verifier"] == verifiers[1]
    assert store.consume_oauth_state(main.DATABASE_PATH, "test-state-one") is None


def test_oauth_scope_relaxation_is_restored_after_token_exchange(monkeypatch):
    class FakeFlow:
        def fetch_token(self, **_kwargs):
            assert main.os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] == "1"

    monkeypatch.delenv("OAUTHLIB_RELAX_TOKEN_SCOPE", raising=False)

    main._fetch_google_token(FakeFlow(), "http://localhost/callback?code=fake")

    assert "OAUTHLIB_RELAX_TOKEN_SCOPE" not in main.os.environ


def test_token_exchange_error_is_reported_without_details():
    error = SimpleNamespace(error="invalid_client")

    assert main._token_exchange_notice(error) == (
        "Google rejected the OAuth client (invalid_client); "
        "refresh credentials.json from Google Cloud"
    )


def test_loopback_oauth_callback_allows_local_http_transport():
    assert main.REDIRECT_URI.startswith("http://localhost:")
    assert main.os.environ["OAUTHLIB_INSECURE_TRANSPORT"] == "1"


def test_drive_browser_lists_folder_contents(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")
    monkeypatch.setattr(main, "list_accounts", lambda _path: [{"email": "user@example.com"}])
    monkeypatch.setattr(main, "_account_has_scope", lambda _email, _scope: True)

    class FilesApi:
        def get(self, **_kwargs):
            return self

        def list(self, **kwargs):
            self.list_kwargs = kwargs
            return self

        def execute(self):
            if hasattr(self, "list_kwargs"):
                return {
                    "files": [
                        {
                            "id": "doc_1",
                            "name": "notes.txt",
                            "mimeType": "text/plain",
                            "size": "512",
                            "modifiedTime": "2026-10-10T10:00:00Z",
                        }
                    ],
                    "nextPageToken": "token+with/slash=",
                }
            return {
                "id": "folder_1",
                "name": "Research",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": ["root"],
            }

    files_api = FilesApi()
    monkeypatch.setattr(main, "_drive_service", lambda _email: SimpleNamespace(files=lambda: files_api))

    with TestClient(main.app) as client:
        response = client.get(
            "/accounts/user%40example.com/files?folder_id=folder_1"
        )

    assert response.status_code == 200
    assert "Research" in response.text
    assert "notes.txt" in response.text
    assert "Next page" in response.text
    assert "page_token=token%2Bwith%2Fslash%3D" in response.text
    assert "Share" in response.text
    assert "Viewer" in response.text
    assert "Editor" in response.text
    assert files_api.list_kwargs["q"] == "'folder_1' in parents and trashed = false"


def test_upload_targets_selected_drive_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")
    monkeypatch.setattr(main, "list_accounts", lambda _path: [{"email": "user@example.com"}])
    created = {}

    class FilesApi:
        def create(self, **kwargs):
            created.update(kwargs)
            return self

        def execute(self):
            return {"id": "new_file", "name": "hello.txt"}

    files_api = FilesApi()
    monkeypatch.setattr(main, "_drive_service", lambda _email: SimpleNamespace(files=lambda: files_api))

    with TestClient(main.app) as client:
        response = client.post(
            "/accounts/user%40example.com/upload",
            data={"folder_id": "folder_1"},
            files={"file": ("hello.txt", b"hello drive", "text/plain")},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert created["body"] == {"name": "hello.txt", "parents": ["folder_1"]}
    assert "folder_id=folder_1" in response.headers["location"]


def test_file_link_access_can_be_created_updated_and_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")
    monkeypatch.setattr(main, "list_accounts", lambda _path: [{"email": "user@example.com"}])
    monkeypatch.setattr(main, "_account_has_scope", lambda _email, _scope: True)
    operations = []

    class FilesApi:
        def get(self, **_kwargs):
            class FileRequest:
                def execute(self):
                    return {
                        "id": "file_1",
                        "webViewLink": "https://drive.google.com/file/d/file_1/view",
                        "permissions": [
                            {"id": "anyone_1", "type": "anyone", "role": "reader"}
                        ],
                    }

            return FileRequest()

    class PermissionsApi:
        def create(self, **kwargs):
            operations.append(("create", kwargs))
            return self

        def update(self, **kwargs):
            operations.append(("update", kwargs))
            return self

        def delete(self, **kwargs):
            operations.append(("delete", kwargs))
            return self

        def execute(self):
            return {"id": "anyone_1"}

    files_api = FilesApi()
    permissions_api = PermissionsApi()
    service = SimpleNamespace(
        files=lambda: files_api,
        permissions=lambda: permissions_api,
    )
    monkeypatch.setattr(main, "_drive_service", lambda _email: service)

    with TestClient(main.app) as client:
        update_response = client.post(
            "/accounts/user%40example.com/files/file_1/share",
            data={"role": "writer", "folder_id": "folder_1"},
            follow_redirects=False,
        )
        revoke_response = client.post(
            "/accounts/user%40example.com/files/file_1/share",
            data={"role": "none", "folder_id": "folder_1"},
            follow_redirects=False,
        )

    assert update_response.status_code == revoke_response.status_code == 303
    assert operations[0][0] == "update"
    assert operations[0][1]["body"] == {"role": "writer"}
    assert "shared_link=https%3A%2F%2Fdrive.google.com%2Ffile%2Fd%2Ffile_1%2Fview" in update_response.headers["location"]
    assert operations[1][0] == "delete"
    assert operations[1][1]["permissionId"] == "anyone_1"


def test_file_link_access_creates_anyone_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")
    monkeypatch.setattr(main, "list_accounts", lambda _path: [{"email": "user@example.com"}])
    monkeypatch.setattr(main, "_account_has_scope", lambda _email, _scope: True)
    created = {}

    class FilesApi:
        def get(self, **_kwargs):
            class FileRequest:
                def execute(self):
                    return {
                        "id": "file_1",
                        "webViewLink": "https://drive.google.com/file/d/file_1/view",
                        "permissions": [],
                    }

            return FileRequest()

    class PermissionsApi:
        def create(self, **kwargs):
            created.update(kwargs)
            return self

        def execute(self):
            return {"id": "anyone_1", "role": "reader"}

    service = SimpleNamespace(
        files=lambda: FilesApi(),
        permissions=lambda: PermissionsApi(),
    )
    monkeypatch.setattr(main, "_drive_service", lambda _email: service)

    with TestClient(main.app) as client:
        response = client.post(
            "/accounts/user%40example.com/files/file_1/share",
            data={"role": "reader"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert created["body"] == {
        "type": "anyone",
        "role": "reader",
        "allowFileDiscovery": False,
    }
    assert "shared_link=" in response.headers["location"]


def test_file_sharing_requires_full_drive_scope(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DATABASE_PATH", tmp_path / "drivepool.sqlite3")
    monkeypatch.setattr(main, "list_accounts", lambda _path: [{"email": "user@example.com"}])
    monkeypatch.setattr(main, "_account_has_scope", lambda _email, _scope: False)
    monkeypatch.setattr(
        main,
        "_drive_service",
        lambda _email: (_ for _ in ()).throw(AssertionError("Google API must not be called")),
    )

    with TestClient(main.app) as client:
        response = client.post(
            "/accounts/user%40example.com/files/file_1/share",
            data={"role": "reader"},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Reconnect+this+Google+account+and+approve+full+Drive+access" in response.headers["location"]