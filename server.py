import os
import httpx
from fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from contextvars import ContextVar

# ==============================
# Глобальный контекст для Authorization
# ==============================

_current_auth_header: ContextVar[str | None] = ContextVar(
    "current_auth_header", default=None
)

# Имя переменной окружения для single-user fallback
INTEGRATION_TOKEN_ENV_VAR = "AI_ORGANISER_INTEGRATION_TOKEN"


class CaptureAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware, который перехватывает заголовок Authorization
    у всех HTTP-запросов к MCP-серверу и кладёт его в contextvar.
    Это позволяет внутри @mcp.tool узнать Bearer-токен,
    который ChatGPT прислал после OAuth.
    """

    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization")
        if auth:
            # Не логируем сам токен, только факт наличия
            _current_auth_header.set(auth)
        else:
            _current_auth_header.set(None)

        response = await call_next(request)
        return response


def get_integration_token() -> str | None:
    """
    Пытается достать токен в следующем порядке:

    1) Authorization: Bearer <access_token> из текущего HTTP-запроса к MCP.
       - access_token выдаётся Supabase oauth-token
       - по нашей договорённости access_token == profiles.integration_token

    2) Если Bearer-токена нет / формат неверный —
       fallback к env-переменной AI_ORGANISER_INTEGRATION_TOKEN
       (как было в single-user режиме).
    """

    # 1) Пробуем взять Authorization: Bearer <token> из contextvar
    auth = _current_auth_header.get()
    token_from_header: str | None = None

    if auth and isinstance(auth, str):
        # Ожидаем формат: "Bearer <token>"
        parts = auth.split(" ", 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            candidate = parts[1].strip()
            if candidate:
                token_from_header = candidate

    if token_from_header:
        # Не логируем сам токен, только то, что он был использован
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
This server exposes a single tool: ai_organiser_save.

GOAL:
- Save ChatGPT responses into the user's AI Organiser account.

AUTH MODEL:
- Each user has their own integration token issued by AI Organiser.
- This token is NOT typed in chat.
- After OAuth:
  - ChatGPT sends Authorization: Bearer <access_token> to this MCP server.
  - The access_token is equal to the user's integration_token in AI Organiser.
- If no bearer token is present, the server falls back to a single integration token from environment
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

# Попробуем повесить middleware на внутренний Starlette-приложение.
# Если по какой-то причине это не удастся — single-user fallback всё равно будет работать.
try:
    if hasattr(mcp, "app") and mcp.app is not None:
        mcp.app.add_middleware(CaptureAuthMiddleware)
        print("CaptureAuthMiddleware attached to MCP app", flush=True)
    else:
        print("WARNING: MCP app not available for middleware attachment", flush=True)
except Exception as e:
    print(
        f"WARNING: failed to add CaptureAuthMiddleware: {e!r}",
        flush=True,
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
            "error": "SUPABASE_ANON_KEY is not configured.",
        }

    integration_token = get_integration_token()

    if not integration_token:
        return {
            "saved": False,
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

    print("Starting AI Organiser MCP server...", flush=True)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
    )
