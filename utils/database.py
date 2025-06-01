import logging
import json
import os
import glob
from datetime import datetime
from typing import Dict, Tuple, Any, Optional, List
from pymongo import MongoClient
from pymongo.errors import PyMongoError
import backoff
from asyncio import Lock
from motor.motor_asyncio import AsyncIOMotorClient

from utils.config import MONGODB_URI, BACKUP_DIR, MAX_BACKUP_FILES

class DatabaseManager:
    def __init__(self):
        """Initialize database manager with connection pooling and async support."""
        try:
            # Sync client for initial loading
            self.sync_client = MongoClient(
                MONGODB_URI, 
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                maxPoolSize=50
            )
            # Test connection
            self.sync_client.admin.command('ping')
            
            # Async client for runtime operations
            self.async_client = AsyncIOMotorClient(
                MONGODB_URI,
                serverSelectionTimeoutMS=5000,
                connectTimeoutMS=10000,
                maxPoolSize=50
            )
            
            self.db_name = 'discord_bot'
            self.sync_db = self.sync_client[self.db_name]
            self.async_db = self.async_client[self.db_name]
            
            # Collection names
            self.voice_stats_collection = 'voice_stats'
            self.co_occurrence_collection = 'co_occurrence_stats'
            
            # Async lock for save operations
            self.save_lock = Lock()
            
            logging.info("DatabaseManager initialized with connection pooling.")
            
        except PyMongoError as e:
            logging.error(f"Failed to connect to MongoDB: {e}")
            raise

    def _rotate_backups(self, pattern: str) -> None:
        """Rotate backup files to maintain MAX_BACKUP_FILES limit."""
        backup_files = sorted(glob.glob(os.path.join(BACKUP_DIR, pattern)))
        
        if len(backup_files) > MAX_BACKUP_FILES:
            files_to_remove = backup_files[:-MAX_BACKUP_FILES]
            for file_path in files_to_remove:
                try:
                    os.remove(file_path)
                    logging.info(f"Removed old backup: {file_path}")
                except OSError as e:
                    logging.error(f"Failed to remove old backup {file_path}: {e}")

    def _save_local_backup(self, data: Dict[Any, Any], filename: str) -> bool:
        """Saves data to a local backup file with rotation."""
        backup_path = os.path.join(BACKUP_DIR, filename)
        
        try:
            # Write to temp file first
            temp_path = backup_path + '.tmp'
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            # Atomic rename
            os.replace(temp_path, backup_path)
            logging.info(f"Local backup created: {backup_path}")
            
            # Rotate old backups
            base_name = filename.split('_')[0]
            self._rotate_backups(f"{base_name}_*.json")
            
            return True
            
        except (IOError, OSError) as e:
            logging.error(f"Failed to create local backup {backup_path}: {e}")
            # Clean up temp file if it exists
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass
            return False

    def _load_local_backup(self, filename: str) -> Optional[Dict[Any, Any]]:
        """Loads data from a local backup file."""
        backup_path = os.path.join(BACKUP_DIR, filename)
        
        if not os.path.exists(backup_path):
            return None
            
        try:
            with open(backup_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            logging.info(f"Loaded data from backup: {backup_path}")
            return data
        except (IOError, json.JSONDecodeError) as e:
            logging.error(f"Failed to load local backup {backup_path}: {e}")
            return None

    def _find_latest_backup(self, prefix: str) -> Optional[str]:
        """Find the most recent backup file with given prefix."""
        pattern = os.path.join(BACKUP_DIR, f"{prefix}_*.json")
        backup_files = sorted(glob.glob(pattern))
        
        if backup_files:
            return os.path.basename(backup_files[-1])
        return None

    @backoff.on_exception(
        backoff.expo,
        PyMongoError,
        max_tries=5,
        max_time=300,
        on_backoff=lambda details: logging.warning(f"MongoDB retry attempt {details['tries']} after {details['wait']:.1f}s")
    )
    async def save_voice_stats(self, voice_stats_data: Dict[int, Dict[int, Dict[str, float]]]) -> bool:
        """Save voice statistics with retry logic and backup."""
        async with self.save_lock:
            try:
                # Create backup
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_data = {
                    str(guild_id): {
                        str(member_id): stats
                        for member_id, stats in members.items()
                    }
                    for guild_id, members in voice_stats_data.items()
                }
                
                backup_success = self._save_local_backup(
                    backup_data, 
                    f"voice_stats_{timestamp}.json"
                )
                
                if not backup_success:
                    logging.warning("Failed to create backup before saving to MongoDB")
                
                # Save to MongoDB using individual operations (more reliable than bulk)
                collection = self.async_db[self.voice_stats_collection]
                
                success_count = 0
                for guild_id, members in voice_stats_data.items():
                    try:
                        serialized_members = {
                            str(member_id): stats
                            for member_id, stats in members.items()
                        }
                        
                        result = await collection.update_one(
                            {'guild_id': guild_id},
                            {
                                '$set': {
                                    'members': serialized_members, 
                                    'updated_at': datetime.utcnow()
                                }
                            },
                            upsert=True
                        )
                        success_count += 1
                        
                    except PyMongoError as e:
                        logging.error(f"Failed to save voice stats for guild {guild_id}: {e}")
                        continue
                
                logging.info(f"Voice stats saved to MongoDB: {success_count}/{len(voice_stats_data)} guilds")
                return success_count > 0
                
            except PyMongoError as e:
                logging.error(f"MongoDB error saving voice stats: {e}")
                raise
            except Exception as e:
                logging.error(f"Unexpected error saving voice stats: {e}", exc_info=True)
                return False

    @backoff.on_exception(
        backoff.expo,
        PyMongoError,
        max_tries=5,
        max_time=300,
        on_backoff=lambda details: logging.warning(f"MongoDB retry attempt {details['tries']} after {details['wait']:.1f}s")
    )
    async def save_co_occurrence_stats(self, co_occurrence_data: Dict[int, Dict[Tuple[int, int], float]]) -> bool:
        """Save co-occurrence statistics with retry logic and backup."""
        async with self.save_lock:
            try:
                # Create backup
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_data = {
                    str(guild_id): {
                        f"{m1},{m2}": duration
                        for (m1, m2), duration in pairs.items()
                    }
                    for guild_id, pairs in co_occurrence_data.items()
                }
                
                backup_success = self._save_local_backup(
                    backup_data,
                    f"co_occurrence_{timestamp}.json"
                )
                
                if not backup_success:
                    logging.warning("Failed to create backup before saving to MongoDB")
                
                # Save to MongoDB using individual operations
                collection = self.async_db[self.co_occurrence_collection]
                
                success_count = 0
                for guild_id, pairs in co_occurrence_data.items():
                    try:
                        serialized_pairs = {
                            f"{m1},{m2}": duration
                            for (m1, m2), duration in pairs.items()
                        }
                        
                        result = await collection.update_one(
                            {'guild_id': guild_id},
                            {
                                '$set': {
                                    'pairs': serialized_pairs, 
                                    'updated_at': datetime.utcnow()
                                }
                            },
                            upsert=True
                        )
                        success_count += 1
                        
                    except PyMongoError as e:
                        logging.error(f"Failed to save co-occurrence stats for guild {guild_id}: {e}")
                        continue
                
                logging.info(f"Co-occurrence stats saved to MongoDB: {success_count}/{len(co_occurrence_data)} guilds")
                return success_count > 0
                
            except PyMongoError as e:
                logging.error(f"MongoDB error saving co-occurrence stats: {e}")
                raise
            except Exception as e:
                logging.error(f"Unexpected error saving co-occurrence stats: {e}", exc_info=True)
                return False

    def load_voice_stats(self) -> Dict[int, Dict[int, Dict[str, float]]]:
        """Load voice statistics from backup or MongoDB."""
        voice_stats: Dict[int, Dict[int, Dict[str, float]]] = {}
        
        try:
            # Try loading from latest backup first
            latest_backup = self._find_latest_backup("voice_stats")
            if latest_backup:
                backup_data = self._load_local_backup(latest_backup)
                if backup_data:
                    # Convert string keys back to integers
                    for guild_id_str, members in backup_data.items():
                        try:
                            guild_id = int(guild_id_str)
                            voice_stats[guild_id] = {}
                            
                            for member_id_str, stats in members.items():
                                try:
                                    member_id = int(member_id_str)
                                    voice_stats[guild_id][member_id] = stats
                                except ValueError:
                                    logging.warning(f"Invalid member ID '{member_id_str}' in backup")
                                    
                        except ValueError:
                            logging.warning(f"Invalid guild ID '{guild_id_str}' in backup")
                    
                    if voice_stats:
                        logging.info(f"Loaded voice stats from backup: {latest_backup}")
                        return voice_stats
            
            # Fall back to MongoDB
            collection = self.sync_db[self.voice_stats_collection]
            for doc in collection.find():
                guild_id = doc['guild_id']
                members = doc.get('members', {})
                
                voice_stats[guild_id] = {}
                for member_id_str, stats in members.items():
                    try:
                        member_id = int(member_id_str)
                        voice_stats[guild_id][member_id] = stats
                    except ValueError:
                        logging.warning(f"Invalid member ID '{member_id_str}' in MongoDB")
            
            logging.info(f"Loaded voice stats from MongoDB: {len(voice_stats)} guilds")
            
        except Exception as e:
            logging.error(f"Error loading voice stats: {e}", exc_info=True)
        
        return voice_stats

    def load_co_occurrence_stats(self) -> Dict[int, Dict[Tuple[int, int], float]]:
        """Load co-occurrence statistics from backup or MongoDB."""
        co_occurrence_stats: Dict[int, Dict[Tuple[int, int], float]] = {}
        
        try:
            # Try loading from latest backup first
            latest_backup = self._find_latest_backup("co_occurrence")
            if latest_backup:
                backup_data = self._load_local_backup(latest_backup)
                if backup_data:
                    loaded_from_backup = self._parse_co_occurrence_data(backup_data, co_occurrence_stats, "backup")
                    if loaded_from_backup:
                        logging.info(f"Loaded co-occurrence stats from backup: {latest_backup}")
                        return co_occurrence_stats
            
            # Fall back to MongoDB
            collection = self.sync_db[self.co_occurrence_collection]
            mongo_data = {}
            
            for doc in collection.find():
                guild_id = doc['guild_id']
                pairs = doc.get('pairs', {})
                mongo_data[str(guild_id)] = pairs
            
            if mongo_data:
                self._parse_co_occurrence_data(mongo_data, co_occurrence_stats, "MongoDB")
                logging.info(f"Loaded co-occurrence stats from MongoDB: {len(co_occurrence_stats)} guilds")
            
        except Exception as e:
            logging.error(f"Error loading co-occurrence stats: {e}", exc_info=True)
        
        return co_occurrence_stats

    def _parse_co_occurrence_data(
        self, 
        data: Dict[str, Dict[str, float]], 
        output: Dict[int, Dict[Tuple[int, int], float]],
        source: str
    ) -> bool:
        """Parse co-occurrence data from backup or MongoDB format."""
        success = False
        
        for guild_id_str, pairs_dict in data.items():
            try:
                guild_id = int(guild_id_str)
                guild_data: Dict[Tuple[int, int], float] = {}
                
                if not isinstance(pairs_dict, dict):
                    logging.warning(f"Invalid pairs data for guild {guild_id_str} from {source}")
                    continue
                
                for pair_key, duration in pairs_dict.items():
                    try:
                        if not isinstance(pair_key, str) or ',' not in pair_key:
                            logging.warning(f"Invalid pair key '{pair_key}' from {source}")
                            continue
                        
                        parts = pair_key.split(',')
                        if len(parts) != 2:
                            logging.warning(f"Invalid pair key format '{pair_key}' from {source}")
                            continue
                        
                        m1, m2 = int(parts[0]), int(parts[1])
                        duration_float = float(duration)
                        
                        if duration_float > 0:
                            # Ensure consistent ordering
                            pair_tuple = tuple(sorted((m1, m2)))
                            guild_data[pair_tuple] = duration_float
                            
                    except (ValueError, TypeError) as e:
                        logging.warning(f"Error parsing pair '{pair_key}' from {source}: {e}")
                
                if guild_data:
                    output[guild_id] = guild_data
                    success = True
                    
            except ValueError:
                logging.warning(f"Invalid guild ID '{guild_id_str}' from {source}")
            except Exception as e:
                logging.error(f"Error processing guild {guild_id_str} from {source}: {e}")
        
        return success

    def close(self):
        """Close database connections."""
        try:
            self.sync_client.close()
            logging.info("Closed sync MongoDB connection")
        except Exception as e:
            logging.error(f"Error closing sync MongoDB connection: {e}")

    async def aclose(self):
        """Close async database connections."""
        try:
            self.async_client.close()
            logging.info("Closed async MongoDB connection")
        except Exception as e:
            logging.error(f"Error closing async MongoDB connection: {e}")

# Optional: Provide a global instance if preferred, though dependency injection is generally better.
# db_manager = DatabaseManager() 