"""
extractor.py

Takes a free-text description (written naturally, no required structure) and
extracts structured fields using Claude. This is used for BOTH beneficiary intake
and mentor intake - the schema differs slightly, so we pass a "kind" parameter.

Critically: this only PROPOSES structured data. Nothing here writes to storage.
The calling code (n8n) must show the result to a human for confirmation before
it's saved anywhere - especially safety-critical constraints like gender restrictions.
"""

import os
import json
from anthropic import Anthropic

client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

MODEL = "claude-sonnet-4-6"

# JSON schema Claude must follow when extracting a BENEFICIARY description
BENEFICIARY_SCHEMA = {
    "name": "string or null - person's name if mentioned",
    "age": "number or null",
    "gender": "'male' | 'female' | null",
    "phone": "string or null - phone number if mentioned, as stated",
    "region": "string or null - geographic area or town mentioned",
    "weekly_hours_needed": "number or null - weekly hours of mentoring needed if mentioned",
    "urgency": "'low' | 'medium' | 'high' or null",
    "needs_summary": "short string - a 1-2 sentence neutral summary of the stated need",
    "mentor_gender_restriction": "'male_only' | 'female_only' | null - ONLY set this if the text explicitly states a restriction on mentor gender. Do not infer this from context or assumptions - only from explicit statements.",
    "mentor_age_restriction": "'young' | 'older' | null - set 'young' only if text explicitly requests a young mentor (e.g. 'mentor under 35', 'young mentor'). Set 'older' only if text explicitly requests an older mentor (e.g. 'mature mentor', 'older mentor'). Do not infer from context.",
    "other_constraints": "array of short strings - any other explicit constraints mentioned. Do not include soft preferences as if they were hard constraints.",
}

# JSON schema Claude must follow when extracting a MENTOR description
MENTOR_SCHEMA = {
    "name": "string or null",
    "age": "number or null",
    "gender": "'male' | 'female' | null",
    "phone": "string or null - phone number if mentioned, as stated",
    "region": "string or null - geographic area or town mentioned",
    "weekly_hours_available": "number or null - weekly hours the mentor is available to give",
    "skills": "array of short strings - areas of skill/expertise mentioned",
    "availability": "string or null - days/hours mentioned, as stated",
    "hobbies": "array of short strings - hobbies/interests mentioned",
}


def extract_fields(description: str, kind: str) -> dict:
    """
    Extract structured fields from free text.

    Args:
        description: the free-text description written by the user
        kind: "beneficiary" or "mentor" - determines which schema to use

    Returns:
        dict with the extracted fields, plus a "confidence_notes" field listing
        anything ambiguous that the human reviewer should double check.
    """
    if kind == "beneficiary":
        schema = BENEFICIARY_SCHEMA
    elif kind == "mentor":
        schema = MENTOR_SCHEMA
    else:
        raise ValueError(f"Unknown kind: {kind}")

    system_prompt = f"""You extract structured data from free-text descriptions written by a social worker.
The text may be in Hebrew or English, written in any order, with no fixed structure.

Extract fields matching this schema exactly:
{json.dumps(schema, indent=2)}

Critical rules:
- Only extract what is EXPLICITLY stated in the text. Never infer, assume, or guess.
- For mentor_gender_restriction specifically: only set this field if the text directly states
  a restriction (e.g. "needs a male mentor", "no female mentors", "she's not comfortable with men").
  Do not set it based on tone, age, or context alone.
- If something is ambiguous or could be read multiple ways, leave the field null/empty
  and add a note in "confidence_notes" explaining the ambiguity, instead of guessing.
- Respond with ONLY valid JSON. No preamble, no markdown formatting, no explanation outside the JSON.

Your response must be a single JSON object with all schema fields, plus an additional
"confidence_notes" field (array of strings) for anything the human should double-check.
"""

    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": description}],
    )

    raw_text = response.content[0].text.strip()

    # Defensive cleanup in case the model wraps the JSON in markdown fences
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:].strip()

    return json.loads(raw_text)
