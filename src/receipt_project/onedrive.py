
import hashlib
import os
import re
from pathlib import Path
from urllib.parse import quote

import requests
from dotenv import load_dotenv, set_key


# --------------------------------------------------
# Microsoft Graph configuration
# --------------------------------------------------

AUTHORITY = "https://login.microsoftonline.com/consumers"
TOKEN_URL = f"{AUTHORITY}/oauth2/v2.0/token"
GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

SCOPES = [
    "offline_access",
    "https://graph.microsoft.com/Files.ReadWrite",
    "https://graph.microsoft.com/User.Read",
]

RECEIPT_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".pdf",
}


# --------------------------------------------------
# Filename utilities
# --------------------------------------------------

def sanitize_filename(filename: str) -> str:
    """
    Replace characters that OneDrive disallows.

    Use when creating output/archive filenames.
    Preserve the original filename for input records.
    """
    safe_name = re.sub(
        r'[<>:"/\\|?*\x00-\x1f]',
        "_",
        filename,
    )

    safe_name = safe_name.strip().rstrip(".")

    if not safe_name:
        raise ValueError("Filename becomes empty after sanitization")

    return safe_name


def get_item_tag(item_id: str) -> str:
    """Create a stable, short identifier for a OneDrive item."""
    return hashlib.sha256(
        item_id.encode("utf-8")
    ).hexdigest()[:12]


def make_archive_name(
    item_id: str,
    original_filename: str,
    extension: str | None = None,
) -> str:
    """
    Create a sanitized filename with a stable identifier.
    """
    path = Path(original_filename)
    safe_stem = sanitize_filename(path.stem)
    item_tag = get_item_tag(item_id)

    suffix = extension if extension is not None else path.suffix

    return f"{safe_stem}__{item_tag}{suffix}"


# --------------------------------------------------
# Microsoft authentication
# --------------------------------------------------

def get_access_token() -> str:
    """
    Exchange a refresh token for a Microsoft Graph access token.
    """
    load_dotenv()

    client_id = os.getenv("MICROSOFT_CLIENT_ID")
    refresh_token = os.getenv("MICROSOFT_REFRESH_TOKEN")

    if not client_id:
        raise RuntimeError(
            "MICROSOFT_CLIENT_ID is missing"
        )

    if not refresh_token:
        raise RuntimeError(
            "MICROSOFT_REFRESH_TOKEN is missing"
        )

    response = requests.post(
        TOKEN_URL,
        data={
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": " ".join(SCOPES),
        },
        timeout=30,
    )

    response.raise_for_status()
    token_data = response.json()

    access_token = token_data.get("access_token")

    if not access_token:
        raise RuntimeError(
            "Microsoft returned an empty access token"
        )

    new_refresh_token = token_data.get("refresh_token")

    if (
        new_refresh_token
        and new_refresh_token != refresh_token
    ):
        os.environ["MICROSOFT_REFRESH_TOKEN"] = new_refresh_token

        env_path = Path(".env")

        if env_path.exists():
            set_key(
                str(env_path),
                "MICROSOFT_REFRESH_TOKEN",
                new_refresh_token,
            )

    return access_token


def graph_headers() -> dict:
    """Generate authorization headers for Graph."""
    return {
        "Authorization": f"Bearer {get_access_token()}",
    }


# --------------------------------------------------
# OneDrive folder utilities
# --------------------------------------------------

