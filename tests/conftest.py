"""
Общая настройка тестов.

Pydantic Settings требует TELEGRAM_BOT_TOKEN. Импорт многих модулей app тянет
app.services.__init__ → app.config.get_settings(), поэтому задаём безопасные
dummy-переменные окружения ДО любого импорта app.* в тестах.
"""
import os

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test:dummy-token")
os.environ.setdefault("BYBIT_API_KEY", "test-key")
os.environ.setdefault("BYBIT_API_SECRET", "test-secret")
os.environ.setdefault("BYBIT_NETWORK", "testnet")
