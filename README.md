
# Discord Voice Activity Tracker Bot

A **Discord bot** that tracks voice channel activity, generates statistics, and provides **Text-to-Speech (TTS)** notifications for channel events. Built with [discord.py](https://github.com/Rapptz/discord.py) and packaged via Docker.

## Features

- **🎤 Voice Channel Activity Tracking**  
  - Tracks time spent in voice channels  
  - Generates daily, weekly, monthly, and yearly statistics  
  - Creates visual reports using [matplotlib](https://matplotlib.org/) and [seaborn](https://seaborn.pydata.org/)  

- **🔊 Text-to-Speech (TTS) Notifications**  
  - Announces when users join, leave, or switch voice channels  
  - Uses [gTTS](https://github.com/pndurette/gTTS) (Google Text-to-Speech) with support for Chinese  
  - Caches TTS files for improved performance and reduced latency  

- **📊 Statistical Analysis**  
  - Generates bar charts for voice activity rankings  
  - Creates heatmaps of user co-presence  
  - Stores historical data in **MongoDB** (voice durations and co-occurrence stats)  

- **🛡️ Robust Error Handling**  
  - Automatic reconnection and retry on disconnect  
  - Comprehensive logging system (with [Python logging](https://docs.python.org/3/library/logging.html))  
  - Periodic data backup (local JSON files) and saving to MongoDB  

## Requirements and Prerequisites

1. **Python 3.10 or higher** (if running locally without Docker).  
2. **Docker** and **Docker Compose** (if using the containerized solution).  
3. **MongoDB** (hosted or local) with a valid connection URI.  
4. A **Discord Bot Token** (create a bot from the [Discord Developer Portal](https://discord.com/developers/docs/intro)).

## Setup: Local Development (Without Docker)

1. **Clone or download** this repository:

   ```bash
   git clone https://github.com/yourusername/discord-voice-tracker.git
   cd discord-voice-tracker
   ```

2. **Create a virtual environment** (recommended):

   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies**:

   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment variables** in a `.env` file at the project root (same folder as `bot.py`). For example:

   ```
   DISCORD_TOKEN=your_discord_bot_token_here
   MONGODB_URI=mongodb+srv://username:password@cluster-url/dbname
   ```

   - `DISCORD_TOKEN`: Your Discord bot token.
   - `MONGODB_URI`: MongoDB URI for storing/retrieving voice stats.
   - (Optional) Other environment-specific variables if needed.

5. **Run the bot**:

   ```bash
   python bot.py
   ```

6. **Invite your bot** to a Discord server via the OAuth2 URL from the Developer Portal (make sure to enable the proper intents in your Developer Portal settings).

## Setup: Docker

This repository includes a **Dockerfile** and a **docker-compose.yml** for quick containerized deployment.

1. **Create a `.env` file** (same as above) with:

   ```
   DISCORD_TOKEN=your_discord_bot_token_here
   MONGODB_URI=mongodb+srv://username:password@cluster-url/dbname
   ```

2. **Build and run** with Docker Compose:

   ```bash
   docker-compose up -d --build
   ```

   This will:
   - Build an image using the multi-stage Dockerfile.
   - Create and run a container for the bot.
   - Expose port `8080` for the health check endpoint (`/health`).

3. **Verify the bot**:
   - Check logs: `docker-compose logs -f`
   - Confirm the bot is running: The health check endpoint at `http://localhost:8080/health` should return `OK`.

### Notes on Docker Setup

- The `docker-compose.yml` includes a **health check** to ensure the bot is running.  
- If you need persistent logging, you can configure **Docker volumes** or bind mounts.  
- Make sure the bot has the correct [Privileged Gateway Intents](https://discordpy.readthedocs.io/en/stable/intents.html) enabled: members, voice states, and message content.

## Usage

Once the bot is online in your server, try out the following commands:

- **`!stats [period]`**  
  Displays voice activity statistics (choose from `daily`, `weekly`, `monthly`, `yearly`).  
  Example: `!stats daily`

- **`!show_relationships`**  
  Generates a heatmap of the current server’s user co-presence (how long they spend together in voice channels).

- **`!leave`**  
  Instructs the bot to leave the voice channel it’s currently connected to.

- **`!play_test`**  
  Tests the bot’s TTS functionality by playing a short audio clip in your voice channel.

## How It Works

1. **Voice Tracking**  
   - The bot listens for `on_voice_state_update` events.  
   - It records join, leave, and switch times for each user.  
   - Usage data (in seconds) is updated per user in-memory.  

2. **Statistics and Storage**  
   - Data is periodically saved to MongoDB.  
   - Local JSON backups are also created (e.g., `data_backup/voice_stats_YYYYMMDD_HHMMSS.json`) in case MongoDB is unavailable.  

3. **Text-to-Speech Announcements**  
   - When a user joins, leaves, or switches channels, the bot generates a short TTS message using [gTTS](https://pypi.org/project/gTTS/).  
   - TTS output is cached (`tts_cache/`) to speed up subsequent plays of the same message.  
   - Bot uses `discord.FFmpegPCMAudio` to stream these MP3 files in the voice channel.  

4. **Scheduled Reports**  
   - The bot uses `apscheduler` to auto-generate daily, weekly, monthly, and yearly reports (at midnight or specified times).  
   - Reports are posted to your server’s system channel (or the first text channel the bot has permission to write in).  
   - Bar charts are created for top users, and activity data is then reset for the given period.  

5. **Heatmap Generation**  
   - The `!show_relationships` command triggers the creation of a heatmap for user co-occurrence in voice channels.  
   - The heatmap is posted as an image in the server.  

## Contributing

Contributions are welcome! To contribute:

1. Fork the repository  
2. Create your feature branch (`git checkout -b feature/my-feature`)  
3. Commit your changes (`git commit -m 'Add new feature'`)  
4. Push to the branch (`git push origin feature/my-feature`)  
5. Open a Pull Request  

We appreciate your help in improving this project!

## License

This project is licensed under the **MIT License**. See the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [discord.py](https://github.com/Rapptz/discord.py)  
- [gTTS](https://github.com/pndurette/gTTS)  
- [Matplotlib](https://matplotlib.org/) and [Seaborn](https://seaborn.pydata.org/)  
- [AIOHTTP](https://github.com/aio-libs/aiohttp) for async HTTP requests  
- [pytz](https://pythonhosted.org/pytz/) and [apscheduler](https://apscheduler.readthedocs.io/en/stable/) for scheduling  

## Contact

- **Author**: [Haokai Tan](https://github.com/Haokaiiii)  
- **Repository**: [GitHub](https://github.com/yourusername/discord-voice-tracker.git)  
- For issues, please open a ticket in the GitHub [Issues](https://github.com/yourusername/discord-voice-tracker/issues) section.

