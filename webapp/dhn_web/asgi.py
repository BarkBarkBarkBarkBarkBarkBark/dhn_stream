from channels.routing import ProtocolTypeRouter, URLRouter
from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler
from django.core.asgi import get_asgi_application
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dhn_web.settings")

django_asgi_app = get_asgi_application()

from dashboard.routing import websocket_urlpatterns  # noqa: E402 — must be after get_asgi_application

application = ProtocolTypeRouter(
    {
        "http": ASGIStaticFilesHandler(django_asgi_app),
        "websocket": URLRouter(websocket_urlpatterns),
    }
)
