"""
main.py

HTTP layer for the mentor-matching agent.
Supports reading service account from env var (for Render hosting) or file (for local dev).
"""

import os
import json
import base64
import tempfile
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Handle service account — from Base64 env var (Render) or file (local)
def setup_service_account():
    b64 = os.environ.get("GOOGLE_SERVICE_ACCOUNT_B64")
    if b64:
        json_bytes = base64.b64decode(b64)
        tmp = tempfile.NamedTemporaryFile(mode='wb', suffix='.json', delete=False)
        tmp.write(json_bytes)
        tmp.close()
        os.environ["GOOGLE_SERVICE_ACCOUNT_FILE"] = tmp.name
    # If no B64, falls back to GOOGLE_SERVICE_ACCOUNT_FILE env var (local dev)

setup_service_account()

from extractor import extract_fields
from matcher import find_matches, get_all_beneficiaries, _read_sheet, _get_sheets_service

SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID")


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

    # 2. Restore mentor hours
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


class DeletePersonRequest(BaseModel):
    name: str


@app.post("/delete-beneficiary")
def handle_delete_beneficiary(req: DeletePersonRequest):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    SCOPES_RW = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES_RW)
    service = build("sheets", "v4", credentials=credentials)

    sheet_meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheet_meta["sheets"]}

    # 1. Find active match for this beneficiary
    matches = _read_sheet("Matches")
    active_match = next((m for m in matches if m.get("BeneficiaryName", "").strip() == req.name.strip()), None)

    # 2. If matched — restore mentor hours and delete match row
    if active_match:
        mentor_name = active_match.get("MentorName", "")
        hours = float(active_match.get("HoursAllocated") or 0)

        # Restore mentor hours
        mentor_values = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Mentors").execute().get("values", [])
        mentor_headers = mentor_values[0] if mentor_values else []
        for i, row in enumerate(mentor_values[1:], start=2):
            padded = row + [""] * (len(mentor_headers) - len(row))
            rd = dict(zip(mentor_headers, padded))
            if rd.get("Name", "").strip() == mentor_name.strip():
                used_col = mentor_headers.index("WeeklyHoursUsed") if "WeeklyHoursUsed" in mentor_headers else None
                if used_col is not None:
                    new_used = max(0, float(rd.get("WeeklyHoursUsed") or 0) - hours)
                    col_letter = chr(ord('A') + used_col)
                    service.spreadsheets().values().update(
                        spreadsheetId=SPREADSHEET_ID, range=f"Mentors!{col_letter}{i}",
                        valueInputOption="RAW", body={"values": [[new_used]]}
                    ).execute()
                break

        # Delete match row
        match_values = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Matches").execute().get("values", [])
        match_headers = match_values[0] if match_values else []
        for i, row in enumerate(match_values[1:], start=2):
            padded = row + [""] * (len(match_headers) - len(row))
            rd = dict(zip(match_headers, padded))
            if rd.get("BeneficiaryName", "").strip() == req.name.strip():
                service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"deleteDimension": {"range": {
                    "sheetId": sheet_ids["Matches"], "dimension": "ROWS",
                    "startIndex": i - 1, "endIndex": i
                }}}]}).execute()
                break

    # 3. Delete beneficiary row
    ben_values = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Beneficiaries").execute().get("values", [])
    ben_headers = ben_values[0] if ben_values else []
    for i, row in enumerate(ben_values[1:], start=2):
        padded = row + [""] * (len(ben_headers) - len(row))
        rd = dict(zip(ben_headers, padded))
        if rd.get("Name", "").strip() == req.name.strip():
            service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"deleteDimension": {"range": {
                "sheetId": sheet_ids["Beneficiaries"], "dimension": "ROWS",
                "startIndex": i - 1, "endIndex": i
            }}}]}).execute()
            break

    return {"status": "ok", "match_cancelled": active_match is not None}


@app.post("/delete-mentor")
def handle_delete_mentor(req: DeletePersonRequest):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
    SCOPES_RW = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials = service_account.Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES_RW)
    service = build("sheets", "v4", credentials=credentials)

    sheet_meta = service.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    sheet_ids = {s["properties"]["title"]: s["properties"]["sheetId"] for s in sheet_meta["sheets"]}

    # 1. Find active match for this mentor
    matches = _read_sheet("Matches")
    active_match = next((m for m in matches if m.get("MentorName", "").strip() == req.name.strip()), None)

    # 2. If matched — delete the match row (beneficiary returns to "waiting")
    if active_match:
        match_values = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Matches").execute().get("values", [])
        match_headers = match_values[0] if match_values else []
        for i, row in enumerate(match_values[1:], start=2):
            padded = row + [""] * (len(match_headers) - len(row))
            rd = dict(zip(match_headers, padded))
            if rd.get("MentorName", "").strip() == req.name.strip():
                service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"deleteDimension": {"range": {
                    "sheetId": sheet_ids["Matches"], "dimension": "ROWS",
                    "startIndex": i - 1, "endIndex": i
                }}}]}).execute()
                break

    # 3. Delete mentor row
    mentor_values = service.spreadsheets().values().get(spreadsheetId=SPREADSHEET_ID, range="Mentors").execute().get("values", [])
    mentor_headers = mentor_values[0] if mentor_values else []
    for i, row in enumerate(mentor_values[1:], start=2):
        padded = row + [""] * (len(mentor_headers) - len(row))
        rd = dict(zip(mentor_headers, padded))
        if rd.get("Name", "").strip() == req.name.strip():
            service.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": [{"deleteDimension": {"range": {
                "sheetId": sheet_ids["Mentors"], "dimension": "ROWS",
                "startIndex": i - 1, "endIndex": i
            }}}]}).execute()
            break

    return {"status": "ok", "match_cancelled": active_match is not None}
