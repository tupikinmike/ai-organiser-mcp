import os
import time
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

Your goal:
- Quickly and reliably save content (usually your previous answer) into AI Organiser
  when the user explicitly asks you to.

There are TWO kinds of triggers:
1) Natural language commands (e.g. "сохрани это").
2) Short command prefixes at the start of the message: "@last" and "@short".

========================================
TRIGGERS: WHEN TO CALL ai_organiser_save
========================================

You MUST call ai_organiser_save when the user clearly asks to save content.

A. NATURAL LANGUAGE TRIGGERS (Russian / English)

Treat these as clear triggers (examples, not a full list):

- "сохрани"
- "сохрани это"
- "сохрани в проект <имя>"
- "сохрани в библиотеку"
- "сохрани конспект"
- "сделай конспект и сохрани"
- "save"
- "save this"
- "save to project <name>"
- "save to the library"
- "save a summary"
- "save the summary"

If the user uses "сохрани" / "save" referring to your previous answer, DO NOT ask for confirmation.
Just call the tool with appropriate arguments.

By default:
- "сохрани это" / "save this" → save FULL previous answer.
- "сохрани конспект" / "save a summary" → save a structured summary (see SUMMARY MODE).

B. SHORT COMMAND PREFIXES: "@last" AND "@short"

If the user message STARTS with "@last" or "@short" (case-insensitive: "@last", "@LAST", "@Last"):

1) "@last" — save the FULL last assistant answer

Format:
- "@last"
    → save your previous assistant message as body, to Inbox.
- "@last <project_name>"
    → save previous assistant message as body, to project "<project_name>".

You SHOULD treat everything after the first space as the project_name (if present).
Examples:
- "@last" → body = previous answer, project_name = null.
- "@last Учёба" → body = previous answer, project_name = "Учёба".
- "@last Pet projects" → body = previous answer, project_name = "Pet projects".

2) "@short" — save a SUMMARY of the last assistant answer

Format:
- "@short"
    → build a summary (see SUMMARY MODE below) and save to Inbox.
- "@short <project_name>"
    → build a summary and save to project "<project_name>".

You SHOULD treat everything after the first space as the project_name (if present).
Examples:
- "@short" → summary, project_name = null (Inbox).
- "@short Пет-проекты" → summary, project_name = "Пет-проекты".
- "@short Study" → summary, project_name = "Study".

If the message starts with "@last" / "@short", you MUST:
- treat it as a direct command to save,
- NOT ask clarifying questions,
- call ai_organiser_save accordingly.

========================================
WHAT TO SAVE
========================================

DEFAULT: FULL TEXT MODE
-----------------------
Triggered by:
- natural language commands like "сохрани это" / "save this",
- OR "@last [...]" prefix.

Behavior:
- body = your PREVIOUS ASSISTANT MESSAGE verbatim.
- Do NOT rephrase or shorten it.
- Assume "это" / "this" refers to your last answer unless the user explicitly points to another text.

EXPLICIT SUMMARY MODE
---------------------
Triggered by:
- natural language like:
  - "сохрани конспект"
  - "сделай конспект и сохрани"
  - "save a summary"
  - "save the summary"
- OR a "@short [...]" prefix.

In SUMMARY MODE you MUST:

1) Take your own previous long answer as the source.

2) Create a SHORT SUMMARY of that answer:
   - About 1–2 screens of text (roughly up to ~1500–2000 characters).
   - Use clear structure: bullet points, чеклист, план действий.
   - It should be компактным, но полезным как отдельная шпаргалка.

3) Build body in the following STRUCTURE (strict):

   Line 1:
   [КОНСПЕКТ, НЕ ПОЛНЫЙ ТЕКСТ]

   Next lines (link to the original long answer in ChatGPT):
   Где искать полный текст:
   Открой ChatGPT и найди диалог по этой фразе (начало оригинального ответа):
   "<первые ~150–200 символов твоего полного ОРИГИНАЛЬНОГО ответа, ДОСЛОВНО>"

   Then:
   ---
   Краткий конспект:

   <твой сжатый конспект, чеклист или план>

