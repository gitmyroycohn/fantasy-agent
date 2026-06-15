"""
Google Drive sync — pushes roster state and run logs to Drive
so Claude can read them in future sessions.
"""
import json
import logging
from datetime import datetime
from pathlib import Path
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
TOKEN_PATH = Path(__file__).parent.parent / "config" / "token.json"
CREDS_PATH = Path(__file__).parent.parent / "config" / "credentials.json"


def get_drive_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("drive", "v3", credentials=creds)


def sync_to_drive(roster_md: str, run_output: dict, folder_name: str = "fantasy-agent"):
    """Push roster state and latest run output to Drive."""
    try:
        service = get_drive_service()
        folder_id = _get_or_create_folder(service, folder_name)

        _upsert_file(service, folder_id, "fantasy_roster_state.md", roster_md, "text/plain")
        _upsert_file(
            service, folder_id, "fantasy_run_log.md",
            _format_run_log(run_output), "text/plain"
        )
        logger.info("Drive sync complete")
    except Exception as e:
        logger.error("Drive sync failed: %s", e)


def _get_or_create_folder(service, name: str) -> str:
    res = service.files().list(
        q=f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id)",
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def _upsert_file(service, folder_id: str, filename: str, content: str, mime: str):
    res = service.files().list(
        q=f"name='{filename}' and '{folder_id}' in parents and trashed=false",
        fields="files(id)",
    ).execute()
    media = MediaInMemoryUpload(content.encode(), mimetype=mime, resumable=False)
    existing = res.get("files", [])
    if existing:
        service.files().update(fileId=existing[0]["id"], media_body=media).execute()
    else:
        meta = {"name": filename, "parents": [folder_id]}
        service.files().create(body=meta, media_body=media, fields="id").execute()
    logger.info("Synced %s to Drive", filename)


def _format_run_log(output: dict) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    return f"# Fantasy Agent Run Log\n\n**Last run:** {ts}\n\n```json\n{json.dumps(output, indent=2)}\n```\n"
