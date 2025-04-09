import logging
from aiohttp import web

from utils.config import HEALTH_CHECK_HOST, HEALTH_CHECK_PORT

async def health_check(request):
    """Simple health check endpoint."""
    # Add more sophisticated checks if needed (e.g., check DB connection, Discord gateway status)
    logging.debug("Health check endpoint accessed.")
    return web.Response(text="OK")

async def start_health_server(bot):
    """Starts the aiohttp web server for health checks."""
    app = web.Application()
    app.add_routes([web.get('/health', health_check)])
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