Important:
- The quoted “original beginning” (first 150–200 characters) MUST be copied exactly
  from the full original answer (no paraphrasing). The user can search by this phrase
  in ChatGPT to locate the original long message.
- Do NOT invent or fabricate any direct URL to the chat. You do NOT know a real
  shareable link. Only explain that the full text is in ChatGPT and can be found
  by that quoted beginning.

Summary:
- "сохрани это" / "save this" / "@last [...]" → save FULL previous answer as body.
- "сохрани конспект" / "save a summary" / "@short [...]" → build SUMMARY MODE body and save that.

========================================
HOW TO FILL ARGUMENTS
========================================

body:
- The text to save:
  - either the full previous answer (full-text mode),
  - or the structured summary described above (summary mode).
- You DON'T need to escape JSON manually.

project_name:
- NATURAL LANGUAGE:
  - If the user wrote "в проект <имя>" / "to project <name>", pass that <name> as project_name.
  - Otherwise pass null (the server will save to Inbox).
- SHORT PREFIXES:
  - If message starts with "@last" / "@short" and contains text after the first space:
    - use that trailing text as project_name (trimmed).
  - If there is no trailing text after "@last"/"@short" → project_name = null (Inbox).

title:
- Very short 3–8 word description of what this note is about, if easy:
  - e.g. "План пет-проекта", "Домашняя тренировка", "React study plan".
- If it's hard to pick a title, you MAY pass null.

========================================
ERROR HANDLING (FOR YOU AS THE MODEL)
========================================

The tool returns JSON with at least:
- saved: boolean
- error_type: "none" | "auth_error" | "backend_error"
- message_for_model: a short English instruction for how you should explain the situation
  to the user.
- body_length: integer (if available)

If saved == False:
- Follow message_for_model in your explanation to the user.
- DO NOT claim things like:
  - "the system blocks such requests for safety reasons"
  - "saving to external apps is globally disabled"
- Prefer concrete, honest messages:
  - ask the user to reconnect AI Organiser (for auth_error),
  - or say there was a temporary technical issue and suggest copying the text manually.

========================================
TOOL CALL POLICY
========================================

- Step 1: Answer the user’s original request normally (создать план, рецепт, объяснение).
- Step 2: If in a FOLLOW-UP message the user:
    - uses natural language triggers ("сохрани это", "сохрани конспект", "save this", "save a summary"),
      OR
    - starts the message with "@last" / "@short",
  you MUST call ai_organiser_save IN ADDITION to your chat reply.

- If the user explicitly asks for a summary to be saved ("сохрани конспект" / "save a summary"
  or a "@short [...]" prefix), you MUST:
    - build the structured summary body (with [КОНСПЕКТ...] and original beginning),
    - call ai_organiser_save with that summary as body.

========================================
AUTH MODEL (for your awareness, no action needed)
========================================

- Each user has their own integration token issued by AI Organiser.
- The user never types this token in chat.
- After OAuth, ChatGPT sends Authorization: Bearer <access_token> to this MCP server.
- This access_token equals the user's integration_token in AI Organiser.
- If no bearer token is present, the server falls back to a single integration token from
  environment variable AI_ORGANISER_INTEGRATION_TOKEN.
- NEVER ask the user to type any tokens or secrets in messages.
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

# Глобальный httpx-клиент для переиспользования соединений
supabase_client = httpx.Client(timeout=10.0)


