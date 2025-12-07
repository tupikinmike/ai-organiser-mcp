import os
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request

# Имя сервера, которое будет видно в ChatGPT
mcp = FastMCP(
    name="AI Organiser MCP",
    instructions="""
This server exposes a single tool: ai_organiser_save.

GOAL:
- Save ChatGPT responses into the user's AI Organiser account.

AUTH / MULTI-USER MODEL:
- Each AI Organiser user has their own integration token.
- This token is NEVER typed in chat.
- The token is provided by the MCP connection itself and this server
  resolves it in the following order:
  1) Query parameter "token" in the MCP URL
     (e.g. https://ai-organiser-mcp.your-domain.com/mcp?token=USER_TOKEN)
  2) HTTP header "x-ai-organiser-token"
  3) HTTP header "Authorization: Bearer <token>"
  4) Environment variable AI_ORGANISER_INTEGRATION_TOKEN (single-user fallback)

- The resolved token is forwarded to AI Organiser as the x-api-key header.
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

# Настройки Supabase edge function (общие для всех пользователей)
SUPABASE_FUNCTION_URL = "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/quick-add"

# anon key Supabase (общий публичный ключ проекта)
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyem93c2Z3dXJndGNkeGp3ZXZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxMDUyMTYsImV4cCI6MjA3ODY4MTIxNn0."
    "0l394mJ9cLNN_QxNl9DKzdw1ni_-SBawGzoSrchNcJI"
)

# Имя переменной окружения, где лежит integration token (single-user fallback)
INTEGRATION_TOKEN_ENV_VAR = "AI_ORGANISER_INTEGRATION_TOKEN"


def _resolve_integration_token() -> str | None:
    """
    Определяем, какой integration token использовать для этого запроса.

    Приоритет:
      1. ?token=... в MCP URL
      2. заголовок x-ai-organiser-token
      3. Authorization: Bearer <token>
      4. переменная окружения AI_ORGANISER_INTEGRATION_TOKEN (fallback)
    """
    token: str | None = None

    try:
        request: Request = get_http_request()

        # 1) query-параметр ?token=...
        token = request.query_params.get("token")

        # 2) заголовок x-ai-organiser-token
        if not token:
            token = request.headers.get("x-ai-organiser-token")

        # 3) Authorization: Bearer <token>
        if not token:
            auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
            if auth_header and auth_header.lower().startswith("bearer "):
                token = auth_header.split(" ", 1)[1].strip()
    except Exception:
        # Если HTTP-контекст недоступен (другой транспорт и т.п.) — идём дальше
        token = None

    # 4) Фоллбек: single-user токен из окружения (как у тебя сейчас)
    if not token:
        token = os.getenv(INTEGRATION_TOKEN_ENV_VAR)

    return token


@mcp.tool
def ai_organiser_save(
    body: str,
    project_name: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    - integration token берётся:
        1) из query-параметра ?token=... MCP URL,
        2) или из заголовков (x-ai-organiser-token / Authorization: Bearer ...),
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
                "The MCP server expects it either in the MCP URL as ?token=YOUR_TOKEN, "
                "in headers (x-ai-organiser-token / Authorization: Bearer <token>), "
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
