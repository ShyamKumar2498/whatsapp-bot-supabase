from fastapi import FastAPI, Request, Form
from fastapi.responses import PlainTextResponse
import httpx, os, uuid
import traceback

# Only needed locally (not on Railway)
if os.getenv("RAILWAY_ENV") is None:
    from dotenv import load_dotenv
    load_dotenv()

app = FastAPI()

# === ENV VARS ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_EMAIL = os.getenv("SUPABASE_EMAIL")
SUPABASE_PASSWORD = os.getenv("SUPABASE_PASSWORD")
SUPABASE_CHAT_API = os.getenv("SUPABASE_CHAT_API")
PUBLIC_SUPABASE_ANON_KEY = os.getenv("PUBLIC_SUPABASE_ANON_KEY")
CHAT_ID = os.getenv("CHAT_ID")

required_envs = [SUPABASE_URL, SUPABASE_EMAIL, SUPABASE_PASSWORD, SUPABASE_CHAT_API]
if not all(required_envs):
    raise RuntimeError("❌ Missing one or more required Supabase environment variables.")

SUPABASE_AUTH_URL = f"{SUPABASE_URL}/auth/v1/token?grant_type=password"

# In-memory suggestion cache per user (ephemeral)
user_suggestion_cache = {}

# === UTILS ===
def sanitize_whatsapp_markdown(text: str) -> str:
    text = text.replace("•", "-")
    text = text.replace("**", "*")
    text = text.replace("__", "_")
    text = text.replace("```", "")  # WhatsApp doesn't render code blocks well
    return text.strip()

# === ROUTES ===

@app.get("/")
def root():
    return {"status": "online", "message": "WhatsApp + Supabase GPT is running 🚀"}

@app.post("/twilio-webhook")
async def receive_whatsapp_message(
    request: Request,
    Body: str = Form(...),
    From: str = Form(...)
):
    print(f"📩 Message from {From}: {Body}")
    chat_id = str(uuid.uuid4())

    # Check if user is clicking a suggestion
    suggestions = user_suggestion_cache.get(From)
    if Body.strip().isdigit() and suggestions:
        index = int(Body.strip()) - 1
        if 0 <= index < len(suggestions):
            Body = suggestions[index]
            print(f"🔁 Mapped suggestion #{index + 1} to: {Body}")

    try:
        # 1. Get Supabase access_token
        async with httpx.AsyncClient() as client:
            auth_response = await client.post(
                SUPABASE_AUTH_URL,
                headers={"Content-Type": "application/json", "apikey": PUBLIC_SUPABASE_ANON_KEY},
                json={"email": SUPABASE_EMAIL, "password": SUPABASE_PASSWORD}
            )
            auth_response.raise_for_status()
            token = auth_response.json().get("access_token")
            if not token:
                raise ValueError("No access_token returned.")

        # 2. Send question to chat endpoint
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SUPABASE_CHAT_API,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json"
                },
                json={
                    "query": Body,
                    "knowledge_base": "Aura Agent",
                    "chat": CHAT_ID
                },
                timeout=30  # Railway allows longer, adjust as needed
            )

        if response.status_code != 200:
            print("❌ Chat API Error:", response.status_code)
            return PlainTextResponse("Something went wrong with the agent response.", media_type="text/plain")

        raw = await response.aread()
        decoded = raw.decode()

        thinking_msgs = []
        final_answer = ""
        suggestions = []

        for line in decoded.splitlines():
            if line.startswith("data: "):
                data = line.replace("data: ", "").strip()
                if data == "[DONE]":
                    break
                try:
                    parsed = httpx.Response(200, content=data).json()
                    content = parsed.get("content", "")
                    if "<thinking>" in content:
                        clean = content.replace("<thinking>", "").replace("</thinking>", "").strip()
                        thinking_msgs.append(clean)
                    elif content:
                        final_answer += content.strip() + "\n"
                    if "final" in parsed:
                        suggestions.extend(parsed["final"])
                except Exception as e:
                    print("⚠️ Parse chunk error:", e)
                    continue

        # 3. Format WhatsApp markdown response
        parts = []

        if thinking_msgs:
            parts.append("*🧠 Thinking*")
            parts.extend([sanitize_whatsapp_markdown(m) for m in thinking_msgs])

        if final_answer:
            parts.append("*💬 Aurora Agent Answer*")
            parts.append(sanitize_whatsapp_markdown(final_answer))

        if suggestions:
            parts.append("*💡 Suggested Questions:*")
            for i, q in enumerate(suggestions, 1):
                parts.append(f"{i}. {q}")
            user_suggestion_cache[From] = suggestions

        markdown = "\n\n".join(parts)
        return PlainTextResponse(markdown, media_type="text/plain")

    except Exception as e:
        print("💥 Supabase Error:", e)
        traceback.print_exc()
        return PlainTextResponse("Sorry, something went wrong on our side.", media_type="text/plain")

# For local dev
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000)
