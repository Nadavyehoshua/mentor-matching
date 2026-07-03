"""
matcher.py

Matching logic: given a beneficiary's profile, finds and ranks suitable mentors.

Two-stage process:
1. Hard filter (pure Python, no AI) - eliminates mentors who violate explicit constraints
2. Soft ranking (Claude API) - ranks remaining eligible mentors by fit quality
"""

import os
import json
from anthropic import Anthropic
from google.oauth2 import service_account
from googleapiclient.discovery import build

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
MODEL = "claude-sonnet-4-6"

SPREADSHEET_ID = os.environ.get("GOOGLE_SPREADSHEET_ID")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service-account.json")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def _get_sheets_service():
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES
    )
    return build("sheets", "v4", credentials=credentials)


def _read_sheet(sheet_name: str) -> list[dict]:
    """Read all rows from a sheet tab, return as list of dicts using header row as keys."""
    service = _get_sheets_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=sheet_name
    ).execute()

    values = result.get("values", [])
    if len(values) < 2:
        return []

    headers = values[0]
    rows = []
    for row in values[1:]:
        padded = row + [""] * (len(headers) - len(row))
        rows.append(dict(zip(headers, padded)))
    return rows


def get_all_beneficiaries() -> list[dict]:
    """Return all beneficiaries from the sheet - used to populate the dropdown."""
    return _read_sheet("Beneficiaries")


def _safe_float(val, default=0.0) -> float:
    """Safely convert a value to float, returning default if conversion fails."""
    try:
        return float(val) if val not in (None, "", "null") else default
    except (ValueError, TypeError):
        return default


def _hard_filter(beneficiary: dict, mentors: list[dict]) -> list[dict]:
    """
    Apply hard constraints - eliminate mentors who violate any constraint.
    This is pure deterministic code, no AI involved.

    Filters applied:
    1. Gender restriction (male_only / female_only)
    2. Hours availability - mentor must have enough free hours for this beneficiary
    """
    eligible = []
    restriction = (beneficiary.get("MentorGenderRestriction") or
                   beneficiary.get("mentor_gender_restriction") or "").strip().lower()

    hours_needed = _safe_float(
        beneficiary.get("WeeklyHoursNeeded") or beneficiary.get("weekly_hours_needed")
    )

    age_restriction = (beneficiary.get("MentorAgeRestriction") or
                        beneficiary.get("mentor_age_restriction") or "").strip().lower()

    for mentor in mentors:
        mentor_gender = mentor.get("Gender", "").strip().lower()
        mentor_age = _safe_float(mentor.get("Age"), default=0)

        # 1. Minimum age check — all mentors must be over 18
        if mentor_age > 0 and mentor_age < 18:
            continue

        # 2. Gender restriction check
        if restriction == "male_only" and mentor_gender not in ("male", "זכר", "גבר", "man", "m"):
            continue
        if restriction == "female_only" and mentor_gender not in ("female", "נקבה", "אישה", "woman", "f"):
            continue

        # 3. Age restriction check
        if age_restriction == "young" and mentor_age > 35:
            continue
        if age_restriction == "older" and mentor_age <= 35:
            continue

        # 4. Hours availability check
        if hours_needed > 0:
            hours_available = _safe_float(mentor.get("WeeklyHoursAvailable"))
            hours_used = _safe_float(mentor.get("WeeklyHoursUsed"))
            hours_free = hours_available - hours_used

            if hours_free < hours_needed:
                continue  # Not enough free hours

        eligible.append(mentor)

    return eligible


def _rank_with_ai(beneficiary: dict, eligible_mentors: list[dict]) -> list[dict]:
    """
    Use Claude to rank eligible mentors by soft fit.
    Returns the same mentors list reordered, with 'match_reason' and 'match_score' added.
    """
    if not eligible_mentors:
        return []

    if len(eligible_mentors) == 1:
        eligible_mentors[0]["match_reason"] = "החונך היחיד שעובר את כל הסינונים."
        eligible_mentors[0]["match_score"] = 8
        return eligible_mentors

    system_prompt = """You are a matching assistant helping a social worker pair beneficiaries with mentors.
You will receive a beneficiary profile and a list of eligible mentors (who already passed hard constraint filters).
Rank the mentors from best fit to worst fit based on:
- Alignment between beneficiary needs and mentor skills
- Geographic proximity (same town/region = better)
- Shared hobbies or interests
- Mentor availability vs beneficiary schedule

Respond ONLY with a valid JSON array, no markdown, no preamble.
Each item must have:
- "index": the original 0-based index of the mentor in the input list
- "match_score": integer 1-10 (10 = best fit)
- "match_reason": 1-2 sentence explanation in Hebrew of why this mentor fits

Example: [{"index": 2, "match_score": 9, "match_reason": "..."}, ...]
"""

    user_message = f"""Beneficiary profile:
{json.dumps(beneficiary, ensure_ascii=False, indent=2)}

Eligible mentors:
{json.dumps([{"index": i, **m} for i, m in enumerate(eligible_mentors)], ensure_ascii=False, indent=2)}

Rank these mentors from best to worst fit."""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}]
    )

    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()

    rankings = json.loads(raw)

    ranked = []
    for r in rankings:
        mentor = eligible_mentors[r["index"]].copy()
        mentor["match_score"] = r["match_score"]
        mentor["match_reason"] = r["match_reason"]
        ranked.append(mentor)

    return ranked


def find_matches(beneficiary: dict) -> dict:
    """
    Main matching function.
    Returns eligible_count, filtered_out, and ranked matches.
    Also checks if the beneficiary is already matched to a mentor.
    """
    # Check if this beneficiary is already matched
    beneficiary_name = (beneficiary.get("Name") or beneficiary.get("name") or "").strip()
    if beneficiary_name:
        existing_matches = _read_sheet("Matches")
        for match in existing_matches:
            if match.get("BeneficiaryName", "").strip() == beneficiary_name:
                return {
                    "already_matched": True,
                    "existing_mentor": match.get("MentorName", ""),
                    "eligible_count": 0,
                    "filtered_out": 0,
                    "matches": []
                }

    all_mentors = _read_sheet("Mentors")
    total = len(all_mentors)

    eligible = _hard_filter(beneficiary, all_mentors)
    filtered_out = total - len(eligible)

    if not eligible:
        return {
            "already_matched": False,
            "eligible_count": 0,
            "filtered_out": filtered_out,
            "matches": []
        }

    ranked = _rank_with_ai(beneficiary, eligible)

    return {
        "already_matched": False,
        "eligible_count": len(eligible),
        "filtered_out": filtered_out,
        "matches": ranked
    }
