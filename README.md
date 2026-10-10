<div align="center">

# Aeon

**One dashboard for all your Google Drive accounts.**

Connect multiple Google Drive accounts you own, see their quota usage side by side, browse folders and files, upload to the account and folder of your choice, and manage link sharing, all from a single FastAPI app.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![FastAPI](https://img.shields.io/badge/FastAPI-starter-009688)
![Status](https://img.shields.io/badge/status-local%20starter-orange)

</div>

---

> [!IMPORTANT]
> **Aeon does not merge Google Drive accounts or increase their quotas.** Accounts remain separate with Google. Aeon lets you browse, upload to, and configure link access for files in each account independently.

## Table of contents

- [Features](#features)
- [Tech stack](#tech-stack)
- [Project structure](#project-structure)
- [Getting started](#getting-started)
- [Configuration](#configuration)
- [Running the tests](#running-the-tests)
- [Security notes](#security-notes)
- [Before production](#before-production)
- [Limitations](#limitations)
- [Contributing](#contributing)

## Features

- **Connect multiple accounts**: link several Google accounts owned by the same user through Google OAuth.
- **Quota overview**: view storage usage for every connected account.
- **Browse**: navigate folders and files in each account.
- **Upload**: send files to a chosen account and folder.
- **Link access**: configure link sharing for files in each account.
- **Encrypted credentials**: OAuth tokens are encrypted before being saved in the local SQLite database.

## Tech stack

| Layer | Technology |
| --- | --- |
| Web framework | FastAPI, served by Uvicorn |
| Templating | Jinja2 |
| Google integration | `google-api-python-client`, `google-auth-oauthlib` |
| Token encryption | `cryptography` (Fernet) |
| Config | `python-dotenv` |
| Storage | SQLite (local) |
| Testing | pytest |

## Project structure

```text
.
├── app/                # FastAPI application
├── tests/              # Test suite (pytest)
├── .gitignore
├── README.md
└── requirements.txt
```

## Getting started

### Prerequisites

- Python 3.10 or newer
- A Google Cloud project with the Google Drive API enabled
- One or more Google accounts you want to connect

### 1. Clone the repository

```bash
git clone https://github.com/AdityaSharma2457/drive-pool.git
cd drive-pool
```

### 2. Create a virtual environment and install dependencies

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

<details>
<summary>macOS / Linux equivalent</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

</details>

### 3. Create Google OAuth credentials

1. In [Google Cloud Console](https://console.cloud.google.com/), create an OAuth client for a **Web application**.
2. Enable the **Google Drive API** for the project.
3. Add `http://localhost:8000/auth/google/callback` as an authorized redirect URI.
4. Download the client JSON into the project root and name it `credentials.json`.

### 4. Configure environment variables

Copy `.env.example` to `.env`, then generate the token-encryption key in PowerShell:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Put the output in `TOKEN_ENCRYPTION_KEY`.

> [!WARNING]
> Keep this key stable and private. Losing it makes saved account tokens and pending OAuth requests unreadable.

### 5. Start the server

```powershell
uvicorn app.main:app --reload
```

### 6. Connect your accounts

Open <http://localhost:8000> and connect each Google account.

Existing accounts must reconnect and approve the updated access request for full Drive browsing, uploads, and link permissions. Aeon uses the broad `drive` scope because Google does not provide a permissions-only scope for managing sharing on arbitrary existing files. OAuth credentials are encrypted in the local SQLite database.

## Configuration

| Item | Purpose |
| --- | --- |
| `credentials.json` | Google OAuth web client credentials, placed in the project root |
| `.env` | Local environment settings, copied from `.env.example` |
| `TOKEN_ENCRYPTION_KEY` | Fernet key used to encrypt saved OAuth tokens and pending OAuth requests |

Never commit `credentials.json`, `.env`, or the SQLite database. Check that `.gitignore` covers them before pushing.

## Running the tests

```powershell
pytest
```

## Security notes

- Tokens are encrypted at rest with a Fernet key you control.
- The `drive` scope grants broad access to the connected account's files. Only connect accounts you own and trust this app with.
- **Link access can expose files to anyone who receives the link**, and editor access allows those recipients to modify them. Use it deliberately.

## Before production

This starter is intended for **one local operator**. Do not expose it publicly as-is.

- The `drive` scope is restricted and requires Google OAuth verification. Public distribution may also require a security assessment.
- A multi-user service additionally needs:
  - authentication
  - tenant-scoped token storage
  - CSRF protection
  - HTTPS
  - managed secrets
  - rate limits
  - production database and storage operations

## Limitations

- Each Google account keeps its own quota, and Aeon cannot pool or extend them.
- Aeon is a Drive browser and uploader, **not an object store**. It exposes no S3-compatible API, so it is not a drop-in storage backend for VMs or applications.
- Google Drive API rate limits and per-account upload caps still apply.
- If an account's OAuth token expires or is revoked, that account must be reconnected.
- Use of the Drive API should stay within [Google's Terms of Service](https://developers.google.com/terms). Using many accounts specifically to get around free-tier quotas may violate them.

## Contributing

Issues and pull requests are welcome. Before opening a PR:

1. Create a feature branch.
2. Run `pytest` and make sure the suite passes.
3. Keep secrets out of commits.

## License

No license has been specified yet. Add a `LICENSE` file (for example MIT) to clarify how others may use this project.
