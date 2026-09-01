"""One-time Google consent, then prints the IDs needed for .env.

Run:  python scripts/authorize.py
Opens a browser once; stores a refresh token in credentials/token.json.
"""
from googleapiclient.discovery import build

from auth import get_credentials

FOLDER_MIME = "application/vnd.google-apps.folder"
SHEET_MIME = "application/vnd.google-apps.spreadsheet"


def _find(drive, mime, name_contains=None):
    q = f"mimeType = '{mime}' and trashed = false"
    if name_contains:
        q += f" and name contains '{name_contains}'"
    out, token = [], None
    while True:
        r = drive.files().list(
            q=q, fields="nextPageToken, files(id, name, parents)",
            pageSize=100, pageToken=token,
        ).execute()
        out.extend(r.get("files", []))
        token = r.get("nextPageToken")
        if not token:
            return out


def main():
    creds = get_credentials(interactive=True)
    print("\nAuthorized. Token saved to credentials/token.json\n")

    drive = build("drive", "v3", credentials=creds)

    print("=" * 60)
    print("DRIVE FOLDERS")
    print("=" * 60)
    for f in _find(drive, FOLDER_MIME):
        print(f"  {f['name']:<35} {f['id']}")

    print()
    print("=" * 60)
    print("SPREADSHEETS")
    print("=" * 60)
    for f in _find(drive, SHEET_MIME):
        print(f"  {f['name']:<35} {f['id']}")
    print()


if __name__ == "__main__":
    main()