def ensure_receipts_folder() -> dict:
    """Return or create the root /Receipts folder."""
    headers = graph_headers()

    response = requests.get(
        f"{GRAPH_BASE_URL}/me/drive/root:/Receipts",
        headers=headers,
        timeout=30,
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code != 404:
        response.raise_for_status()

    response = requests.post(
        f"{GRAPH_BASE_URL}/me/drive/root/children",
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        json={
            "name": "Receipts",
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def ensure_child_folder(folder_name: str) -> dict:
    """
    Return or create a child folder inside /Receipts.
    """
    if folder_name not in {"processed", "review", "extracted"}:
        raise ValueError("Unsupported receipt subfolder")

    ensure_receipts_folder()
    headers = graph_headers()

    url = (
        f"{GRAPH_BASE_URL}"
        f"/me/drive/root:/Receipts/{folder_name}"
    )

    response = requests.get(
        url,
        headers=headers,
        timeout=30,
    )

    if response.status_code == 200:
        return response.json()

    if response.status_code != 404:
        response.raise_for_status()

    response = requests.post(
        f"{GRAPH_BASE_URL}/me/drive/root:/Receipts:/children",
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        json={
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail",
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def ensure_processed_folder() -> dict:
    return ensure_child_folder("processed")


def ensure_review_folder() -> dict:
    return ensure_child_folder("review")


def ensure_extracted_folder() -> dict:
    return ensure_child_folder("extracted")


# --------------------------------------------------
# List receipt files
# --------------------------------------------------

def list_receipt_files() -> list[dict]:
    """
    List receipt files from /Receipts.

    Skip folders and unsupported extensions.
    Handle Graph pagination.
    """
    headers = graph_headers()

    url = (
        f"{GRAPH_BASE_URL}"
        "/me/drive/root:/Receipts:/children"
    )

    receipt_files = []

    while url:
        response = requests.get(
            url,
            headers=headers,
            timeout=30,
        )

        response.raise_for_status()
        payload = response.json()

        for item in payload.get("value", []):
            if "file" not in item:
                continue

            name = item.get("name", "")
            suffix = Path(name).suffix.lower()

            if suffix in RECEIPT_EXTENSIONS:
                receipt_files.append(item)

        url = payload.get("@odata.nextLink")

    return receipt_files


# --------------------------------------------------
# Download receipt
# --------------------------------------------------

def download_drive_item(item_id: str) -> bytes:
    """Download an existing OneDrive receipt."""
    response = requests.get(
        (
            f"{GRAPH_BASE_URL}"
            f"/me/drive/items/{item_id}/content"
        ),
        headers=graph_headers(),
        timeout=30,
    )

    response.raise_for_status()

    return response.content


# --------------------------------------------------
# Upload extracted JSON
# --------------------------------------------------

def upload_extraction_json(
    item_id: str,
    original_filename: str,
    json_content: str,
) -> dict:
    """
    Save raw extracted JSON into /Receipts/extracted.

    Sanitize the destination filename to prevent
    Microsoft Graph 400 errors.
    """
    extracted_folder = ensure_extracted_folder()

    json_filename = make_archive_name(
        item_id,
        original_filename,
        extension=".json",
    )

    # Encode filename as a URL path segment.
    encoded_filename = quote(
        json_filename,
        safe="",
    )

    upload_url = (
        f"{GRAPH_BASE_URL}"
        f"/me/drive/items/{extracted_folder['id']}"
        f":/{encoded_filename}:/content"
    )

    response = requests.put(
        upload_url,
        headers={
            **graph_headers(),
            "Content-Type": "application/json",
        },
        data=json_content.encode("utf-8"),
        timeout=30,
    )

    response.raise_for_status()

    print(f"Saved extraction JSON: {json_filename}")

    return response.json()


# --------------------------------------------------
# Move receipts
# --------------------------------------------------

def move_drive_item(
    item_id: str,
    original_filename: str,
    destination_folder: dict,
) -> dict:
    """
    Move a receipt to another OneDrive folder.

    Rename the destination file using a sanitized name
    plus a stable identifier to avoid collisions.
    """
    archived_name = make_archive_name(
        item_id,
        original_filename,
    )

    response = requests.patch(
        f"{GRAPH_BASE_URL}/me/drive/items/{item_id}",
        headers={
            **graph_headers(),
            "Content-Type": "application/json",
        },
        json={
            "parentReference": {
                "id": destination_folder["id"],
            },
            "name": archived_name,
        },
        timeout=30,
    )

    response.raise_for_status()

    return response.json()


def move_drive_item_to_processed(
    item_id: str,
    original_filename: str,
) -> dict:
    """Move a successful receipt into /Receipts/processed."""
    folder = ensure_processed_folder()

    return move_drive_item(
        item_id,
        original_filename,
        folder,
    )


def move_drive_item_to_review(
    item_id: str,
    original_filename: str,
) -> dict:
    """Move a receipt requiring review into /Receipts/review."""
    folder = ensure_review_folder()

    return move_drive_item(
        item_id,
        original_filename,
        folder,
    )
