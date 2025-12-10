import os
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from starlette.responses import JSONResponse

# ==============================
# Имя env-переменной для single-user fallback
# ==============================

INTEGRATION_TOKEN_ENV_VAR = "AI_ORGANISER_INTEGRATION_TOKEN"


def get_integration_token() -> str | None:
    """
    Пытается достать токен в следующем порядке:

    1) Authorization: Bearer <access_token> из текущего HTTP-запроса к MCP.
       - access_token выдаётся Supabase oauth-token
       - по договорённости access_token == profiles.integration_token

    2) Если Bearer-токена нет / формат неверный —
       fallback к env-переменной AI_ORGANISER_INTEGRATION_TOKEN
       (single-user режим).
    """

    # 1) Пробуем взять Authorization: Bearer <token> из HTTP-заголовков
    headers = get_http_headers()  # вернёт {} если контекста запроса нет
    auth = headers.get("authorization")

    token_from_header: str | None = None

    if auth and isinstance(auth, str):
        # Ожидаем формат: "Bearer <token>"
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            candidate = parts[1].strip()
            if candidate:
                token_from_header = candidate

    if token_from_header:
        # Не логируем сам токен, только факт использования
        print(
            "ai_organiser_save: using bearer access token from Authorization header",
            flush=True,
        )
        return token_from_header

    # 2) Fallback: env-переменная
    env_token = os.getenv(INTEGRATION_TOKEN_ENV_VAR)
    if env_token:
        print(
            "ai_organiser_save: using env AI_ORGANISER_INTEGRATION_TOKEN fallback",
            flush=True,
        )
        return env_token

    # Вообще ничего нет — вернём None
    print(
        "ai_organiser_save: NO bearer token and NO env AI_ORGANISER_INTEGRATION_TOKEN",
        flush=True,
    )
    return None


# ==============================
# FastMCP сервер
# ==============================

mcp = FastMCP(
    name="AI Organiser MCP",
    instructions="""
You are connected to the user's AI Organiser account via this MCP server.

You have ONE tool: ai_organiser_save(body, project_name, title).

WHEN TO CALL THE TOOL (CRITICAL)
- If the user (in Russian or English) clearly asks to save something, you MUST call ai_organiser_save.
- Trigger words/phrases include (but are not limited to):
  - "сохрани"
  - "сохрани это"
  - "сохрани в проект <имя>"
  - "сохрани в библиотеку"
  - "save"
  - "save this"
  - "save to project <name>"
  - "save to the library"
- If the message contains "сохрани" as a verb referring to your previous answer, you SHOULD call the tool.
- Do NOT answer things like "I cannot save to external storage" — you CAN save using this tool.

WHAT "THIS" MEANS
- Phrases like "сохрани это" / "save this" almost always refer to YOUR PREVIOUS ASSISTANT MESSAGE,
  unless the user explicitly points to a different text.

HOW TO FILL ARGUMENTS (KEEP IT SIMPLE)
- body:
    - Just pass the full text that should be saved. Usually: your previous answer, verbatim.
    - It's OK if the text has newlines and quotes. You DO NOT need to compress it or manually escape anything.
    - You DON'T need to think about JSON escaping: the tool runner will handle encoding.
- project_name:
    - If the user said "в проект <имя>" / "to project <name>" — use that name as a plain string.
    - Otherwise pass null (the server will save to Inbox).
- title:
    - Optional. If easy, generate a very short title (3–8 words) summarizing the content.
    - If you are unsure or it slows you down, you MAY pass null.

ERROR HANDLING (IMPORTANT FOR YOU AS THE MODEL)
- The tool returns a JSON object with at least:
    - saved: boolean
    - error_type: "none" | "auth_error" | "backend_error"
    - message_for_model: short English instruction for how you should explain the situation to the user.
- When saved == False:
    - You SHOULD follow message_for_model.
    - You SHOULD NOT invent that "the system globally forbids saving to external apps".
    - Prefer concrete, honest explanations like:
        - asking the user to reconnect AI Organiser;
        - suggesting to copy text manually if the backend failed.

TOOL CALL POLICY
- First, answer the user normally if they asked you to create or explain something.
- If in a FOLLOW-UP message they ask to save it (using the trigger words above),
  you MUST call ai_organiser_save in addition to your chat response.

AUTH MODEL
- Each user has their own integration token issued by AI Organiser.
- This token is NOT typed in chat.
- After OAuth:
  - ChatGPT sends Authorization: Bearer <access_token> to this MCP server.
  - The access_token is equal to the user's integration_token in AI Organiser.
- If no bearer token is present, the server falls back to a single integration token from
  environment variable AI_ORGANISER_INTEGRATION_TOKEN.
- NEVER ask the user to type their token in messages.
""",
)

