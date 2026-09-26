from __future__ import annotations

import unittest
from unittest.mock import patch

from dashboard import app


class NetworkDashboardTests(unittest.TestCase):
    def test_renders_recipe_site_map_and_execution_comparison(self):
        data = {
            "runs": [],
            "experiences": [],
            "policies": [],
            "evaluations": [],
            "learning_curve": [],
            "lineage": [],
            "top_lessons": [],
            "skills": [],
            "recipes": [{
                "name": "local:checkin",
                "scope": {"site": "127.0.0.1:8765"},
                "version": 2,
                "status": "active",
                "uses": 5,
                "wins": 4,
                "lift": 0.6,
            }],
            "site_map": [{
                "site": "127.0.0.1:8765",
                "start": "GET /api/session",
                "chain": [{"from": "GET /api/session", "to": "POST /api/checkin"}],
            }],
            "episodes": [
                {"task": "Check in guest", "mode": "api_recipe", "outcome": "success", "duration_ms": 38, "llm_calls": 1},
                {"task": "Check in guest", "mode": "computer_use", "outcome": "success", "duration_ms": 14000, "llm_calls": 9},
            ],
        }
        with patch.object(app, "load_data", return_value=(data, "test fixture")):
            body = app.render().decode()

        self.assertIn("API recipes", body)
        self.assertIn("local:checkin", body)
        self.assertIn("GET /api/session", body)
        self.assertIn("POST /api/checkin", body)
        self.assertIn("Execution cost by path", body)
        self.assertIn("Mean model calls", body)
        self.assertIn("14000 ms", body)
        self.assertIn("Recent runs", body)


if __name__ == "__main__":
    unittest.main()
