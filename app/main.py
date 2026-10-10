import os
import logging
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, quote_plus, urlencode, urlparse

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from app.storage import DriveUsage, summarize_storage
from app.store import (
    delete_account,
    consume_oauth_state,
    get_credentials,
    initialize_database,
    list_accounts,
    save_oauth_state,
    save_account,
)

ROOT = Path(__file__).resolve().parent.parent
logger = logging.getLogger(__name__)
load_dotenv(ROOT / ".env")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(ROOT / "data" / "drivepool.sqlite3")))
CLIENT_SECRETS_PATH = Path(
    os.getenv("GOOGLE_CLIENT_SECRETS_FILE", str(ROOT / "credentials.json"))
)
REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/google/callback"
)
redirect_parts = urlparse(REDIRECT_URI)
if redirect_parts.scheme == "http" and redirect_parts.hostname in {
    "localhost",
    "127.0.0.1",
    "::1",
}:
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
SCOPES = [
    "https://www.googleapis.com/auth/drive",
]
SHARING_SCOPE = SCOPES[0]


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database(DATABASE_PATH)
    yield


app = FastAPI(title="Aeon", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=ROOT / "app" / "templates")


def _read_drive_usage(account: dict[str, str]) -> DriveUsage:
    credentials_info = get_credentials(DATABASE_PATH, account["email"])
    credentials = Credentials.from_authorized_user_info(credentials_info, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleRequest())
        save_account(DATABASE_PATH, account["email"], credentials.to_json())

    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    about = service.about().get(
        fields="user(displayName,emailAddress),storageQuota(limit,usage)"
    ).execute()
    quota = about.get("storageQuota", {})
    user = about.get("user", {})
    return DriveUsage(
        email=user.get("emailAddress", account["email"]),
        display_name=user.get("displayName"),
        used_bytes=int(quota["usage"]) if quota.get("usage") is not None else None,
        limit_bytes=int(quota["limit"]) if quota.get("limit") is not None else None,
    )


def _drive_service(email: str):
    credentials_info = get_credentials(DATABASE_PATH, email)
    credentials = Credentials.from_authorized_user_info(credentials_info, SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleRequest())
        save_account(DATABASE_PATH, email, credentials.to_json())
    return build("drive", "v3", credentials=credentials, cache_discovery=False)


def _account_has_scope(email: str, required_scope: str) -> bool:
    credentials_info = get_credentials(DATABASE_PATH, email)
    scopes = credentials_info.get("scopes", [])
    if isinstance(scopes, str):
        scopes = scopes.split()
    return required_scope in scopes


def _sharing_error_notice(error: HttpError) -> str:
    try:
        payload = json.loads(error.content.decode("utf-8"))
        details = payload.get("error", {}).get("errors", [])
        reason = details[0].get("reason") if details else None
    except (AttributeError, IndexError, TypeError, ValueError):
        reason = None

    messages = {
        "insufficientFilePermissions": "This account cannot change sharing for this file. Ask its owner for sharing access.",
        "domainPolicy": "The file owner's organization blocks link sharing outside its domain.",
        "cannotModifyInheritedTeamDrivePermission": "This file inherits sharing from its shared drive; change access on the shared-drive folder instead.",
        "sharingRateLimitExceeded": "Google temporarily limited sharing changes. Try again later.",
    }
    if reason in messages:
        return messages[reason]
    if isinstance(reason, str) and re.fullmatch(r"[A-Za-z0-9_-]+", reason):
        return f"Google denied this sharing change ({reason})."
    return f"Google denied this sharing change (HTTP {error.resp.status})."


def _validate_folder_id(folder_id: str | None) -> None:
    if folder_id and not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
        raise HTTPException(status_code=400, detail="Invalid Drive folder ID")


def _drive_path(email: str, folder_id: str | None = None) -> str:
    url = f"/accounts/{quote(email, safe='@.')}/files"
    if folder_id:
        url += "?" + urlencode({"folder_id": folder_id})
    return url


def _drive_redirect(
    email: str,
    folder_id: str | None,
    notice: str,
    shared_link: str | None = None,
) -> RedirectResponse:
    url = _drive_path(email, folder_id)
    params = {"notice": notice}
    if shared_link:
        params["shared_link"] = shared_link
    separator = "&" if "?" in url else "?"
    return RedirectResponse(f"{url}{separator}{urlencode(params)}", 303)


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "Not reported"
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    amount = float(value)
    for unit in units:
        if amount < 1000 or unit == units[-1]:
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1000
    return "Not reported"


