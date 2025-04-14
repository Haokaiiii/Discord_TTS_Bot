import logging
import json
import os
import glob
from datetime import datetime
from pymongo import MongoClient
import backoff
from asyncio import Lock

from utils.config import MONGODB_URI, BACKUP_DIR

class DatabaseManager:
    def __init__(self):
        self.client = MongoClient(MONGODB_URI)
        self.db = self.client['discord_bot']
        self.voice_stats_col = self.db['voice_stats']
        self.co_occurrence_col = self.db['co_occurrence_stats']
        self.save_lock = Lock()
        logging.info("DatabaseManager initialized and connected to MongoDB.")

    def _save_local_backup(self, data, filename):
        """Saves data to a local backup file."""
        backup_path = os.path.join(BACKUP_DIR, filename)
        try:
            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=4)
            logging.info(f"Local backup created: {backup_path}")
        except IOError as e:
            logging.error(f"Failed to create local backup {backup_path}: {e}")

    def _load_local_backup(self, filename):
        """Loads data from a local backup file."""
        backup_path = os.path.join(BACKUP_DIR, filename)
        if os.path.exists(backup_path):
            try:
                with open(backup_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (IOError, json.JSONDecodeError) as e:
                logging.error(f"Failed to load local backup {backup_path}: {e}")
        return None

    @backoff.on_exception(backoff.expo, Exception, max_tries=5, max_time=300)
    async def save_voice_stats(self, voice_stats_data: dict):
        async with self.save_lock:
            logging.debug("Acquired lock for saving voice stats.")
            try:
                # Prepare data for backup (convert keys to strings)
                backup_data = {
                    str(guild_id): {
                        str(member_id): data
                        for member_id, data in members.items()
                    }
                    for guild_id, members in voice_stats_data.items()
                }
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                self._save_local_backup(backup_data, f"voice_stats_{timestamp}.json")

                # Save to MongoDB
                for guild_id, members in voice_stats_data.items():
                    serialized_members = {
                        str(member_id): data
                        for member_id, data in members.items()
                    }
                    self.voice_stats_col.update_one(
                        {'guild_id': guild_id},
                        {'$set': {'members': serialized_members}},
                        upsert=True
                    )
                logging.info("Voice stats saved to MongoDB.")
            except Exception as e:
                logging.error(f"Error saving voice stats: {e}", exc_info=True)
                raise
            finally:
                logging.debug("Released lock for saving voice stats.")

    @backoff.on_exception(backoff.expo, Exception, max_tries=5, max_time=300)
    async def save_co_occurrence_stats(self, co_occurrence_data: dict):
        async with self.save_lock:
            logging.debug("Acquired lock for saving co-occurrence stats.")
            try:
                # Prepare data for backup
                backup_data = {
                    str(guild_id): {
                        f"{m1},{m2}": duration
                        for (m1, m2), duration in pairs.items()
                    }
                    for guild_id, pairs in co_occurrence_data.items()
                }
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                self._save_local_backup(backup_data, f"co_occurrence_{timestamp}.json")

                # Save to MongoDB
                for guild_id, pairs in co_occurrence_data.items():
                    serialized_pairs = {
                        f"{m1},{m2}": duration
                        for (m1, m2), duration in pairs.items()
                    }
                    self.co_occurrence_col.update_one(
                        {'guild_id': guild_id},
                        {'$set': {'pairs': serialized_pairs}},
                        upsert=True
                    )
                logging.info("Co-occurrence stats saved to MongoDB.")
            except Exception as e:
                logging.error(f"Error saving co-occurrence stats: {e}", exc_info=True)
                raise
            finally:
                logging.debug("Released lock for saving co-occurrence stats.")

    def load_voice_stats(self) -> dict:
        voice_stats = {}
        try:
            # Try loading from the latest local backup first
            backup_files = sorted(glob.glob(os.path.join(BACKUP_DIR, "voice_stats_*.json")))
            if backup_files:
                latest_backup_path = backup_files[-1]
                backup_data = self._load_local_backup(os.path.basename(latest_backup_path))
                if backup_data:
                    for guild_id_str, members in backup_data.items():
                        try:
                            guild_id = int(guild_id_str)
                            voice_stats[guild_id] = {
                                int(member_id): data
                                for member_id, data in members.items()
                            }
                        except ValueError:
                            logging.warning(f"Skipping invalid guild ID '{guild_id_str}' in backup file {latest_backup_path}")
                    logging.info(f"Loaded voice stats from local backup: {latest_backup_path}")
                    return voice_stats
                else:
                    logging.warning(f"Failed to load data from latest backup {latest_backup_path}. Attempting MongoDB.")
            else:
                 logging.info("No local voice stats backups found. Attempting MongoDB.")

            # If backup loading failed or no backups exist, load from MongoDB
            for doc in self.voice_stats_col.find():
                guild_id = doc['guild_id']
                members = doc.get('members', {})
                voice_stats[guild_id] = {
                    int(member_id): data
                    for member_id, data in members.items()
                }
            logging.info("Loaded voice stats from MongoDB.")
        except Exception as e:
            logging.error(f"Error loading voice stats: {e}", exc_info=True)
        return voice_stats

    def load_co_occurrence_stats(self) -> dict:
        co_occurrence_stats = {}
        try:
            # Try loading from the latest local backup first
            backup_files = sorted(glob.glob(os.path.join(BACKUP_DIR, "co_occurrence_*.json")))
            if backup_files:
                latest_backup_path = backup_files[-1]
                logging.info(f"Attempting to load co-occurrence stats from local backup: {latest_backup_path}")
                backup_data = self._load_local_backup(os.path.basename(latest_backup_path))
                if backup_data:
                    loaded_from_backup = False
                    for guild_id_str, pairs_str_dict in backup_data.items():
                        try:
                            guild_id = int(guild_id_str)
                            guild_data = {}
                            if not isinstance(pairs_str_dict, dict):
                                logging.warning(f"Expected a dictionary for pairs in guild {guild_id_str}, got {type(pairs_str_dict)}. Skipping guild in backup.")
                                continue
                            
                            for pair_key_str, duration in pairs_str_dict.items():
                                try:
                                    # Ensure the key is a string and contains a comma
                                    if not isinstance(pair_key_str, str) or ',' not in pair_key_str:
                                        logging.warning(f"Skipping invalid non-string or malformed pair key '{pair_key_str}' (type: {type(pair_key_str)}) in backup for guild {guild_id_str}.")
                                        continue
                                    
                                    m1_str, m2_str = pair_key_str.split(',')
                                    m1 = int(m1_str)
                                    m2 = int(m2_str)
                                    duration_float = float(duration) # Ensure duration is float
                                    # Create the tuple key with integers, sorted
                                    tuple_key = tuple(sorted((m1, m2)))
                                    guild_data[tuple_key] = duration_float
                                    # logging.debug(f"Loaded pair {tuple_key} -> {duration_float} for guild {guild_id}")
                                except (ValueError, TypeError) as e:
                                    logging.warning(f"Skipping invalid pair key components or duration '{pair_key_str}' -> '{duration}' in backup for guild {guild_id_str}. Error: {e}")
                                except Exception as e:
                                    logging.error(f"Unexpected error processing pair key '{pair_key_str}' in backup for guild {guild_id_str}: {e}", exc_info=True)
                                
                            if guild_data: # Only add if we successfully loaded pairs
                                co_occurrence_stats[guild_id] = guild_data
                                loaded_from_backup = True
                                
                        except ValueError:
                            logging.warning(f"Skipping invalid guild ID '{guild_id_str}' in backup file {latest_backup_path}")
                        except Exception as e:
                            logging.error(f"Unexpected error processing guild data for '{guild_id_str}' in backup {latest_backup_path}: {e}", exc_info=True)
                           
                    if loaded_from_backup:
                        logging.info(f"Successfully loaded co-occurrence stats from local backup: {latest_backup_path}")
                        return co_occurrence_stats
                    else:
                        logging.warning(f"Loaded backup file {latest_backup_path}, but no valid data found. Attempting MongoDB.")
                else:
                    logging.warning(f"Failed to load data from latest backup {latest_backup_path}. Attempting MongoDB.")
            else:
                logging.info("No local co-occurrence stats backups found. Attempting MongoDB.")

            # If backup loading failed or no backups/valid data exist, load from MongoDB
            logging.info("Loading co-occurrence stats from MongoDB.")
            mongo_data_loaded = False
            for doc in self.co_occurrence_col.find():
                try:
                    guild_id = doc['guild_id']
                    pairs = doc.get('pairs', {})
                    guild_data = {}
                    if not isinstance(pairs, dict):
                        logging.warning(f"Expected a dictionary for pairs in MongoDB for guild {guild_id}, got {type(pairs)}. Skipping.")
                        continue
                    
                    for pair_str, duration in pairs.items():
                        try:
                            if not isinstance(pair_str, str) or ',' not in pair_str:
                                logging.warning(f"Skipping invalid non-string or malformed pair key '{pair_str}' from MongoDB for guild {guild_id}")
                                continue
                            
                            m1_str, m2_str = pair_str.split(',')
                            m1 = int(m1_str)
                            m2 = int(m2_str)
                            duration_float = float(duration)
                            # Ensure consistent key order (smaller ID first)
                            tuple_key = tuple(sorted((m1, m2)))
                            guild_data[tuple_key] = duration_float
                        except (ValueError, TypeError) as e:
                            logging.warning(f"Skipping invalid pair key components or duration '{pair_str}' -> '{duration}' in MongoDB for guild {guild_id}. Error: {e}")
                        except Exception as e:
                            logging.error(f"Unexpected error processing pair key '{pair_str}' from MongoDB for guild {guild_id}: {e}", exc_info=True)
                            
                    if guild_data:
                        co_occurrence_stats[guild_id] = guild_data
                        mongo_data_loaded = True
                        
                except Exception as e:
                    logging.error(f"Error processing MongoDB document {doc.get('_id', '?')} for co-occurrence stats: {e}", exc_info=True)
                    
            if mongo_data_loaded:
                logging.info("Finished loading co-occurrence stats from MongoDB.")
            else:
                logging.info("No co-occurrence data found in MongoDB.")
                
        except Exception as e:
            logging.error(f"General error loading co-occurrence stats: {e}", exc_info=True)
            
        # Ensure the final structure is sound, even if empty
        final_stats = {}
        for gid, pairs_dict in co_occurrence_stats.items():
            if isinstance(gid, int) and isinstance(pairs_dict, dict):
                final_stats[gid] = {k: v for k, v in pairs_dict.items() if isinstance(k, tuple) and len(k) == 2 and isinstance(k[0], int) and isinstance(k[1], int) and isinstance(v, float)}
            else:
                logging.warning(f"Filtering out invalid guild data during final check: Guild={gid} (type: {type(gid)}) Data Type: {type(pairs_dict)}")
                
        return final_stats

# Optional: Provide a global instance if preferred, though dependency injection is generally better.
# db_manager = DatabaseManager() 