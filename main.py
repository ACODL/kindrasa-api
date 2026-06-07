from fastapi import FastAPI
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from anthropic import Anthropic
from fastapi.middleware.cors import CORSMiddleware
import json 

import os

load_dotenv()

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
supabase = create_client(supabase_url, supabase_key)

anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
client = Anthropic(api_key=anthropic_key)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class DraftRequest(BaseModel):
    lead_id: str

class EmailParseRequest(BaseModel):
    email_text: str
    agent_id: str

@app.post("/draft")
def create_draft(request: DraftRequest):

    lead = supabase.table("leads").select("*").eq("lead_id", request.lead_id).single().execute()
    activities = supabase.table("lead_activities").select("*").eq("lead_id", request.lead_id).order("created_at", desc=True).execute()

    activity_lines = []
    for activity in activities.data:
        activity_lines.append(f"- {activity['type']}: {activity['content']}")

    activity_summary = "\n".join(activity_lines)

    first_name = lead.data["first_name"]
    stage = lead.data["pipeline_stage"]

    prompt = f"""You are helping a real estate agent write a follow-up message to a lead.

    Lead name: {first_name}
    Current pipeline stage: {stage}

    Recent activity:
    {activity_summary}

    Write a brief, friendly, professional SMS follow-up message to {first_name}. Keep it under 160 characters. Do not include a subject line. Reference their interest naturally."""

    message = client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=300,
    messages=[
        {"role": "user", "content": prompt}
    ]
    )
    draft = supabase.table("ai_drafts").insert({
    "lead_id": request.lead_id,
    "agent_id": lead.data["agent_id"],
    "message_content": message.content[0].text,
    "channel": "SMS"
    }).execute()


    return {"draft": draft.data}

@app.post("/parse-email")
def parse_email(request: EmailParseRequest):

    prompt = f"""Extract real estate lead information from the following email.

    Email:
    {request.email_text}

    Return ONLY a JSON object with these exact fields:
    - first_name (string)
    - last_name (string, empty string if not found)
    - email (string, empty string if not found)
    - phone_number (string, empty string if not found)
    - notes (string: a brief summary of what the lead is interested in)

    If a field cannot be found, use an empty string. Return only the JSON, no other text."""

    message = client.messages.create(
    model="claude-haiku-4-5-20251001",
    max_tokens=300,
    messages=[
        {"role": "user", "content": prompt}
    ]
    )

    raw_text = message.content[0].text.strip()

    # remove markdown code fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]   # grab content between fences
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]            # drop the "json" language tag
        raw_text = raw_text.strip()
    try:
        extracted = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "Could not parse lead from email", "raw": raw_text}
    
    new_lead = supabase.table("leads").insert({
    "agent_id": request.agent_id,
    "first_name": extracted["first_name"],
    "last_name": extracted["last_name"],
    "email": extracted["email"],
    "phone_number": extracted["phone_number"],
    "pipeline_stage": "new"

    }).execute()

    new_lead_id = new_lead.data[0]["lead_id"]

    if extracted.get("notes"):
    
        supabase.table("lead_activities").insert({
        "lead_id": new_lead_id,
        "performing_agent": request.agent_id,
        "type": "note",
        "content": extracted["notes"]
        }).execute()

    return {"lead": new_lead.data}