def _token_exchange_notice(error: Exception) -> str:
    code = getattr(error, "error", None)
    if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum():
        if code == "invalid_client":
            return "Google rejected the OAuth client (invalid_client); refresh credentials.json from Google Cloud"
        if code == "invalid_grant":
            return "Google rejected the authorization code (invalid_grant); start a fresh connection"
        return f"Google OAuth token exchange failed ({code})"
    return f"OAuth token exchange failed ({type(error).__name__}); restart the connection"


def _fetch_google_token(flow, authorization_response: str) -> None:
    previous_setting = os.environ.get("OAUTHLIB_RELAX_TOKEN_SCOPE")
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    try:
        flow.fetch_token(authorization_response=authorization_response)
    finally:
        if previous_setting is None:
            os.environ.pop("OAUTHLIB_RELAX_TOKEN_SCOPE", None)
        else:
            os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = previous_setting


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    accounts = list_accounts(DATABASE_PATH)
    drives = []
    for account in accounts:
        try:
            drives.append(_read_drive_usage(account))
        except Exception as error:
            detail = "Reconnect this account to refresh its quota."
            if isinstance(error, HttpError) and error.resp.status == 403:
                detail = "Drive metadata access was denied. Reconnect and grant access."
            drives.append(DriveUsage(email=account["email"], error=detail))

    summary = summarize_storage(drives)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "accounts": drives,
            "summary": summary,
            "format_bytes": _format_bytes,
            "client_configured": CLIENT_SECRETS_PATH.is_file(),
            "notice": request.query_params.get("notice"),
        },
    )


@app.get("/auth/google")
def google_login(request: Request, login_hint: str | None = None):
    if not CLIENT_SECRETS_PATH.is_file():
        return RedirectResponse("/?notice=Add+Google+OAuth+credentials+to+connect+an+account", 303)
    if not os.getenv("TOKEN_ENCRYPTION_KEY"):
        return RedirectResponse("/?notice=Set+TOKEN_ENCRYPTION_KEY+before+connecting+accounts", 303)

    flow = Flow.from_client_secrets_file(
        str(CLIENT_SECRETS_PATH), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent select_account",
        login_hint=login_hint,
    )
    save_oauth_state(DATABASE_PATH, state, flow.code_verifier)
    return RedirectResponse(authorization_url, 303)


@app.get("/auth/google/callback")
def google_callback(request: Request):
    state = request.query_params.get("state")
    if not state:
        return RedirectResponse("/?notice=Google+sign-in+state+did+not+match", 303)
    if request.query_params.get("error"):
        return RedirectResponse("/?notice=Google+account+connection+was+cancelled", 303)
    code_verifier = consume_oauth_state(DATABASE_PATH, state)
    if not code_verifier:
        return RedirectResponse("/?notice=OAuth+state+expired+or+already+used;+start+again", 303)

    stage = "token exchange"
    try:
        flow = Flow.from_client_secrets_file(
            str(CLIENT_SECRETS_PATH),
            scopes=SCOPES,
            state=state,
            redirect_uri=REDIRECT_URI,
            code_verifier=code_verifier,
        )
        _fetch_google_token(flow, str(request.url))
        credentials = flow.credentials
        stage = "Google Drive permission check"
        granted_scopes = set(credentials.scopes or [])
        if not set(SCOPES).issubset(granted_scopes):
            raise ValueError("Google did not grant all requested Drive scopes")
        stage = "Google Drive account lookup"
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)
        user = service.about().get(fields="user(emailAddress)").execute().get("user", {})
        email = user.get("emailAddress")
        if not email:
            raise ValueError("Google did not return an account email")
        stage = "encrypted token storage"
        save_account(DATABASE_PATH, email, credentials.to_json())
    except Exception as error:
        oauth_error_code = getattr(error, "error", None)
        logger.error(
            "Google OAuth callback failed during %s (%s, code=%s)",
            stage,
            type(error).__name__,
            oauth_error_code if isinstance(oauth_error_code, str) else "unavailable",
        )
        if stage == "token exchange":
            notice = _token_exchange_notice(error)
        elif stage == "Google Drive account lookup" and isinstance(error, HttpError):
            if error.resp.status == 403:
                notice = "Google Drive API returned 403; enable the API and check OAuth access"
            elif error.resp.status == 401:
                notice = "Google rejected Drive access; reconnect and grant the requested access"
            else:
                notice = f"Google Drive account lookup failed (HTTP {error.resp.status})"
        elif stage == "Google Drive account lookup":
            notice = "Could not read the Google account profile; verify Drive API setup"
        elif stage == "Google Drive permission check":
            notice = "Google did not grant browse and upload access; reconnect and approve all requested permissions"
        else:
            notice = "Could not securely save the account; check TOKEN_ENCRYPTION_KEY"
        return RedirectResponse(f"/?notice={quote_plus(notice)}", 303)
    return RedirectResponse("/?notice=Google+Drive+account+connected", 303)


