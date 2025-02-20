
# Discord Voice Activity Tracker Bot

A **Discord bot** that tracks voice channel activity, generates statistics, and provides **Text-to-Speech (TTS)** notifications for channel events. Built with [discord.py](https://github.com/Rapptz/discord.py) and packaged via Docker.

## Features

- **🎤 Voice Channel Activity Tracking**  
  - Tracks time spent in voice channels  
  - Generates daily, weekly, monthly, and yearly statistics  
  - Creates visual reports using [matplotlib](https://matplotlib.org/) and [seaborn](https://seaborn.pydata.org/)

- **🔊 Text-to-Speech (TTS) Notifications**  
  - Announces when users **join**, **leave**, **switch** voice channels, and (optionally) when they **change their nickname** while in voice  
  - Uses [gTTS](https://github.com/pndurette/gTTS) (Google Text-to-Speech) with support for Chinese  
  - Caches TTS files for improved performance and reduced latency  

- **📊 Statistical Analysis**  
  - Generates **bar charts** for voice activity rankings  
  - Creates **heatmaps** of user co-presence (both **absolute** and **relative** co-occurrence)  
  - Automatically sorts heatmap axes by **total voice time** (descending), providing a more intuitive view  
  - Stores historical data in **MongoDB** (voice durations and co-occurrence stats)  
  - **Local JSON backups** in case MongoDB is unavailable (e.g., `data_backup/voice_stats_YYYYMMDD_HHMMSS.json`)

- **🛡️ Robust Error Handling**  
  - Automatic reconnection and retry on disconnect  
  - Periodic checks to reset/fix broken voice connections  
  - Comprehensive logging system (with [Python logging](https://docs.python.org/3/library/logging.html))  
  - Scheduled data backup (local JSON + MongoDB) and cleanup of old TTS files  

## Requirements and Prerequisites

1. **Python 3.10 or higher** (if running locally without Docker).  
2. **Docker** and **Docker Compose** (if using the containerized solution).  
3. **MongoDB** (hosted or local) with a valid connection URI.  
4. A **Discord Bot Token** (create a bot from the [Discord Developer Portal](https://discord.com/developers/docs/intro)).  
5. (Optional) If you want to **restrict commands** to a single text channel, be sure to update the **`ALLOWED_COMMAND_CHANNEL_ID`** in `bot.py` to the channel ID of your choice.

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

   > **Note:** The bot code **hardcodes** `ALLOWED_COMMAND_CHANNEL_ID`. To change it, open `bot.py` and modify the value near the top:
   >
   > ```python
   > ALLOWED_COMMAND_CHANNEL_ID = 555240012460064771
   > ```
   > This ensures all bot commands are only accepted in that specific text channel.

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
- (Optional) If you want to change the text channel where commands are allowed, edit the `ALLOWED_COMMAND_CHANNEL_ID` in `bot.py` and rebuild the image.

## Font Support and CJK Characters

The bot uses a comprehensive set of fonts to properly display Chinese characters and emojis in its statistical visualizations. The following fonts are included in the Docker image:

- Noto CJK (fonts-noto-cjk)
- Noto CJK Extra (fonts-noto-cjk-extra)
- Noto Color Emoji (fonts-noto-color-emoji)
- WenQuanYi MicroHei (fonts-wqy-microhei)
- WenQuanYi ZenHei (fonts-wqy-zenhei)
- Arphic UKai (fonts-arphic-ukai)
- Arphic UMing (fonts-arphic-uming)

This ensures proper rendering of:
- Chinese characters in statistics and reports
- Emoji in usernames and channel names
- Various Unicode symbols used in the bot's output

### Font Configuration

The bot uses system fonts with default configuration, allowing for automatic font selection based on the characters being displayed. This approach provides:

- Seamless handling of mixed Latin and CJK text
- Proper emoji rendering
- Consistent appearance across different types of text

No manual font configuration is needed in the Python code, as the system automatically selects the appropriate font based on the content being displayed.

## Usage

Once the bot is online in your server, and **if your `ALLOWED_COMMAND_CHANNEL_ID`** is set, you can only issue the following commands from that channel:

- **`!stats [period]`**  
  Displays voice activity statistics (choose from `daily`, `weekly`, `monthly`, `yearly`).  
  Example: `!stats daily`

- **`!show_relationships`**  
  Generates a **heatmap of absolute co-presence** in voice channels, sorted by each user's total voice time (largest to smallest).

- **`!show_relationships_relative`**  
  Generates a **relative** co-presence heatmap, showing the percentage of mutual time compared to each pair's minimum total voice time, also sorted by total voice time.

- **`!check_nickname [@member]`**  
  Displays the specified member's internal "preferred name" logic (nick vs. global_name vs. username).

- **`!leave`**  
  Instructs the bot to leave the voice channel it's currently connected to.

- **`!play_test`**  
  Tests the bot's TTS functionality by playing a short audio clip in your voice channel.

- **`!test_delete`**  
  Sends a test message that automatically deletes itself after 5 seconds (for debugging).

### Automatic (Scheduled) Reports

The bot uses `apscheduler` to automatically generate daily, weekly, monthly, and yearly reports at **midnight** (or the start of the week/month/year) based on `Australia/Sydney` time. These reports include:

1. A **text ranking** of top users for the specified period.  
2. A **bar chart** image showing the same ranking.  
3. Two **heatmaps**: absolute co-presence (hours) and relative co-presence (percentage).

After each automated report, the bot **resets** that period’s counters (e.g., `daily`, `weekly`, etc.) back to zero.

## How It Works

1. **Voice Tracking**  
   - The bot listens for `on_voice_state_update` events.  
   - It records join, leave, and switch times for each user.  
   - Usage data (in seconds) is updated per user in-memory.  

2. **Statistics and Storage**  
   - Data is periodically saved to MongoDB **and** also backed up locally (JSON) in case Mongo fails.  
   - Local JSON backups are created in `data_backup/` with timestamps.  
   - On startup, the bot attempts to load from either the most recent backup or MongoDB.

3. **Text-to-Speech Announcements**  
   - When a user joins, leaves, or switches channels, the bot generates a short TTS message using [gTTS](https://pypi.org/project/gTTS/).  
   - **Nickname changes:** If the user changes their nickname while still in a voice channel, the bot can optionally announce it.  
   - TTS output is cached (`tts_cache/`) to speed up subsequent plays of the same message.  
   - The bot uses `discord.FFmpegPCMAudio` to stream these MP3 files in the voice channel.  

4. **Automatic Reconnection and Health Checks**  
   - A periodic task checks for broken or inactive voice connections and attempts to reconnect.  
   - If the bot is offline or has trouble connecting to a channel, it uses exponential backoff for retries.

5. **Scheduled Reports**  
   - The bot uses `apscheduler` to auto-generate daily, weekly, monthly, and yearly reports (at midnight or specified times).  
   - Reports are posted to your server's system channel (or the first text channel the bot has permission to write in).  
   - Bar charts are created for top users, and activity data is then reset for the given period.  

6. **Heatmap Generation**  
   - The `!show_relationships` command triggers the creation of an **absolute** heatmap for user co-occurrence in voice channels.  
   - The `!show_relationships_relative` command triggers a **relative** heatmap, showing co-presence as a percentage (0–100%).  
   - Both heatmaps are automatically **sorted** by total user voice time in **descending** order.  
   - The resulting heatmap is posted as an image in the server.

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

