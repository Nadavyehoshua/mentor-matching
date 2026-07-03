"""
main.py

HTTP layer for the mentor-matching agent.
POST /extract        - takes free text + kind, returns structured fields
POST /match          - takes a beneficiary profile, returns ranked mentor matches
GET  /beneficiaries  - returns all beneficiaries from the sheet
GET  /mentors        - returns all mentors from the sheet
GET  /matches        - returns all matches from the sheet
POST /delete-match   - removes a match and restores mentor hours
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from extractor import extract_fields
from matcher import find_matches, get_all_beneficiaries, _read_sheet, _get_sheets_service
import os

SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID")

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class ExtractRequest(BaseModel):
    description: str
    kind: str


class MatchRequest(BaseModel):
    beneficiary: dict


class DeleteMatchRequest(BaseModel):
    beneficiary_name: str
    mentor_name: str
    hours_allocated: float


@app.post("/extract")
def handle_extract(req: ExtractRequest):
    return extract_fields(description=req.description, kind=req.kind)


@app.post("/match")
def handle_match(req: MatchRequest):
    return find_matches(beneficiary=req.beneficiary)


@app.get("/beneficiaries")
def handle_get_beneficiaries():
    return get_all_beneficiaries()


@app.get("/mentors")
def handle_get_mentors():
    return _read_sheet("Mentors")


@app.get("/matches")
def handle_get_matches():
    return _read_sheet("Matches")


@app.post("/delete-match")
def handle_delete_match(req: DeleteMatchRequest):
    """
    Deletes a match row from the Matches sheet and restores the mentor's hours.
    Uses the Google Sheets API directly with write permissions.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    SCOPES_RW = ["https://www.googleapis.com/auth/spreadsheets"]

    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES_RW
    )
    service = build("sheets", "v4", credentials=credentials)

    # 1. Find and delete the match row
    matches_result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Matches"
    ).execute()
    values = matches_result.get("values", [])
    headers = values[0] if values else []

    row_to_delete = None
    for i, row in enumerate(values[1:], start=2):
        padded = row + [""] * (len(headers) - len(row))
        row_dict = dict(zip(headers, padded))
        if row_dict.get("BeneficiaryName") == req.beneficiary_name and row_dict.get("MentorName") == req.mentor_name:
            row_to_delete = i
            break

    if row_to_delete:
        # Get the sheet ID for Matches tab
        sheet_meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
        matches_sheet_id = next(
            s["properties"]["sheetId"] for s in sheet_meta["sheets"]
            if s["properties"]["title"] == "Matches"
        )
        service.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"deleteDimension": {"range": {
                "sheetId": matches_sheet_id, "dimension": "ROWS",
                "startIndex": row_to_delete - 1, "endIndex": row_to_delete
            }}}]}
        ).execute()

    # 2. Restore mentor hours in Mentors sheet
    mentors_result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID, range="Mentors"
    ).execute()
    mentor_values = mentors_result.get("values", [])
    mentor_headers = mentor_values[0] if mentor_values else []

    for i, row in enumerate(mentor_values[1:], start=2):
        padded = row + [""] * (len(mentor_headers) - len(row))
        row_dict = dict(zip(mentor_headers, padded))
        if row_dict.get("Name") == req.mentor_name:
            used_col = mentor_headers.index("WeeklyHoursUsed") if "WeeklyHoursUsed" in mentor_headers else None
            if used_col is not None:
                current_used = float(row_dict.get("WeeklyHoursUsed") or 0)
                new_used = max(0, current_used - req.hours_allocated)
                col_letter = chr(ord('A') + used_col)
                service.spreadsheets().values().update(
                    spreadsheetId=SPREADSHEET_ID,
                    range=f"Mentors!{col_letter}{i}",
                    valueInputOption="RAW",
                    body={"values": [[new_used]]}
                ).execute()
            break

    return {"status": "ok"}


@app.get("/health")
def health_check():
    return {"status": "ok"}
