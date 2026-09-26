from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from pymongo.errors import ServerSelectionTimeoutError

from recursive_computer_use import agent
from recursive_computer_use.store import Action, ActionStore


class FakeCollection:
    def __init__(self):
        self.inserted = []
        self.updated = []
        self.indexes = []

    def create_index(self, keys, **kwargs):
        self.indexes.append((keys, kwargs))

    def insert_one(self, document):
        self.inserted.append(dict(document))
        return object()

    def update_one(self, query, update):
        self.updated.append((query, update))
        return object()


class FakeDatabase:
    def __init__(self):
        self.collections = {
            "runs": FakeCollection(),
            "actions": FakeCollection(),
        }

    def __getitem__(self, name):
        return self.collections[name]


class FakeClient:
    def __init__(self):
        self.database = FakeDatabase()
        self.admin = self
        self.pinged = False
        self.closed = False

    def command(self, name):
        self.pinged = name == "ping"

    def __getitem__(self, _name):
        return self.database

    def close(self):
        self.closed = True


class StoreTests(unittest.TestCase):
    def test_connect_and_run_lifecycle(self):
        client = FakeClient()
        with patch("recursive_computer_use.store.MongoClient", return_value=client):
            store = ActionStore.connect("mongodb://example", "test_db")

        run_id = store.start_run("demo task", "test-model")
        store.record_action(
            Action(run_id=run_id, turn=1, seq=store.next_seq(), kind="click", x=1, y=2)
        )
        store.finish_run(run_id, "completed", "done")

        self.assertTrue(store.enabled)
        self.assertTrue(client.pinged)
        self.assertEqual(client.database.collections["runs"].inserted[0]["run_id"], run_id)
        self.assertEqual(client.database.collections["actions"].inserted[0]["kind"], "click")
        finish = client.database.collections["runs"].updated[0][1]["$set"]
        self.assertEqual(finish["status"], "completed")
        self.assertEqual(finish["final_text"], "done")

    def test_connection_failure_returns_disabled_store(self):
        with patch(
            "recursive_computer_use.store.MongoClient",
            side_effect=ServerSelectionTimeoutError("offline"),
        ):
            store = ActionStore.connect("mongodb://offline", "test_db")

        self.assertFalse(store.enabled)
        run_id = store.start_run("task", "model")
        self.assertTrue(run_id)
        store.finish_run(run_id, "failed")


class FakeRunStore:
    def __init__(self):
        self.started = []
        self.finished = []

    def start_run(self, prompt, model):
        self.started.append((prompt, model))
        return "run-1"

    def finish_run(self, run_id, status, final_text=None):
        self.finished.append((run_id, status, final_text))

    def next_seq(self):
        return 1

    def record_action(self, _action):
        return None


class FakeSandbox:
    def __init__(self, *, store):
        self.store = store

    def set_action_context(self, _run_id, _turn):
        return None


class FinalMessage:
    tool_calls = None
    content = "finished"

    def model_dump(self, **_kwargs):
        return {"role": "assistant", "content": self.content}


class FakeCompletionClient:
    def __init__(self, *, error=None):
        self.error = error
        self.chat = SimpleNamespace(completions=self)

    def create(self, **_kwargs):
        if self.error:
            raise self.error
        return SimpleNamespace(choices=[SimpleNamespace(message=FinalMessage())])


class AgentLifecycleTests(unittest.TestCase):
    def run_with_client(self, client, store):
        with (
            patch.object(
                agent,
                "resolve_auth",
                return_value=SimpleNamespace(api_key="test", base_url=None),
            ),
            patch.object(agent, "OpenAI", return_value=client),
            patch.object(agent, "Sandbox", FakeSandbox),
        ):
            return agent.run("task", model="model", action_store=store)

    def test_successful_run_is_finished(self):
        store = FakeRunStore()

        result = self.run_with_client(FakeCompletionClient(), store)

        self.assertEqual(result, "finished")
        self.assertEqual(store.started, [("task", "model")])
        self.assertEqual(store.finished, [("run-1", "completed", "finished")])

    def test_failed_run_is_finished(self):
        store = FakeRunStore()

        with self.assertRaisesRegex(RuntimeError, "model failed"):
            self.run_with_client(
                FakeCompletionClient(error=RuntimeError("model failed")), store
            )

        self.assertEqual(store.finished, [("run-1", "failed", None)])


if __name__ == "__main__":
    unittest.main()
