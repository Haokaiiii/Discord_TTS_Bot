# Discord Voice Activity Tracker Bot

A Discord bot that tracks voice channel activity, generates statistics, and provides Text-to-Speech (TTS) notifications for channel events. Built with discord.py and Docker.

## Features

- 🎤 Voice Channel Activity Tracking
  - Tracks time spent in voice channels
  - Generates daily, weekly, monthly, and yearly statistics
  - Creates visual reports using matplotlib and seaborn

- 🔊 Text-to-Speech (TTS) Notifications
  - Announces when users join/leave voice channels
  - Supports Chinese language using gTTS
  - Caches TTS files for better performance

- 📊 Statistical Analysis
  - Generates heatmaps of user co-presence
  - Creates bar charts for voice activity rankings
  - Stores historical data in MongoDB

- 🛡️ Robust Error Handling
  - Automatic reconnection on disconnects
  - Rate limiting and cooldown management
  - Comprehensive logging system

## Prerequisites

- Python 3.10 or higher
- Docker and Docker Compose
- MongoDB database
- Discord Bot Token

## Installation

1. Clone the repository:
bash
git clone https://github.com/yourusername/discord-voice-tracker.git
cd discord-voice-tracker

2. Create a `.env` file with your credentials:

bash
DISCORD_TOKEN=your_discord_token_here
MONGODB_URI=your_mongodb_uri_here

3. Build and run with Docker Compose:

bash
docker-compose up -d


## Commands

- `!stats [period]` - Show voice activity statistics (daily/weekly/monthly/yearly)
- `!show_relationships` - Display user co-presence heatmap
- `!leave` - Make the bot leave the voice channel
- `!play_test` - Test TTS functionality

## Docker Support

The application is containerized using Docker with multi-stage builds for optimal performance. The Docker setup includes:

- Health checks
- Volume mounts for persistent data
- Automatic restarts
- Log rotation
- Non-root user security

## Development

To set up the development environment:

1. Create a virtual environment:

bash
python -m venv venv
source venv/bin/activate # On Windows: venv\Scripts\activate


2. Install dependencies:

bash
pip install -r requirements.txt


3. Run the bot locally:

bash
python bot.py


## Contributing

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- [discord.py](https://github.com/Rapptz/discord.py)
- [gTTS](https://github.com/pndurette/gTTS)
- [matplotlib](https://matplotlib.org/)
- [seaborn](https://seaborn.pydata.org/)

## Contact

Haokai Tan - [@Haokai Tan](https://github.com/Haokaiiii)# Discord-copy
