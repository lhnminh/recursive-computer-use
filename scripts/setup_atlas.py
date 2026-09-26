"""Create the MongoDB indexes used by the recursive harness."""

from __future__ import annotations

import os

from dotenv import load_dotenv
from pymongo import ASCENDING, MongoClient
from pymongo.operations import SearchIndexModel

from recursive_computer_use.evolution.embedding import EMBEDDING_DIMENSIONS


load_dotenv()
uri = os.environ["MONGODB_URI"]
database = os.environ.get("MONGODB_DB", "recursive_computer_use")
client = MongoClient(uri)
db = client[database]

db.experiences.create_index([("task_key", ASCENDING), ("created_at", ASCENDING)])
db.policies.create_index(
    [("task_key", ASCENDING), ("version", ASCENDING)], unique=True
)
db.evaluations.create_index([("task_key", ASCENDING), ("created_at", ASCENDING)])

model = SearchIndexModel(
    definition={
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": EMBEDDING_DIMENSIONS,
                "similarity": "cosine",
            },
            {"type": "filter", "path": "task_key"},
        ]
    },
    name="experience_embedding",
    type="vectorSearch",
)
try:
    db.experiences.create_search_index(model=model)
    print("Created Atlas Vector Search index: experience_embedding")
except Exception as exc:
    print(f"Vector index may already exist or still be provisioning: {exc}")
finally:
    client.close()