@app.get("/accounts/{email}/files", response_class=HTMLResponse)
def browse_drive(
    request: Request,
    email: str,
    folder_id: str | None = None,
    page_token: str | None = None,
):
    if email not in {account["email"] for account in list_accounts(DATABASE_PATH)}:
        return RedirectResponse("/?notice=Connect+this+Google+account+first", 303)
    _validate_folder_id(folder_id)
    try:
        service = _drive_service(email)
        current_folder = None
        parent_folder_id = None
        if folder_id:
            current_folder = service.files().get(
                fileId=folder_id, fields="id,name,mimeType,parents"
            ).execute()
            if current_folder.get("mimeType") != "application/vnd.google-apps.folder":
                raise HTTPException(status_code=400, detail="Selected item is not a folder")
            parents = current_folder.get("parents", [])
            parent_folder_id = parents[0] if parents and parents[0] != "root" else None

        query = f"'{folder_id or 'root'}' in parents and trashed = false"
        result = service.files().list(
            q=query,
            pageSize=100,
            pageToken=page_token,
            orderBy="folder,name",
            fields="nextPageToken,files(id,name,mimeType,size,modifiedTime,webViewLink,permissions(id,type,role))",
        ).execute()
    except HTTPException:
        raise
    except HttpError as error:
        if error.resp.status == 403:
            detail = "Google denied Drive listing. Reconnect this account and grant the new Drive access."
        elif error.resp.status == 404:
            detail = "That folder is unavailable or was deleted."
        else:
            detail = f"Could not load Drive contents (HTTP {error.resp.status})."
        return templates.TemplateResponse(
            request=request,
            name="drive.html",
            context={"email": email, "error": detail, "files": []},
            status_code=403 if error.resp.status == 403 else 502,
        )
    except Exception:
        return templates.TemplateResponse(
            request=request,
            name="drive.html",
            context={
                "email": email,
                "error": "Could not load this Drive. Reconnect the account and try again.",
                "files": [],
            },
            status_code=502,
        )

    files = result.get("files", [])
    for drive_file in files:
        drive_file["is_folder"] = drive_file.get("mimeType") == "application/vnd.google-apps.folder"
        drive_file["link_permission"] = next(
            (
                permission
                for permission in drive_file.get("permissions", [])
                if permission.get("type") == "anyone"
            ),
            None,
        )
    next_page_url = None
    if result.get("nextPageToken"):
        query_params = {"page_token": result["nextPageToken"]}
        if folder_id:
            query_params["folder_id"] = folder_id
        next_page_url = (
            f"/accounts/{quote(email, safe='@.')}/files?{urlencode(query_params)}"
        )
    return templates.TemplateResponse(
        request=request,
        name="drive.html",
        context={
            "email": email,
            "files": files,
            "folder": current_folder,
            "folder_id": folder_id or "",
            "parent_folder_id": parent_folder_id,
            "next_page_url": next_page_url,
            "notice": request.query_params.get("notice"),
            "shared_link": request.query_params.get("shared_link"),
            "has_sharing_scope": _account_has_scope(email, SHARING_SCOPE),
            "format_bytes": _format_bytes,
        },
    )


