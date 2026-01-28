from careerai_bot_mvp import app as fastapi_app

# Vercel Python runtime expects a WSGI/ASGI-compatible object named `app`.
# Здесь мы просто проксируем FastAPI-приложение из careerai_bot_mvp.
app = fastapi_app