@mcp.tool
def ai_organiser_save(
    body: str,
    project_name: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    Token resolution order:
    1) Try Authorization: Bearer <access_token> from the current MCP HTTP request.
       - access_token is equal to the user's integration_token returned by oauth-token.
    2) If no bearer token is present, fall back to AI_ORGANISER_INTEGRATION_TOKEN
       from environment (single-user mode).

    - If project_name is None -> save to Inbox (do not send 'project' field).
    - If project_name is provided -> send it as 'project'.
    """

    tool_start = time.monotonic()
    print("ai_organiser_save CALLED; project_name =", project_name, flush=True)

    # Диагностика размера body (для логов и отладки)
    body_length = len(body) if isinstance(body, str) else None
    if body_length is not None:
        preview_start = body[:80].replace("\n", "\\n")
        preview_end = body[-80:].replace("\n", "\\n") if body_length > 80 else ""
        print(
            f"ai_organiser_save BODY length={body_length}, "
            f"preview_start='{preview_start}'"
            + (f", preview_end='{preview_end}'" if preview_end else ""),
            flush=True,
        )

    if not SUPABASE_ANON_KEY:
        total_elapsed = time.monotonic() - tool_start
        print(
            f"ai_organiser_save EARLY EXIT (no anon key), total {total_elapsed:.3f}s",
            flush=True,
        )
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
            "body_length": body_length,
        }

    integration_token = get_integration_token()

    if not integration_token:
        total_elapsed = time.monotonic() - tool_start
        print(
            f"ai_organiser_save EARLY EXIT (no integration token), total {total_elapsed:.3f}s",
            flush=True,
        )
        return {
            "saved": False,
            "error_type": "auth_error",
            "message_for_model": (
                "There is no valid integration token for AI Organiser in this request. "
                "Ask the user to connect or reconnect their AI Organiser account via "
                "the app's OAuth / account linking flow, then try again. "
                "Also remind them that the full content is still available in this chat."
            ),
            "error": (
                "No integration token available: neither bearer token from Authorization "
                "header nor AI_ORGANISER_INTEGRATION_TOKEN env is set."
            ),
            "body_length": body_length,
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
        supabase_start = time.monotonic()
        res = supabase_client.post(SUPABASE_FUNCTION_URL, json=payload, headers=headers)
        supabase_elapsed = time.monotonic() - supabase_start
        print(
            f"Supabase quick-add HTTP call took {supabase_elapsed:.3f}s "
            f"(status={res.status_code})",
            flush=True,
        )

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
                "You SHOULD NOT say that ChatGPT or the system is globally forbidden "
                "to save to external tools."
            )

            if res.status_code in (401, 403):
                error_type = "auth_error"
                message_for_model = (
                    "AI Organiser reported that the access token or integration key is "
                    "invalid or no longer accepted. Ask the user to reconnect their "
                    "AI Organiser account via the app's OAuth / account linking flow "
                    "and then try again. Also remind them that the full content "
                    "is still available in this chat."
                )

            total_elapsed = time.monotonic() - tool_start
            print(
                f"ai_organiser_save FINISHED with error, total {total_elapsed:.3f}s",
                flush=True,
            )

            return {
                "saved": False,
                "error_type": error_type,
                "message_for_model": message_for_model,
                "supabase_status": res.status_code,
                "supabase_response": data,
                "body_length": body_length,
            }

        try:
            data = res.json()
        except Exception:
            data = res.text

        total_elapsed = time.monotonic() - tool_start
        print(
            f"ai_organiser_save SUCCESS, total {total_elapsed:.3f}s",
            flush=True,
        )

        return {
            "saved": True,
            "error_type": "none",
            "project_name": project_name or "Inbox",
            "title": title,
            "body_length": body_length,
            "truncated": False,
            "supabase_response": data,
        }

    except Exception as e:
        # Сетевые/прочие исключения
        total_elapsed = time.monotonic() - tool_start
        print(
            f"Exception while calling Supabase: {repr(e)}, total {total_elapsed:.3f}s",
            flush=True,
        )
        return {
            "saved": False,
            "error_type": "backend_error",
            "message_for_model": (
                "There was a network or backend error while calling AI Organiser. "
                "Tell the user that saving failed due to a temporary technical issue, "
                "and suggest copying the content manually so it is not lost."
            ),
            "error": f"Exception while calling Supabase: {e}",
            "body_length": body_length,
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
