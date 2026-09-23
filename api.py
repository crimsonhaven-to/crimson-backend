"""The FastAPI app: routers, middleware and error handlers. The lifespan body is
in ``startup.py``; every endpoint lives in its engine."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import ORJSONResponse
from slowapi.errors import RateLimitExceeded

import startup
from account_engine import (
    admin_routes as account_admin,
    auth_routes,
    library_routes,
    profile_routes,
    security_routes,
    wrapped_routes,
)
from account_engine.audit import rate_limit_handler
from account_engine.login_wall import LoginWallMiddleware
from apikey_engine import admin_routes as apikey_admin
from cache_engine import admin_routes as cache_admin, routes as cache_routes
from changelog_engine import routes as changelog_routes
from chat_engine import admin_routes as chat_admin, routes as chat_routes
from core import errors, logging_setup
from core.config import get_settings
from core.middleware import LumiHeaderMiddleware, RequestContextMiddleware
from core.rate_limit import limiter
from core.version import VERSION
from download_engine import admin_routes as download_admin
from iptv_engine import routes as iptv_routes
from local_engine import admin_routes as local_admin, media_routes as local_media, routes as local_routes
from manga_engine import routes as manga_routes
from metadata_engine import admin_routes as metadata_admin, discovery_routes, routes as metadata_routes
from notify_engine import routes as airing_routes
from playback_engine import (
    admin_routes as playback_admin,
    grant_routes,
    movieweb_routes,
    proxy_routes,
    watch_routes,
)
from recommend_engine import routes as recommend_routes
from skiptimes_engine import routes as skiptimes_routes
from subtitles_engine import routes as subtitles_routes
from supporters_engine import routes as supporters_routes
from system_engine import admin_routes as system_admin, metrics_routes, routes as system_routes
from telemetry_engine import admin_routes as telemetry_admin, routes as telemetry_routes

logging_setup.configure(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await startup.start(app, logger)
    yield
    await startup.shutdown(app, logger)


app = FastAPI(
    title="Anime Streaming API",
    description="API for streaming anime with multi-season support",
    version=VERSION,
    lifespan=lifespan,
    # Several times faster than stdlib json for every plain dict response.
    default_response_class=ORJSONResponse,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_exception_handler(HTTPException, errors.http_exception_handler)
app.add_exception_handler(Exception, errors.unhandled_exception_handler)

for module in (
    auth_routes, profile_routes, library_routes, security_routes, wrapped_routes,
    supporters_routes, changelog_routes, recommend_routes, chat_routes, subtitles_routes,
    skiptimes_routes, manga_routes, iptv_routes, airing_routes,
    system_routes, discovery_routes, watch_routes, grant_routes, movieweb_routes,
    metadata_routes, proxy_routes, cache_routes, telemetry_routes, local_routes, local_media,
    metrics_routes,
    account_admin, apikey_admin, metadata_admin, local_admin, cache_admin, download_admin,
    chat_admin, telemetry_admin, playback_admin, system_admin,
):
    app.include_router(module.router)

overlay_prefixes = proxy_routes.register_overlay_proxies(app)

# Added innermost first. CORS sits outside the login wall so its headers reach
# even the wall's 401, which a browser needs to surface the error at all; the
# request context is outermost so a rejected request still gets an id and a count.
app.add_middleware(LoginWallMiddleware, extra_public_prefixes=overlay_prefixes)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(LumiHeaderMiddleware)
app.add_middleware(RequestContextMiddleware)
