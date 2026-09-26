from __future__ import annotations

import threading
import time
import unittest
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import OperationFailure

from recursive_computer_use.evolution.feed import PolicyFeed


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


class FakeStream:
    def __init__(self, event):
        self.event = event
        self.resume_token = {"_data": "token-1"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def try_next(self):
        event, self.event = self.event, None
        return event


class FakePolicies:
    def __init__(self, events):
        self.events = list(events)
        self.watch_args = None
        self.watch_calls = []

    def watch(self, **kwargs):
        self.watch_args = kwargs
        self.watch_calls.append(kwargs)
        return FakeStream(self.events.pop(0))


class FakeState:
    def __init__(self, token=None):
        self.token = token
        self.updated = None

    def find_one(self, query):
        return {"_id": query["_id"], "resume_token": self.token} if self.token else None

    def update_one(self, query, update, upsert=False):
        self.updated = update["$set"]
        self.token = self.updated["resume_token"]


class FakeDB:
    def __init__(self, events, token=None):
        self.collections = {
            "policies": FakePolicies(events),
            "agent_state": FakeState(token),
        }

    def __getitem__(self, name):
        return self.collections[name]


class PolicyFeedTests(unittest.TestCase):
    def test_delivers_policy_and_persists_resume_token(self):
        policy = {"task_key": "task", "version": 2, "status": "accepted"}
        db = FakeDB([{"fullDocument": policy, "_id": {"_data": "token-1"}}])
        received = threading.Event()
        feed = PolicyFeed(db, "agent-a")
        feed.watch("task", lambda doc: received.set() if doc == policy else None)
        self.assertTrue(received.wait(1))
        feed.stop()
        self.assertEqual(db["agent_state"].token, {"_data": "token-1"})

    def test_restart_resumes_from_saved_token(self):
        first = {"task_key": "task", "version": 2, "status": "accepted"}
        missed = {"task_key": "task", "version": 3, "status": "accepted"}
        db = FakeDB(
            [
                {"fullDocument": first, "_id": {"_data": "token-1"}},
                {"fullDocument": missed, "_id": {"_data": "token-2"}},
            ]
        )
        received_first = threading.Event()
        feed = PolicyFeed(db, "agent-a")
        feed.watch("task", lambda _: received_first.set())
        self.assertTrue(received_first.wait(1))
        feed.stop()

        received_missed = threading.Event()
        restarted = PolicyFeed(db, "agent-a")
        restarted.watch("task", lambda doc: received_missed.set() if doc == missed else None)
        self.assertTrue(received_missed.wait(1))
        restarted.stop()
        self.assertEqual(db["policies"].watch_calls[1]["resume_after"], {"_data": "token-1"})

    def test_unsupported_change_stream_becomes_noop(self):
        class UnsupportedPolicies:
            def watch(self, **kwargs):
                raise OperationFailure("change streams are not supported on standalone", code=40573)

        class DB:
            def __getitem__(self, name):
                return UnsupportedPolicies() if name == "policies" else FakeState()

        feed = PolicyFeed(DB(), "agent-a")
        with self.assertLogs("recursive_computer_use.evolution.feed", level="WARNING"):
            feed.watch("task", lambda _: None)
            feed.stop()
        self.assertFalse(feed._thread.is_alive())


@unittest.skipUnless(os.environ.get("MONGODB_URI"), "set MONGODB_URI to run Atlas integration")
class AtlasPolicyFeedTests(unittest.TestCase):
    def test_restart_receives_policy_accepted_while_offline(self):
        client = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=5000)
        db = client["rcu_test_codex"]
        suffix = uuid.uuid4().hex
        task_key = f"codex-feed-{suffix}"
        agent_id = f"codex-agent-{suffix}"
        policies = db["policies"]
        events = [threading.Event(), threading.Event()]
        received: list[int] = []
        try:
            for version in (1, 2):
                policies.insert_one(
                    {
                        "task_key": task_key,
                        "version": version,
                        "parent_version": version - 1 if version > 1 else None,
                        "status": "candidate",
                        "rules": ["Inspect the screen before acting."],
                        "limits": {},
                        "reason": "Feed integration check.",
                        "created_at": datetime.now(timezone.utc),
                    }
                )

            def receive(index: int):
                def callback(policy):
                    received.append(policy["version"])
                    events[index].set()
                return callback

            first_feed = PolicyFeed(db, agent_id)
            first_feed.watch(task_key, receive(0))
            time.sleep(0.5)
            policies.update_one(
                {"task_key": task_key, "version": 1}, {"$set": {"status": "accepted"}}
            )
            self.assertTrue(events[0].wait(10))
            first_feed.stop()

            policies.update_one(
                {"task_key": task_key, "version": 2}, {"$set": {"status": "accepted"}}
            )
            restarted = PolicyFeed(db, agent_id)
            restarted.watch(task_key, receive(1))
            self.assertTrue(events[1].wait(10))
            restarted.stop()
            self.assertEqual(received, [1, 2])
        finally:
            db["agent_state"].delete_one({"_id": agent_id})
            policies.delete_many({"task_key": task_key})
            client.close()


if __name__ == "__main__":
    unittest.main()
