import os
import re
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

SERVER-SIDE SAVE RULES (RUSSIAN "СОХРАНИ"):
- The client MUST pass the latest user message as `user_message_raw`.
- The server enforces:

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

- If a project with that name does not exist, the AI Organiser backend is
  responsible for creating it or reusing it.

INTENT (WHEN TO CALL):
- The client SHOULD call ai_organiser_save only when the user clearly asks
  to save something, for example:
  - "сохрани это"
  - "сохрани в \"Здоровье\""
  - "сохрани в «Работа»"
- But even if the client misbehaves, the server-side rules above guarantee
  that nothing is saved unless the user message contains "сохрани".

WHAT TO SAVE:
- When the user says "сохрани это":
  - Use the content of YOUR PREVIOUS ASSISTANT MESSAGE as `body`,
    unless the user explicitly points to another text.
- Do NOT save the user's request ("Составь план..."), save your answer.

PROJECT / FOLDERS:
- "сохрани" (without "в") -> save to Inbox (no project field).
- "сохрани в \"Имя\"" or "сохрани в «Имя»" -> save to project "Имя".

TURN ORDER / PATTERN:
- First: answer the user's main question normally.
- Only in a FOLLOW-UP user message like "сохрани это…" you may call this tool.
- Never both generate the main answer and call ai_organiser_save in the same turn.

SECURITY / PRIVACY:
- Never ask the user to paste or reveal their AI Organiser integration token.
- Assume the token is provided via MCP URL or connector/auth settings only.
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
        # Если HTTP-контекст недоступен — идём дальше
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

    text = user_message_raw
    lower = text.lower()

    # 1. Нет "сохрани" — не сохраняем вообще
    if "сохрани" not in lower:
        return False, None

    # 2. Есть "сохрани", но нет "сохрани в" — сохраняем в Inbox
    if "сохрани в" not in lower:
        return True, None  # Inbox

    # 3. Есть "сохрани в ..."
    # Сначала пытаемся найти текст в кавычках (оригинальный текст, чтобы не терять регистр)

    # Вариант с «ёлочками»
    match = re.search(r"сохрани\s+в\s*«([^»]+)»", text, flags=re.IGNORECASE)
    if match:
        project = match.group(1).strip()
        return True, project or None

    # Вариант с обычными двойными кавычками
    match = re.search(r'сохрани\s+в\s*"([^"]+)"', text, flags=re.IGNORECASE)
    if match:
        project = match.group(1).strip()
        return True, project or None

    # Если кавычек нет — берём всё после "сохрани в"
    idx = lower.find("сохрани в")
    if idx != -1:
        after = text[idx + len("сохрани в") :].strip()
        if after:
            return True, after

    # Если ничего не смогли вытащить — считаем, что сохраняем в Inbox
    return True, None


@mcp.tool
def ai_organiser_save(
    body: str,
    user_message_raw: str,             # последний месседж юзера — ОБЯЗАТЕЛЬНО
    project_name: str | None = None,   # можно не передавать — сервер сам выведет из фразы
    title: str | None = None,
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
                "AI Organiser integration token is not provided. "
                "The MCP server expects it either in the MCP URL as ?token=YOUR_TOKEN, "
                "in headers (x-ai-organiser-token / Authorization: Bearer <token>), "
                f"or in the environment variable {INTEGRATION_TOKEN_ENV_VAR}."
            ),
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
    # 1) если явно передан project_name — используем его;
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
