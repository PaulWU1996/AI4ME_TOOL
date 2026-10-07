import os
import urllib.parse
from functools import lru_cache

from pymongo import MongoClient
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


def store_obj(doc_id: str, data: dict, collection_name: str = "gemma_audio_analysis") -> None:
    client = get_mongodb_client()
    collection = client.get_default_database()[collection_name]
    collection.replace_one({"_id": doc_id}, data, upsert=True)
