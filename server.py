import os
import httpx
from fastmcp import FastMCP
from starlette.responses import JSONResponse

# Имя сервера, которое будет видно в ChatGPT
mcp = FastMCP(
    name="AI Organiser MCP",
    instructions="""
This server exposes a single tool: ai_organiser_save.

GOAL:
- Save ChatGPT responses into the user's AI Organiser account.

AUTH MODEL:
- Each user has their own integration token issued by AI Organiser.
- This token is NOT typed in chat.
- For now, this server uses a single integration token from environment
  variable AI_ORGANISER_INTEGRATION_TOKEN.
- NEVER ask the user to type their token in messages.

WHEN TO CALL:
- Only call ai_organiser_save when the user clearly asks to save something, e.g.:
  - "сохрани это"
  - "сохрани в библиотеку"
  - "сохрани в проект <name>"
  - "save this to the library"
- If the user did not mention saving, DO NOT call this tool.

WHAT TO SAVE:
- When the user says "сохрани это" / "save this":
  - Use the content of YOUR PREVIOUS ASSISTANT MESSAGE as `body`,
    unless the user explicitly points to another text.
- Do NOT save the user's request ("Составь план..."), save your answer.

TURN ORDER:
- First: answer normally.
- Only in a FOLLOW-UP user message like "сохрани это..." you may call this tool.
- Never call ai_organiser_save in the same turn where you generate the content.
""",
)

# ---------- OAuth protected resource metadata ----------

# ВАЖНО: resource теперь БЕЗ /mcp
RESOURCE_URL = os.getenv(
    "MCP_RESOURCE_URL",
    "https://ai-organiser-mcp-1.onrender.com",
)

OAUTH_AUTH_SERVER = os.getenv(
    "MCP_OAUTH_AUTH_SERVER",
    "https://llm-wisdom-vault.lovable.app",
)

OAUTH_SCOPES = ["notes:write"]


def _protected_resource_metadata() -> dict:
    return {
        "resource": RESOURCE_URL,
        "authorization_servers": [OAUTH_AUTH_SERVER],
        "scopes_supported": OAUTH_SCOPES,
        "resource_documentation": "https://ai-organiser.app/docs/chatgpt",
    }


# Вариант 1: корень (https://host/.well-known/...)
@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource_root(request):
    return JSONResponse(_protected_resource_metadata())


# Вариант 2: с префиксом /mcp (https://host/mcp/.well-known/...)
@mcp.custom_route("/mcp/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource_with_prefix(request):
    return JSONResponse(_protected_resource_metadata())


# ---------- Supabase edge function настройки ----------

SUPABASE_FUNCTION_URL = "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/quick-add"

SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyem93c2Z3dXJndGNkeGp3ZXZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxMDUyMTYsImV4cCI6MjA3ODY4MTIxNn0."
    "0l394mJ9cLNN_QxNl9DKzdw1ni_-SBawGzoSrchNcJI"
)

INTEGRATION_TOKEN_ENV_VAR = "AI_ORGANISER_INTEGRATION_TOKEN"


@mcp.tool
def ai_organiser_save(
    body: str,
    project_name: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    - integration token берётся из переменной окружения AI_ORGANISER_INTEGRATION_TOKEN
      на сервере (Render).
    - Если project_name is None -> сохраняем в Inbox (не отправляем поле 'project').
    - Если project_name задан  -> отправляем его в поле 'project'.
    """

    if not SUPABASE_ANON_KEY:
        return {
            "saved": False,
            "error": "SUPABASE_ANON_KEY is not configured.",
        }

    integration_token = os.getenv(INTEGRATION_TOKEN_ENV_VAR)

    if not integration_token:
        return {
            "saved": False,
            "error": (
                "AI_ORGANISER_INTEGRATION_TOKEN is not set on the server. "
                "Set it in Render → Environment."
            ),
        }

    payload = {
        "text": body,
        "sourceUrl": None,
        "sourceTitle": None,
    }

    if project_name:
        payload["project"] = project_name

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        "x-api-key": integration_token,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(SUPABASE_FUNCTION_URL, json=payload, headers=headers)

        if res.status_code >= 400:
            try:
                data = res.json()
            except Exception:
                data = res.text

            return {
                "saved": False,
                "status_code": res.status_code,
                "error": f"Supabase returned {res.status_code}",
                "response": data,
            }

        try:
            data = res.json()
        except Exception:
            data = res.text

        return {
            "saved": True,
            "project_name": project_name or "Inbox",
            "title": title,
            "body_preview": body[:160],
            "supabase_response": data,
        }

    except Exception as e:
        return {
            "saved": False,
            "error": f"Exception while calling Supabase: {e}",
        }


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")

    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
    )
