import os
import httpx

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request
from starlette.responses import JSONResponse

# Имя сервера, которое будет видно в ChatGPT
mcp = FastMCP(
    name="AI Organiser MCP",
    instructions="""
This server exposes a single tool: ai_organiser_save.

GOAL:
- Save ChatGPT responses into the user's AI Organiser account.

AUTH MODEL (CURRENT STATE):
- Each AI Organiser user has their own integration token.
- For now, this MCP server resolves a token in the following order:
  1) Authorization: Bearer <token> header (for future OAuth-based access tokens)
  2) x-ai-organiser-token header (custom header, if provided)
  3) Environment variable AI_ORGANISER_INTEGRATION_TOKEN (single-user fallback)
- The resolved token is forwarded to AI Organiser as the x-api-key header.
- NEVER ask the user to type their token in chat messages.

WHEN TO CALL:
- Only call ai_organiser_save when the user clearly asks to save something, e.g.:
  - "сохрани это"
  - "сохрани в библиотеку"
  - "сохрани в проект <name>"
  - "save this"
  - "save this to the library"
- If the user did not mention saving, DO NOT call this tool.

WHAT EXACTLY TO SAVE (VERY IMPORTANT):
- When the user says "сохрани это" / "save this" / "сохрани в проект <name>":
  - You MUST use the content of YOUR PREVIOUS ASSISTANT MESSAGE as `body`.
  - `body` MUST be an EXACT COPY of that assistant message:
    - do NOT summarize,
    - do NOT rephrase,
    - do NOT translate,
    - do NOT add any comments like "Пользователь попросил сохранить...",
    - do NOT prepend or append anything.
  - In other words: `body` must be byte-for-byte identical to the last
    assistant message that the user wants to save.

PROJECT HANDLING:
- If the user just says "сохрани это":
  - Call ai_organiser_save with `project_name = null` (or omit it).
  - The note will go to Inbox.
- If the user says "сохрани в проект <name>" or similar:
  - Pass `<name>` as `project_name`.
  - Do NOT change or normalize the name; send it as the user wrote it.
- Creation of the project (if it does not exist) is handled by the backend.

TURN ORDER:
- First: answer the main question normally (without calling the tool).
- Only in a FOLLOW-UP user message like "сохрани это..." you may call this tool.
- Never call ai_organiser_save in the same turn where you generate the content.

SECURITY / PRIVACY:
- Never ask the user to paste or reveal their AI Organiser integration token.
- Assume the token is provided via connector configuration / headers / environment only.
- Do not echo tokens in tool outputs, logs or error messages.
""",
)

# Настройки Supabase edge function (общие для всех пользователей)
SUPABASE_FUNCTION_URL = "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/quick-add"

# anon key Supabase (общий публичный ключ проекта)
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyem93c2Z3dXJndGNkeGp3ZXZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxMDUyMTYsImV4cCI6MjA3ODY4MTIxNn0."
    "0l394mJ9cLNN_QxNl9DKzdw1ni_-SBawGzoSrchNcJI"
)

# Имя переменной окружения, где лежит integration token аккаунта AI Organiser
INTEGRATION_TOKEN_ENV_VAR = "AI_ORGANISER_INTEGRATION_TOKEN"

# ТЕПЕРЬ: в качестве OAuth authorization server используем Supabase-проект
AUTH_BASE_URL = "https://trzowsfwurgtcdxjwevi.supabase.co"


def _resolve_integration_token() -> str | None:
    """
    Определяем, какой токен использовать для этого запроса.

    Текущая логика (до полноценного OAuth):
      1) Authorization: Bearer <token>  -> используем <token> как integration_token
      2) x-ai-organiser-token           -> используем значение как integration_token
      3) переменная окружения AI_ORGANISER_INTEGRATION_TOKEN (single-user fallback)
    """
    token: str | None = None

    try:
        request: Request = get_http_request()

        # 1) Authorization: Bearer <token>
        auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
        if auth_header and auth_header.lower().startswith("bearer "):
            token = auth_header.split(" ", 1)[1].strip()

        # 2) x-ai-organiser-token
        if not token:
            token = request.headers.get("x-ai-organiser-token")
    except Exception:
        token = None

    # 3) Фоллбек: single-user токен из окружения
    if not token:
        token = os.getenv(INTEGRATION_TOKEN_ENV_VAR)

    return token


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def oauth_protected_resource(request: Request) -> JSONResponse:
    """
    OAuth protected resource metadata for this MCP server.

    ChatGPT будет читать здесь:
      - какой ресурс защищаем,
      - какими авторизационными серверами можно пользоваться,
      - какие scope мы поддерживаем.
    """
    data = {
        "resource": "https://ai-organiser-mcp-1.onrender.com",
        "authorization_servers": [
            AUTH_BASE_URL,
        ],
        "scopes_supported": ["notes:write"],
        "resource_documentation": "https://ai-organiser.app/docs/chatgpt",
    }
    return JSONResponse(data)


@mcp.tool
def ai_organiser_save(
    body: str,
    project_name: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    - integration token берётся:
        1) из Authorization: Bearer <token> (будущий OAuth-доступ),
        2) или из заголовка x-ai-organiser-token,
        3) или из переменной окружения AI_ORGANISER_INTEGRATION_TOKEN (fallback).
    - Если project_name is None -> сохраняем в Inbox (не отправляем поле 'project').
    - Если project_name задан  -> отправляем его в поле 'project'.
    """

    if not SUPABASE_ANON_KEY:
        return {
            "saved": False,
            "error": "SUPABASE_ANON_KEY is not configured.",
        }

    integration_token = _resolve_integration_token()

    if not integration_token:
        return {
            "saved": False,
            "error": (
                "AI Organiser integration token is not provided. "
                "The MCP server expects it either via Authorization: Bearer <token>, "
                "x-ai-organiser-token header, "
                f"or in the environment variable {INTEGRATION_TOKEN_ENV_VAR}."
            ),
        }

    payload = {
        "text": body,
        "sourceUrl": None,
        "sourceTitle": title,
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
    # Запускаем сервер в режиме HTTP /mcp, который ждёт соединений от ChatGPT
    host = os.getenv("MCP_HOST", "0.0.0.0")
    port = int(os.getenv("MCP_PORT", "8000"))
    path = os.getenv("MCP_PATH", "/mcp")

    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path=path,
    )
