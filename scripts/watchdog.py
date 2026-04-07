"""
Watchdog — проверяет статус контейнеров и шлёт алерт в Telegram.
Запускать через cron каждые 5 минут.
"""
import subprocess
import sys
import httpx
import os

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
USER_IDS = os.environ.get("TELEGRAM_ALLOWED_USERS", "").split(",")

CONTAINERS = ["dd_bot", "dd_analyzer", "dd_ingestion", "dd_postgres", "dd_redis"]


def check_containers() -> list[str]:
    """Вернуть список упавших контейнеров."""
    failed = []
    for name in CONTAINERS:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True,
            text=True,
        )
        status = result.stdout.strip()
        if status != "running":
            failed.append(f"{name}: {status or 'not found'}")
    return failed


def send_alert(text: str) -> None:
    if not BOT_TOKEN:
        print("No bot token configured")
        return

    for user_id in USER_IDS:
        user_id = user_id.strip()
        if not user_id:
            continue
        try:
            httpx.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": user_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception as e:
            print(f"Failed to send alert: {e}")


if __name__ == "__main__":
    failed = check_containers()

    if failed:
        text = (
            "🚨 <b>Watchdog Alert</b>\n\n"
            "Следующие контейнеры не работают:\n"
            + "\n".join(f"❌ {c}" for c in failed)
            + "\n\n<i>Проверьте сервер</i>"
        )
        send_alert(text)
        print(f"ALERT sent: {failed}")
        sys.exit(1)
    else:
        print("All containers running OK")
        sys.exit(0)