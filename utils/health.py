"""Health check server utilities.

Provides a lightweight aiohttp server exposing a ``/health`` endpoint for
container orchestrators or uptime probes.
"""
import logging
from aiohttp import web

from utils.config import HEALTH_CHECK_HOST, HEALTH_CHECK_PORT
from utils.database import DatabaseManager

async def health_check(request):
    """Simple health check endpoint.

    Parameters
    ----------
    request : aiohttp.web.Request
        Incoming HTTP request.

    Returns
    -------
    aiohttp.web.Response
        Response with the text ``"OK"`` when healthy.
    """
    # Add more sophisticated checks if needed (e.g., check DB connection, Discord gateway status)
    logging.debug("Health check endpoint accessed.")
    return web.Response(text="OK")


async def readiness_check(request):
    """Readiness probe endpoint.

    Attempts a lightweight check against core dependencies (e.g., MongoDB)
    to determine if the bot is ready to serve traffic.

    Parameters
    ----------
    request : aiohttp.web.Request
        Incoming HTTP request.

    Returns
    -------
    aiohttp.web.Response
        200 OK if ready, 503 Service Unavailable otherwise.
    """
    ok = True
    try:
        # Minimal DB ping using sync client for simplicity
        dbm = DatabaseManager()
        dbm.sync_client.admin.command('ping')
    except Exception:
        ok = False

    status = 200 if ok else 503
    return web.Response(text="READY" if ok else "NOT_READY", status=status)

async def start_health_server(bot):
    """Start the aiohttp web server for health checks.

    Parameters
    ----------
    bot : discord.Client or commands.Bot
        Bot instance used to store the runner for later cleanup.

    Returns
    -------
    None
    """
    app = web.Application()
    app.add_routes([
        web.get('/health', health_check),
        web.get('/ready', readiness_check),
    ])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HEALTH_CHECK_HOST, HEALTH_CHECK_PORT)
    try:
        await site.start()
        logging.info(f"Health check server started on http://{HEALTH_CHECK_HOST}:{HEALTH_CHECK_PORT}/health")
        # Keep the server running in the background
        # The runner will be cleaned up when the bot shuts down
        bot.health_runner = runner # Store runner on bot for later cleanup
    except Exception as e:
        logging.error(f"Failed to start health check server: {e}", exc_info=True) 