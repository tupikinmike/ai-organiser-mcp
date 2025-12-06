import os
import httpx
from fastmcp import FastMCP

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
- It is provided via connector configuration and passed to this tool
  as the `integration_token` parameter.
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
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InRyem93c2Z3dXJndGNkeGp3ZXZpIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjMxMDUyMTYsImV4cCI6MjA3ODY4MTIxNn0.0l394mJ9cLNN_QxNl9DKzdw1ni_-SBawGzoSrchNcJI"  # у себя подставишь реальный anon key


@mcp.tool
def ai_organiser_save(
    body: str,
    integration_token: str,
    project_name: str | None = None,
    title: str | None = None,
) -> dict:
    """
    Save a text message to AI Organiser as a note.

    - integration_token: per-user integration token from AI Organiser (required)
      (this is the same token the user sees in Settings → Integrations).
    - If project_name is None -> save to Inbox (do not send 'project' field)
    - If project_name is set   -> save to that project (send 'project' field)
    """

    if not SUPABASE_ANON_KEY:
        return {
            "saved": False,
            "error": "SUPABASE_ANON_KEY is not configured.",
        }

    if not integration_token:
        return {
            "saved": False,
            "error": (
                "integration_token is required. It must be provided via connector "
                "configuration, NOT typed by the user in chat."
            ),
        }

    # Базовый payload для edge-функции quick-add
    payload = {
        "text": body,
        "sourceUrl": None,
        "sourceTitle": None,
    }

    # Если проект указан — добавляем его в payload,
    # иначе даём функции самой положить в Inbox
    if project_name:
        payload["project"] = project_name

    headers = {
        "Content-Type": "application/json",
        # инфраструктурный ключ Supabase (одинаковый для всех пользователей)
        "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
        # integration_token конкретного юзера: бэкенд AI Organiser по нему поймёт,
        # в чей аккаунт сохранять
        "x-api-key": integration_token,
    }

    try:
        with httpx.Client(timeout=10.0) as client:
            res = client.post(SUPABASE_FUNCTION_URL, json=payload, headers=headers)

        if res.status_code >= 400:
            return {
                "saved": False,
                "status_code": res.status_code,
                "error": f"Supabase returned {res.status_code}",
                "response_text": res.text,
            }

        try:
            data = res.json()
        except Exception:
            data = None

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