@app.post("/accounts/{email}/upload")
def upload_to_drive(
    email: str,
    file: UploadFile = File(...),
    folder_id: str = Form(""),
):
    if email not in {account["email"] for account in list_accounts(DATABASE_PATH)}:
        return RedirectResponse("/?notice=Connect+this+Google+account+first", 303)
    folder_id = folder_id or None
    _validate_folder_id(folder_id)
    if not file.filename:
        return RedirectResponse(_drive_path(email, folder_id), 303)

    try:
        service = _drive_service(email)
        body = {"name": Path(file.filename).name}
        if folder_id:
            body["parents"] = [folder_id]
        media = MediaIoBaseUpload(
            file.file,
            mimetype=file.content_type or "application/octet-stream",
            resumable=True,
        )
        service.files().create(
            body=body, media_body=media, fields="id,name"
        ).execute()
    except HttpError as error:
        if error.resp.status == 403:
            notice = "Upload denied. Reconnect this account and grant file creation access."
        else:
            notice = f"Upload failed (Google Drive HTTP {error.resp.status})."
        return _drive_redirect(email, folder_id, notice)
    except Exception:
        notice = "Upload failed. Reconnect the account and try again."
        return _drive_redirect(email, folder_id, notice)
    return _drive_redirect(email, folder_id, "File uploaded to Google Drive")


@app.post("/accounts/{email}/files/{file_id}/share")
def set_file_link_access(
    email: str,
    file_id: str,
    role: str = Form(...),
    folder_id: str = Form(""),
):
    if email not in {account["email"] for account in list_accounts(DATABASE_PATH)}:
        return RedirectResponse("/?notice=Connect+this+Google+account+first", 303)
    _validate_folder_id(folder_id or None)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id):
        raise HTTPException(status_code=400, detail="Invalid Drive file ID")
    if role not in {"reader", "commenter", "writer", "none"}:
        raise HTTPException(status_code=400, detail="Unsupported link access role")
    try:
        if not _account_has_scope(email, SHARING_SCOPE):
            return _drive_redirect(
                email,
                folder_id or None,
                "Reconnect this Google account and approve full Drive access before changing sharing",
            )
    except (KeyError, ValueError):
        return _drive_redirect(email, folder_id or None, "Reconnect this Google account before sharing files")

    try:
        service = _drive_service(email)
        file_resource = service.files().get(
            fileId=file_id,
            fields="id,webViewLink,permissions(id,type,role),capabilities(canShare)",
            supportsAllDrives=True,
        ).execute()
        if not file_resource.get("capabilities", {}).get("canShare", True):
            return _drive_redirect(
                email,
                folder_id or None,
                "This account cannot change sharing for this file. Ask its owner for sharing access.",
            )
        link_permission = next(
            (
                permission
                for permission in file_resource.get("permissions", [])
                if permission.get("type") == "anyone"
            ),
            None,
        )

        if role == "none":
            if link_permission:
                service.permissions().delete(
                    fileId=file_id,
                    permissionId=link_permission["id"],
                    supportsAllDrives=True,
                ).execute()
            return _drive_redirect(email, folder_id or None, "Link access removed")

        permission_body = {
            "type": "anyone",
            "role": role,
            "allowFileDiscovery": False,
        }
        if link_permission:
            service.permissions().update(
                fileId=file_id,
                permissionId=link_permission["id"],
                body={"role": role},
                fields="id,type,role",
                supportsAllDrives=True,
            ).execute()
        else:
            service.permissions().create(
                fileId=file_id,
                body=permission_body,
                fields="id,type,role",
                supportsAllDrives=True,
            ).execute()
    except HttpError as error:
        if error.resp.status == 404:
            notice = "That file is unavailable or was deleted"
        elif error.resp.status == 403:
            notice = _sharing_error_notice(error)
        else:
            notice = f"Could not update link access (Google Drive HTTP {error.resp.status})"
        return _drive_redirect(email, folder_id or None, notice)
    except Exception:
        return _drive_redirect(email, folder_id or None, "Could not update link access; reconnect and try again")

    if role == "writer":
        access_label = "Editor"
    else:
        access_label = {"reader": "Viewer", "commenter": "Commenter"}[role]
    link = file_resource.get("webViewLink") or f"https://drive.google.com/open?id={file_id}"
    return _drive_redirect(
        email,
        folder_id or None,
        f"Anyone with the link can now access this file as {access_label}",
        link,
    )


@app.post("/accounts/{email}/disconnect")
def disconnect_account(email: str):
    delete_account(DATABASE_PATH, email)
    return RedirectResponse("/?notice=Google+Drive+account+disconnected", 303)


@app.get("/health")
def health():
    return {"status": "ok"}