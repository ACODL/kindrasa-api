from fastapi import FastAPI
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from anthropic import Anthropic
from fastapi.middleware.cors import CORSMiddleware

import os

os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"

from google_auth_oauthlib.flow import Flow
from fastapi.responses import RedirectResponse
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.mime.text import MIMEText

import base64
import json 

load_dotenv()

supabase_url = os.environ.get("SUPABASE_URL")
supabase_key = os.environ.get("SUPABASE_SERVICE_KEY")
supabase = create_client(supabase_url, supabase_key)

google_client_id = os.environ.get("GOOGLE_CLIENT_ID")
google_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/userinfo.email",
]

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

class SendEmailRequest(BaseModel):
    agent_id: str
    to_email: str
    subject: str
    body: str

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

@app.get("/auth/google/start")
def google_auth_start(agent_id: str):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": google_client_id,
                "client_secret": google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GMAIL_SCOPES,
    )
    flow.redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")

    flow.code_verifier = "kindrasastaticverifierstringthatislongenough1234567890"

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=agent_id,
    )

    return RedirectResponse(auth_url)

@app.get("/auth/google/callback")
def google_auth_callback(code: str, state: str):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": google_client_id,
                "client_secret": google_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=GMAIL_SCOPES,
    )
    flow.redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI")

    flow.code_verifier = "kindrasastaticverifierstringthatislongenough1234567890"

    # exchange the code for tokens
    flow.fetch_token(code=code)
    credentials = flow.credentials

    userinfo_service = build("oauth2", "v2", credentials=credentials)
    userinfo = userinfo_service.userinfo().get().execute()
    email_address = userinfo["email"]

    # the agent_id we tucked into state earlier
    agent_id = state

    # store the connection
    supabase.table("email_connections").upsert({
        "agent_id": agent_id,
        "provider": "google",
        "email_address": email_address,
        "refresh_token": credentials.refresh_token,
    }, on_conflict="agent_id").execute()

    frontend_url = os.environ.get("FRONTEND_URL")
    return RedirectResponse(f"{frontend_url}/dashboard?gmail=connected")

@app.post("/send-email")
def send_email(request: SendEmailRequest):

    connection = supabase.table("email_connections").select("refresh_token, email_address").eq("agent_id", request.agent_id).single().execute()

    stored_refresh_token = connection.data["refresh_token"]
    sender_email_address = connection.data["email_address"]

    creds = Credentials(
    token=None,
    refresh_token=stored_refresh_token,
    token_uri="https://oauth2.googleapis.com/token",
    client_id=google_client_id,
    client_secret=google_client_secret,
    scopes=GMAIL_SCOPES,
    )

    mime = MIMEText(request.body)
    mime["to"] = request.to_email
    mime["from"] = sender_email_address
    mime["subject"] = request.subject
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
   
    try:
        service = build("gmail", "v1", credentials=creds)
        service.users().messages().send(userId="me", body={"raw": raw}).execute()
        frontend_url = os.environ.get("FRONTEND_URL")
        return RedirectResponse(f"{frontend_url}/dashboard?gmail=connected")
    except Exception as e:
        return {"status": "error", "detail": str(e)}