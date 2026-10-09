import os
import urllib.parse
from functools import lru_cache

from pymongo import MongoClient, ReplaceOne
from pymongo.errors import ConnectionFailure


@lru_cache(maxsize=1)
def get_mongodb_client() -> MongoClient:
    db_username = os.environ.get("MONGO_MACHINE_USER", "")
    db_password = os.environ.get("MONGO_MACHINE_PASSWORD", "")
    db_host = os.environ.get("MONGO_HOST", "")
    db_port = os.environ.get("MONGO_PORT", "")
    db_name = os.environ.get("MONGO_DATABASE", "")

    if not all([db_username, db_password, db_host, db_port, db_name]):
        raise ValueError("MongoDB environment variables are not set.")

    escaped_username = urllib.parse.quote_plus(db_username)
    escaped_password = urllib.parse.quote_plus(db_password)

    connection_string = (
        f"mongodb://{escaped_username}:{escaped_password}@{db_host}:{db_port}/{db_name}"
    )

    client = MongoClient(
        connection_string,
        serverSelectionTimeoutMS=5000,
        socketTimeoutMS=30000,
    )
    try:
        client.admin.command("ping")
    except ConnectionFailure as e:
        client.close()
        print("Failed to connect to MongoDB. Check your credentials, IP, and firewall settings.")
        print(f"Error details: {e}")
        raise

    print(f"Successfully connected to the '{db_name}' database at {db_host}:{db_port}!")
    return client


def ensure_available() -> None:
    """Fail fast if MONGO_* config is missing or the server is unreachable.

    Raises ValueError (config) or a PyMongo connection error. Safe to call
    repeatedly — the client is cached after the first successful ping.
    """
    get_mongodb_client()


def find_scenes(program_id: str, window_start: float, window_end: float | None) -> list[dict]:
    """Scenes of `program_id` overlapping `[window_start, window_end)`, in programme seconds.

    `window_end=None` means the window runs to the end of the programme.
    """
    client = get_mongodb_client()
    collection = client.get_default_database()[os.environ.get("MONGO_SCENES_COLLECTION", "scenes")]
    query: dict = {"programme_id": program_id, "end_time": {"$gt": window_start}}
    if window_end is not None:
        query["start_time"] = {"$lt": window_end}
    projection = {"_id": 0, "scene_id": 1, "start_time": 1, "end_time": 1}
    return list(collection.find(query, projection).sort("start_time", 1))


def store_scene_results(docs: list[dict], collection_name: str = "gemma_audio_analysis") -> None:
    """Upsert one doc per (program_id, scene_id).

    Mongo generates the ObjectId `_id` on first insert; re-running a job
    replaces the scene's doc instead of duplicating it.
    """
    if not docs:
        return
    client = get_mongodb_client()
    collection = client.get_default_database()[collection_name]
    collection.bulk_write(
        [
            ReplaceOne(
                {"program_id": doc["program_id"], "scene_id": doc["scene_id"]},
                doc,
                upsert=True,
            )
            for doc in docs
        ]
    )
