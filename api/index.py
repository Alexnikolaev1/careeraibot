"""
Vercel Serverless Function для CareerAI Bot
Этот файл экспортирует FastAPI app для работы на Vercel
"""
import sys
import os

# Добавляем корневую директорию в путь для импорта
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from careerai_bot_mvp import app
    
    # Vercel Python runtime автоматически ищет переменную `app` (ASGI/WSGI)
    # Все запросы будут обрабатываться через FastAPI приложение
except Exception as e:
    # Если импорт не удался, создаем простой app для диагностики
    from fastapi import FastAPI
    app = FastAPI()
    
    @app.get("/")
    async def error():
        return {"error": f"Failed to import main app: {str(e)}"}
    
    @app.get("/api/health")
    async def health():
        return {"error": f"Failed to import main app: {str(e)}"}
