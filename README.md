# Aeon

A FastAPI starter app that connects Google Drive accounts owned by the same user, shows quota usage, browses folders and files, and uploads files to a selected account and folder.

**This does not merge Google Drive accounts or increase their quotas.** Accounts remain separate with Google; Aeon lets you browse, upload to, and configure link access for files in each account independently.

## Run locally

1. Create and activate a virtual environment, then install dependencies:

   ```powershell
   py -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. In Google Cloud Console, create an OAuth client for a **Web application**, enable the Google Drive API, and add `http://localhost:8000/auth/google/callback` as an authorized redirect URI. Download the client JSON to this project as `credentials.json`.
3. Copy `.env.example` to `.env`. Generate the token-encryption key in PowerShell:

   ```powershell
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

   Put the output in `TOKEN_ENCRYPTION_KEY`. Keep it stable and private; losing it makes saved account tokens and pending OAuth requests unreadable.
4. Start the server:

   ```powershell
   uvicorn app.main:app --reload
   ```

5. Open <http://localhost:8000> and connect each Google account. Existing accounts must reconnect and approve the updated access request for full Drive browsing, uploads, and link permissions. Aeon uses the broad `drive` scope because Google does not provide a permissions-only scope for managing sharing on arbitrary existing files. OAuth credentials are encrypted in the local SQLite database.

## Verify

```powershell
pytest
```

## Before production

This starter is intended for one local operator. The `drive` scope is restricted and requires Google OAuth verification; public distribution may also require a security assessment. Link access can expose files to anyone who receives the link, and editor access allows those recipients to modify them. A multi-user service also needs authentication, tenant-scoped token storage, CSRF protection, HTTPS, managed secrets, rate limits, and production database/storage operations. Do not expose it publicly as-is.