# Discord TTS Bot Project Structure

This document outlines the structure of the refactored Discord TTS Bot.

## Directory Structure

```
Discord_TTS_Bot/
├── cogs/                 # Contains Discord.py Cogs (modular components)
│   ├── __init__.py
│   ├── tts.py            # TTS related commands and logic
│   ├── stats.py          # Voice statistics commands and logic
│   ├── admin.py          # Administrative commands (if any)
│   └── events.py         # Event handlers (on_ready, on_voice_state_update, etc.)
├── utils/                # Utility functions and classes
│   ├── __init__.py
│   ├── config.py         # Configuration loading and management
│   ├── database.py       # Database interaction logic (MongoDB)
│   ├── helpers.py        # General helper functions
│   ├── logging_config.py # Logging setup
│   └── plotting.py       # Functions for generating graphs/plots
├── data_backup/          # Local backups of database data (auto-generated)
├── tts_cache/            # Cached TTS audio files (auto-generated)
├── .env                  # Environment variables (Discord token, MongoDB URI, etc.) - **DO NOT COMMIT**
├── Dockerfile            # Docker build instructions
├── main.py               # Main bot entry point
├── requirements.txt      # Python dependencies
└── project.md            # This file
```

## Module Descriptions

*   **`main.py`**: The main entry point for the bot. Initializes the bot, loads configuration, sets up logging, connects to the database, loads cogs, and starts the bot and background tasks (like the health check server).
*   **`cogs/`**: Directory containing modules (Cogs) that group related commands and event listeners.
    *   **`tts.py`**: Handles Text-to-Speech functionality, including commands to play TTS, queue management, TTS generation, voice channel joining/leaving, and related voice state updates.
    *   **`stats.py`**: Manages voice activity tracking, statistics calculation (daily, weekly, etc.), co-occurrence tracking, and commands to display stats and relationship heatmaps. Includes data saving/loading logic (interacting with `database.py`).
    *   **`admin.py`**: (Optional, can be added later) For administrative commands like setting command channels, excluded channels, etc.
    *   **`events.py`**: Contains general event handlers like `on_ready`, `on_guild_join`, `on_guild_remove`, `on_disconnect`, and potentially parts of `on_voice_state_update` that coordinate actions across multiple cogs or handle initial setup.
*   **`utils/`**: Directory for shared utility code.
    *   **`config.py`**: Loads environment variables and potentially other configuration settings. Provides access to configuration values throughout the application.
    *   **`database.py`**: Encapsulates all interactions with the MongoDB database. Provides functions for saving/loading voice stats and co-occurrence data. Handles database connection setup. Includes backup/restore logic coordination.
    *   **`helpers.py`**: Contains miscellaneous helper functions used across different parts of the bot (e.g., `get_preferred_name`, permission checks).
    *   **`logging_config.py`**: Configures the logging format and level for the application.
    *   **`plotting.py`**: Contains functions responsible for generating the Matplotlib/Seaborn graphs (heatmaps, stat charts).
*   **`data_backup/`**: Stores local JSON backups of the database content as a safety measure.
*   **`tts_cache/`**: Stores generated TTS audio files to avoid regenerating them frequently.
*   **`.env`**: Stores sensitive information like API keys and connection strings.
*   **`Dockerfile`**: Defines the Docker image for the bot, including dependencies, copying code, and specifying the entry point (`main.py`).
*   **`requirements.txt`**: Lists Python package dependencies.

## Key Changes from Original `bot.py`

*   **Modularity**: Code is broken down into logical units (Cogs and utils) instead of one large file.
*   **Cogs**: Discord.py's Cog system is used for better organization of commands and listeners.
*   **Clear Separation**: Database logic, configuration, plotting, and core bot functionality are separated into distinct modules.
*   **Dependency Injection**: Shared resources like the database connection or configuration might be passed to Cogs during initialization instead of relying solely on global variables.
*   **`main.py` as Entry Point**: The bot startup and orchestration logic is now in `main.py`.

This structure aims to improve maintainability, readability, and scalability of the bot. 