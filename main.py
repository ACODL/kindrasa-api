from fastapi import FastAPI
from supabase import create_client
from dotenv import load_dotenv
from pydantic import BaseModel
from anthropic import Anthropic
from fastapi.middleware.cors import CORSMiddleware
from fastapi import UploadFile, File, Form

import vobject
import os
import csv
import io
import anthropic
import json
import re


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

class CheckEmailRequest(BaseModel):
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

    prompt = f"""You are helping a real estate agent write a follow-up EMAIL to a lead.

    Lead name: {first_name}
    Current pipeline stage: {stage}

    Recent activity:
    {activity_summary}

    Write a brief, warm, professional follow-up email. Return ONLY a JSON object with these exact fields:
    - subject (string: a short, natural email subject line)
    - body (string: the email message, friendly and concise, no subject line inside it)
    Return only the JSON, no other text."""

    message = client.messages.create(
    model="claude-sonnet-4-5",
    max_tokens=300,
    messages=[
        {"role": "user", "content": prompt}
    ]
    )

    raw_text = message.content[0].text.strip()

    # strip markdown fences if present
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {"error": "Could not parse draft", "raw": raw_text}
    
    draft = supabase.table("ai_drafts").insert({
    "lead_id": request.lead_id,
    "agent_id": lead.data["agent_id"],
    "message_content": parsed["body"],
    "subject": parsed["subject"],
    "channel": "email"
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
        return {"status": "success", "to": request.to_email}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    
@app.get("/check-email-connection")
def check(agent_id: str):
    result = supabase.table("email_connections").select("email_address").eq("agent_id", agent_id).execute()

    if result.data:
        return {"connected": True, "email_address": result.data[0]["email_address"]}
    else:
        return {"connected": False}

class FormLeadRequest(BaseModel):
    first_name: str
    last_name: str = ""
    email: str = ""
    phone_number: str = ""
    message: str = ""

@app.post("/webhook/form/{webhook_token}")
def form_webhook(webhook_token: str, request: FormLeadRequest):
    # look up which agent owns this token
    agent = supabase.table("agents").select("userid").eq("webhook_token", webhook_token).execute()

    if not agent.data:
        return {"status": "error", "detail": "Invalid token"}

    agent_id = agent.data[0]["userid"]

    # create the lead
    new_lead = supabase.table("leads").insert({
        "agent_id": agent_id,
        "first_name": request.first_name,
        "last_name": request.last_name,
        "email": request.email,
        "phone_number": request.phone_number,
        "pipeline_stage": "new"
    }).execute()

    new_lead_id = new_lead.data[0]["lead_id"]

    # log the message as a first activity, if there is one
    if request.message:
        supabase.table("lead_activities").insert({
            "lead_id": new_lead_id,
            "performing_agent": agent_id,
            "type": "note",
            "content": request.message
        }).execute()

    return {"status": "success"}

@app.post("/parse-vcard")
async def parse_vcard(agent_id: str, file: UploadFile = File(...)):
    contents = await file.read()
    text = contents.decode("utf-8")

    contacts = []
    for vcard in vobject.readComponents(text):
        if not hasattr(vcard, "fn"):
            continue  # skip contacts with no name
        contact = {"agent_id": agent_id, "status": "pending", "name": vcard.fn.value}
        if hasattr(vcard, "fn"):
            contact["name"] = vcard.fn.value
        if hasattr(vcard, "email"):
            contact["email"] = vcard.email.value
        if hasattr(vcard, "tel"):
            contact["phone"] = vcard.tel.value
        if hasattr(vcard, "org"):
            contact["company"] = vcard.org.value
        if hasattr(vcard, "note"):
            contact["note"] = vcard.note.value
        if hasattr(vcard, "bday"):
            contact["birthday"] = vcard.bday.value
        contacts.append(contact)

    if contacts:
        supabase.table("pending_contacts").insert(contacts).execute()

    return {"count": len(contacts), "contacts": contacts}

class VCardContact(BaseModel):
    name: str
    email: str = ""
    phone: str = ""
    birthday: str = ""

class ImportContactsRequest(BaseModel):
    agent_id: str
    contacts: list[VCardContact]

class AcceptContactRequest(BaseModel):
    pending_id: str

@app.post("/accept-contact")
def accept_contact(request: AcceptContactRequest):
    # get the pending contact
    pending = supabase.table("pending_contacts").select("*").eq("pending_id", request.pending_id).single().execute()
    contact = pending.data

    # split the name
    parts = contact["name"].split(" ", 1)
    first_name = parts[0]
    last_name = parts[1] if len(parts) > 1 else ""

    # validate birthday — only use it if it's a clean full date
    birthday = None
    if contact.get("birthday"):
        raw = contact["birthday"]
        # accept only YYYY-MM-DD format; skip no-year or malformed
        if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
            birthday = raw

    # create the lead
    new_lead = supabase.table("leads").insert({
        "agent_id": contact["agent_id"],
        "first_name": first_name,
        "last_name": last_name,
        "email": contact.get("email", ""),
        "phone_number": clean_phone(contact.get("phone", "")),
        "birthday": birthday,
        "pipeline_stage": "new"
    }).execute()

    new_lead_id = new_lead.data[0]["lead_id"]

    # log the import as a first activity
    supabase.table("lead_activities").insert({
        "lead_id": new_lead_id,
        "performing_agent": contact["agent_id"],
        "type": "note",
        "content": "Imported from phone contacts"
    }).execute()

    # mark the pending contact as imported
    supabase.table("pending_contacts").update({"status": "imported"}).eq("pending_id", request.pending_id).execute()

    return {"status": "imported"}

@app.post("/parse-csv")
async def parse_csv(file: UploadFile = File(...)):
    contents = await file.read()
    text = contents.decode("utf-8", errors="ignore")

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        return {"headers": [], "sample_rows": [], "total_rows": 0}

    headers = rows[0]
    data_rows = rows[1:]

    # grab a few non-empty sample rows for context
    sample_rows = []
    for row in data_rows:
        if any(cell.strip() for cell in row):  # skip fully blank rows
            sample_rows.append(row)
        if len(sample_rows) >= 3:
            break

    return {
        "headers": headers,
        "sample_rows": sample_rows,
        "total_rows": len(data_rows),
    }

@app.post("/map-columns")
async def map_columns(payload: dict):
    headers = payload["headers"]
    sample_rows = payload["sample_rows"]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    prompt = f"""You are helping map spreadsheet columns to a CRM's fields.

The CRM needs these fields: first_name, last_name, email, phone, birthday

Here are the spreadsheet's column headers:
{json.dumps(headers)}

Here are a few sample rows (as arrays matching the headers order):
{json.dumps(sample_rows)}

Some headers may be blank or unclear — use the sample values to infer what each column contains.
Note that names may be in "Last, First" format, "First Last" format, or a single name. 

Return ONLY a JSON object (no markdown, no explanation) with this structure:
{{
  "mapping": {{ "first_name": <header or null>, "last_name": <header or null>, "email": <header or null>, "phone": <header or null>, "birthday": <header or null> }},
  "name_format": "last_first" | "first_last" | "single" | "unknown"
}}

For each CRM field, give the exact header string from the spreadsheet that best matches, or null if no column matches. If one column contains the full name, map it to first_name and indicate the name_format."""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # strip code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1].replace("json", "", 1).strip()

    mapping = json.loads(raw)
    return mapping

def split_name(full_name: str, name_format: str):
    full_name = full_name.strip()
    if name_format == "last_first" and "," in full_name:
        parts = full_name.split(",", 1)
        return parts[1].strip(), parts[0].strip()   # first, last
    elif name_format == "first_last":
        parts = full_name.split(" ", 1)
        return parts[0], parts[1] if len(parts) > 1 else ""
    else:  # single or unknown
        return full_name, ""
    
def clean_phone(value: str):
    if not value:
        return ""
    digits = re.sub(r'\D', '', value)   # strip everything but digits
    # a real phone has 10-11 digits (US); reject things with too few
    if len(digits) < 10:
        return ""
    return value.strip()

@app.post("/process-csv")
async def process_csv(
    agent_id: str,
    mapping: str = Form(...),
    name_format: str = Form(...),
    file: UploadFile = File(...),
):
    # mapping comes in as a JSON string in a form field — parse it
    mapping_dict = json.loads(mapping)

    contents = await file.read()
    text = contents.decode("utf-8", errors="ignore")
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        return {"count": 0}

    headers = rows[0]
    data_rows = rows[1:]

    # helper: given a row and a Kindrasa field, return that cell's value
    def cell_for(row, field):
        col = mapping_dict.get(field)
        if not col or col not in headers:
            return ""
        idx = headers.index(col)
        return row[idx].strip() if idx < len(row) else ""

    contacts = []
    for row in data_rows:
        if not any(c.strip() for c in row):   # skip blank rows
            continue

        raw_name = cell_for(row, "first_name")
        if not raw_name:                       # skip rows with no name
            continue

        first, last = split_name(raw_name, name_format)

        contact = {
            "agent_id": agent_id,
            "status": "pending",
            "name": f"{first} {last}".strip(),  # pending_contacts stores a single name
            "email": cell_for(row, "email"),
            "phone": clean_phone(cell_for(row, "phone")),
            "birthday": cell_for(row, "birthday") or None,
        }
        contacts.append(contact)

    if contacts:
        supabase.table("pending_contacts").insert(contacts).execute()

    return {"count": len(contacts)}