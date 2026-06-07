from fastapi import FastAPI
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from anthropic import Anthropic
from fastapi.middleware.cors import CORSMiddleware

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