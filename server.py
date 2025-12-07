import os
import re
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

STRICT CALL & PARSING RULES (SERVER-ENFORCED)
- The client MUST pass the exact latest user message as `user_message_raw`.
- The server inspects user_message_raw and applies these rules:

  1) If user_message_raw does NOT contain the word "сохрани" (any case),
     the server WILL NOT save anything and will return saved = False.

  2) If user_message_raw contains "сохрани" but does NOT contain "сохрани в",
     the server saves into Inbox (no project field).

  3) If user_message_raw contains "сохрани в ...":
     - If the message has quotes, e.g.:
         - 'сохрани в "Здоровье"'
         - 'сохрани в «Здоровье»'
       then the project name is the text inside the quotes.
     - Otherwise, the project name is everything after "сохрани в"
       (trimmed whitespace).

- The caller MAY optionally pass project_name directly; if it is provided,
  the server will use it. If project_name is None, the server will derive the
  project name from user_message_raw as described above.

- If a project with that name does not exist, the AI Organiser backend is
  responsible for creating it or reusing it.

WHEN TO CALL (INTENT)
- The client SHOULD call ai_organiser_save only when the user clearly asks
  to save something, for example:
  - "сохрани это"
  - "сохрани в \"Здоровье\""
  - "сохрани в «Работа»"
  - "save this"
- But even if the client misbehaves, the server-side checks above guarantee
  that nothing is saved unless the user message contains "сохрани".

WHAT TO SAVE
- When the user says "сохрани это" / "save this":
  - Use the content of YOUR PREVIOUS ASSISTANT MESSAGE as the body,
    unless the user explicitly points to another text.
  - Do NOT save the user's request ("Составь план..."), save your answer.

PROJECT / FOLDERS
- "сохрани" (without "в") -> save to Inbox (no project field).
- "сохрани в \"Имя\"" or "сохрани в «Имя»" -> save to project "Имя".
- If the backend needs to create the project, it will do so based on the
  passed project name.

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
        token = None

    # 4) Фоллбек: single-user токен из окружения (как у тебя сейчас)
    if not token:
        token = os.getenv(INTEGRATION_TOKEN_ENV_VAR)

    return token


def _infer_project_name_from_user_message(user_message_raw: str) -> tuple[bool, str | None]:
    """
    Разбираем user_message_raw и возвращаем:
      (should_save, project_name)

    Правила:
    - если нет "сохрани" -> should_save = False
    - если есть "сохрани", но нет "сохрани в" -> Inbox (project_name = None)
    - если есть "сохрани в ...":
        * сначала ищем кавычки ("Имя" или «Имя»)
        * если кавычек нет — берём всё после "сохрани в" как имя проекта
    """
    if not user_message_raw:
        return False, None

    normalized = user_message_raw.lower()

    # 1. Нет "сохрани" — не сохраняем вообще
    if "сохрани" not in normalized:
        return False, None

    # 2. Есть "сохрани", но нет "сохрани в" — сохраняем в Inbox
    if "сохрани в" not in normalized:
        return True, None  # Inbox

    # 3. Есть "сохрани в ..."
    # Сначала пытаемся найти текст в кавычках (оригинальный текст, чтобы не терять регистр)
    text = user_message_raw

    # Вариант с «ёлочками»
    match = re.search(r"сохрани\s+в\s*«([^»]+)»", text, flags=re.IGNORECASE)
    if match:
        project = match.group(1).strip()
        return True, project if project else None

    # Вариант с обычными двойными кавычками
    match = re.search(r'sохрани\s+в\s*"([^"]+)"', text, flags=re.IGNORECASE)
    if match:
        project = match.group(1).strip()
        return True, project if project else None

    # Если кавычек нет — берём всё после "сохрани в"
    # Простой фоллбек: ищем позицию "сохрани в" без учёта регистра
    idx = normalized.find("сохрани в")
    if idx != -1:
        # Берём часть строки после "сохрани в"
        after = text[idx + len("сохрани в") :].strip()
        if after:
            return True, after

    # Если ничего не смогли вытащить — считаем, что сохраняем в Inbox
    return True, None


@mcp.tool
def ai_organiser_save(
    body: str,
    project_name: str | None = None,
    title: str | None = None,
    user_message_raw: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    IMPORTANT SERVER LOGIC:
    - The client MUST pass the latest user message as `user_message_raw`.
    - The server:
        * will NOT save anything if user_message_raw does not contain "сохрани";
        * will save to Inbox if there is "сохрани" but no "сохрани в";
        * will save to a specific project if the message contains "сохрани в ...",
          using either the quoted name or the text after "сохрани в".

    - integration token берётся:
        1) из query-параметра ?token=... MCP URL,
        2) или из заголовков (x-ai-organiser-token / Authorization: Bearer ...),
        3) или из переменной окружения AI_ORGANISER_INTEGRATION_TOKEN (fallback).
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
                "AI Organiser integration token not found. "
                "The MCP server expects it either in the MCP URL "
                "as ?token=YOUR_TOKEN or in the server environment "
                f"variable {INTEGRATION_TOKEN_ENV_VAR}."
            ),
        }

    if not user_message_raw:
        # Без последнего сообщения пользователя мы не можем применить правила.
        return {
            "saved": False,
            "skipped": True,
            "reason": (
                "user_message_raw is missing. The client must pass the latest "
                "user message into user_message_raw when calling ai_organiser_save."
            ),
            "body_preview": (body or "")[:160],
        }

    should_save, inferred_project = _infer_project_name_from_user_message(user_message_raw)

    if not should_save:
        # Пользователь явно не просил "сохрани" — ничего не делаем.
        return {
            "saved": False,
            "skipped": True,
            "reason": (
                "Latest user message does not contain 'сохрани'; "
                "skipping save to avoid accidental calls."
            ),
            "user_message_preview": user_message_raw[:160],
            "body_preview": (body or "")[:160],
        }

    # Определяем итоговое имя проекта:
    # 1) если явно передан project_name в аргументах — используем его;
    # 2) иначе используем то, что вытащили из user_message_raw;
    # 3) если там None — значит сохраняем в Inbox.
    final_project_name = project_name if project_name is not None else inferred_project

    payload: dict = {
        "text": body,
        "sourceUrl": None,
        "sourceTitle": title,
    }

    if final_project_name:
        payload["project"] = final_project_name

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
            "project_name": final_project_name or "Inbox",
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