# ==============================
# OAuth protected resource metadata (для ChatGPT MCP)
# ==============================

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
    meta = {
        "resource": RESOURCE_URL,
        "authorization_servers": [OAUTH_AUTH_SERVER],
        "scopes_supported": OAUTH_SCOPES,
        "resource_documentation": "https://ai-organiser.app/docs/chatgpt",
    }
    print("OAUTH META GENERATED (protected-resource):", meta, flush=True)
    return meta


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource_root(request):
    print(
        "OAUTH META HIT (protected root):",
        request.method,
        str(request.url),
        "UA=",
        request.headers.get("user-agent"),
        flush=True,
    )
    return JSONResponse(_protected_resource_metadata())


@mcp.custom_route("/mcp/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource_with_prefix(request):
    print(
        "OAUTH META HIT (protected /mcp):",
        request.method,
        str(request.url),
        "UA=",
        request.headers.get("user-agent"),
        flush=True,
    )
    return JSONResponse(_protected_resource_metadata())


# ==============================
# OAuth authorization server metadata (ChatGPT тоже ищет их на MCP)
# ==============================

AUTH_SERVER_METADATA = {
    "issuer": "https://llm-wisdom-vault.lovable.app",
    "authorization_endpoint": "https://llm-wisdom-vault.lovable.app/chatgpt/oauth",
    "token_endpoint": "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/oauth-token",
    "registration_endpoint": "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/oauth-register",
    "grant_types_supported": ["authorization_code"],
    "response_types_supported": ["code"],
    "code_challenge_methods_supported": ["S256"],
    "token_endpoint_auth_methods_supported": ["none"],
    "scopes_supported": ["notes:write"],
    "debug_version": "mcp-auth-meta-v1",
}


def _auth_server_metadata() -> dict:
    print("OAUTH META GENERATED (auth-server):", AUTH_SERVER_METADATA, flush=True)
    return AUTH_SERVER_METADATA


@mcp.custom_route("/.well-known/oauth-authorization-server/mcp", methods=["GET"])
async def oauth_auth_server_suffix_mcp(request):
    print(
        "OAUTH AUTH META HIT (/.well-known/.../mcp):",
        request.method,
        str(request.url),
        "UA=",
        request.headers.get("user-agent"),
        flush=True,
    )
    return JSONResponse(_auth_server_metadata())


@mcp.custom_route("/mcp/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_auth_server_with_prefix(request):
    print(
        "OAUTH AUTH META HIT (/mcp/.well-known/...):",
        request.method,
        str(request.url),
        "UA=",
        request.headers.get("user-agent"),
        flush=True,
    )
    return JSONResponse(_auth_server_metadata())


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def oauth_auth_server_root(request):
    print(
        "OAUTH AUTH META HIT (/.well-known/...):",
        request.method,
        str(request.url),
        "UA=",
        request.headers.get("user-agent"),
        flush=True,
    )
    return JSONResponse(_auth_server_metadata())


# ==============================
# Supabase quick-add настройки
# ==============================

SUPABASE_FUNCTION_URL = (
    "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/quick-add"
)

SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyem93c2Z3dXJndGNkeGp3ZXZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxMDUyMTYsImV4cCI6MjA3ODY4MTIxNn0."
    "0l394mJ9cLNN_QxNl9DKzdw1ni_-SBawGzoSrchNcJI"
)


@mcp.tool
def ai_organiser_save(
    body: str,
    project_name: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    WHEN THE MODEL SHOULD USE THIS TOOL:
    - Whenever the user asks to "save" content in Russian or English, for example:
      - "сохрани"
      - "сохрани это"
      - "сохрани в проект <имя>"
      - "сохрани в библиотеку"
      - "save this"
      - "save to project <name>"

    ARGUMENTS:
    - body:
        - the text that should be stored (usually the previous assistant reply), passed verbatim;
        - it may contain multiple lines and quotes, no need to compress or escape it manually.
    - project_name:
        - if the user mentioned a project name, use it;
        - otherwise leave as null to save to Inbox.
    - title:
        - optional short human-readable summary of the note;
        - may be null if generating a title is inconvenient.

    ERROR CONTRACT:
    - On success:
        - returns: { "saved": True, "error_type": "none", ... }
    - On failure:
        - returns: { "saved": False, "error_type": "auth_error" | "backend_error",
                     "message_for_model": <instruction for how to explain this to the user>,
                     ... }
        - The model SHOULD:
            - follow message_for_model when talking to the user;
            - NOT claim that ChatGPT is globally forbidden to save to external tools.

    Token resolution order:
    1) Try Authorization: Bearer <access_token> from the current MCP HTTP request.
       - access_token is equal to the user's integration_token returned by oauth-token.
    2) If no bearer token is present, fall back to AI_ORGANISER_INTEGRATION_TOKEN
       from environment (single-user mode).

    - If project_name is None -> save to Inbox (do not send 'project' field).
    - If project_name is provided -> send it as 'project'.
    """

    print("ai_organiser_save CALLED; project_name =", project_name, flush=True)

    if not SUPABASE_ANON_KEY:
        return {
            "saved": False,
            "error_type": "backend_error",
            "message_for_model": (
                "The AI Organiser backend is misconfigured on the server "
                "(missing Supabase anon key). Tell the user that saving "
                "to AI Organiser is temporarily unavailable and that they "
                "should copy the content manually."
            ),
            "error": "SUPABASE_ANON_KEY is not configured.",
        }

    integration_token = get_integration_token()

    if not integration_token:
        # Здесь логичнее было бы вернуть 401, но в рамках tools/call
        # мы просто даём понятную инструкцию модели.
        return {
            "saved": False,
            "error_type": "auth_error",
            "message_for_model": (
                "There is no valid integration token for AI Organiser in this request. "
                "Ask the user to connect or reconnect their AI Organiser account via "
                "the app's OAuth / account linking flow, then try saving again. "
                "Also remind them that the full content is still available in this chat."
            ),
            "error": (
                "No integration token available: neither bearer token from Authorization "
                "header nor AI_ORGANISER_INTEGRATION_TOKEN env is set."
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

            # Логируем для себя, но не заставляем модель говорить "403"
            print("Supabase quick-add error:", res.status_code, data, flush=True)

            error_type = "backend_error"
            message_for_model = (
                "The AI Organiser backend returned an error while saving the note. "
                "Tell the user that saving to AI Organiser failed this time, but they "
                "still have the full content in the chat and can copy it manually. "
                "You SHOULD NOT say that ChatGPT is globally forbidden to save to "
                "external tools."
            )

            if res.status_code in (401, 403):
                error_type = "auth_error"
                message_for_model = (
                    "AI Organiser reported that the access token or integration key is "
                    "invalid or no longer accepted. Ask the user to reconnect their "
                    "AI Organiser account via the app's OAuth / account linking flow "
                    "and then try saving again. Also remind them that the full content "
                    "is still available in this chat."
                )

            return {
                "saved": False,
                "error_type": error_type,
                "message_for_model": message_for_model,
                "supabase_status": res.status_code,
                "supabase_response": data,
            }

        try:
            data = res.json()
        except Exception:
            data = res.text

        return {
            "saved": True,
            "error_type": "none",
            "project_name": project_name or "Inbox",
            "title": title,
            "body_preview": body[:160],
            "supabase_response": data,
        }

    except Exception as e:
        # Сетевые/прочие исключения
        print("Exception while calling Supabase:", repr(e), flush=True)
        return {
            "saved": False,
            "error_type": "backend_error",
            "message_for_model": (
                "There was a network or backend error while calling AI Organiser. "
                "Tell the user that saving failed due to a temporary technical issue, "
                "and suggest copying the content manually so it is not lost."
            ),
            "error": f"Exception while calling Supabase: {e}",
        }


if __name__ == "__main__":
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")

    print("Starting AI Organiser MCP server...", flush=True)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
    )
