import os
import httpx
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.requests import Request

mcp = FastMCP(
    name="AI Organiser MCP",
    instructions="""
This MCP server exposes a single tool: ai_organiser_save.

GOAL
- Save ChatGPT responses into the calling user's AI Organiser account.

AUTH / MULTI-USER MODEL
- Each AI Organiser user has their own integration token.
- The token is NEVER typed or requested in chat.
- The token comes from the MCP connection itself:
  - Prefer: the query parameter "token" in the MCP URL
    (for example: https://ai-organiser-mcp.your-domain.com/mcp?token=USER_TOKEN).
  - Optionally: connector/auth secrets forwarded as HTTP headers by the MCP client
    (for example: x-ai-organiser-token or Authorization: Bearer <token>).
- The server forwards this token to AI Organiser as the x-api-key header.
- If no per-user token is found, the server MAY fall back to a single shared
  token from its environment variable AI_ORGANISER_INTEGRATION_TOKEN.
  This is mainly for development / single-user setups.

STRICT TOOL CALL CONDITIONS
- You MUST call ai_organiser_save ONLY if ALL of the following are true:
  1) The LATEST user message explicitly contains the word "сохрани"
     (any case, e.g. "Сохрани", "сохрани", "СОХРАНИ"),
     or the English word "save" (e.g. "save this", "Save to project health").
  2) There is already at least one previous ASSISTANT message that contains
     the content to save.
- If the user does NOT explicitly say "сохрани" or "save" in their last message,
  DO NOT call ai_organiser_save.
- Never guess or assume that the user wants to save something implicitly.
- Examples when you MUST NOT call the tool:
  - "напиши мне привет"
  - "составь план тренировки"
  - "перепиши текст более коротко"
  - any greeting or question without the word "сохрани" or "save".

WHEN TO CALL (EXAMPLES)
- Allowed examples:
  - "сохрани это"
  - "сохрани в библиотеку"
  - "сохрани в проект здоровье"
  - "save this"
  - "save this to the library"
  - "save this to project X"
- In all other cases, do not call this tool.

WHAT TO SAVE
- When the user says "сохрани это" / "save this":
  - Use the content of YOUR PREVIOUS ASSISTANT MESSAGE as the body,
    unless the user explicitly points to another text.
  - Do NOT save the user's request ("Составь план..."), save your answer.

PROJECT / FOLDERS
- If the user just says "сохрани это", save into the default Inbox
  (do not send a project name).
- If the user names a project (e.g. "сохрани в проект здоровье"),
  pass that project name as the "project" field.
- Do not try to validate or normalize project names: the AI Organiser backend
  will decide whether to create or reuse the project.

TURN ORDER / PATTERN
- First: answer the user's main question normally.
- Only in a FOLLOW-UP user message like "сохрани это…" you may call this tool.
- Never both generate the main answer and call ai_organiser_save in the same turn.

SECURITY / PRIVACY
- Never ask the user to paste or reveal their AI Organiser integration token.
- Assume the token is provided via MCP URL or connector/auth settings only.
- Do not echo tokens in tool outputs, logs or error messages.
""",
)

SUPABASE_FUNCTION_URL = "https://trzowsfwurgtcdxjwevi.supabase.co/functions/v1/quick-add"

# Публичный anon key Supabase (по твоему описанию можно держать в коде,
# но в проде лучше вынести в переменную окружения).
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyem93c2Z3dXJndGNkeGp3ZXZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxMDUyMTYsImV4cCI6MjA3ODY4MTIxNn0."
    "0l394mJ9cLNN_QxNl9DKzdw1ni_-SBawGzoSrchNcJI"
)

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

    # 1–3: пробуем достать токен из текущего HTTP-запроса.
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
        # Если по какой-то причине HTTP-контекст недоступен — просто пропускаем.
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

    IMPORTANT:
    - Call this tool ONLY if the LATEST user message explicitly contains
      the word "сохрани" (Russian) or "save" (English).
    - Never call this tool for greetings, questions or any messages
      that do not explicitly ask to save something.

    - integration token берётся:
        1) из query-параметра ?token=... MCP URL,
        2) или из заголовков (x-ai-organiser-token / Authorization: Bearer ...),
        3) или из переменной окружения AI_ORGANISER_INTEGRATION_TOKEN (fallback).
    - Если project_name is None -> сохраняем в Inbox (не отправляем поле 'project').
    - Если project_name задан -> отправляем его в поле 'project'.
    """

    if not SUPABASE_ANON_KEY:
        return {
            "saved": False,
            "error": "SUPABASE_ANON_KEY is not configured.",
        }

    integration_token = _resolve_integration_token()

    if not integration_token:
        # Важно: не просим пользователя вставлять токен в чат.
        return {
            "saved": False,
            "error": (
                "AI Organiser integration token not found. "
                "The MCP server expects it either in the MCP URL "
                "as ?token=YOUR_TOKEN or in the server environment "
                f"variable {INTEGRATION_TOKEN_ENV_VAR}."
            ),
        }

    payload: dict = {
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
    # Для Render: слушаем порт из env и путь /mcp
    port = int(os.getenv("PORT", "8000"))
    mcp.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=port,
        path="/mcp",
    )
