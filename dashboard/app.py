"""Read-only local dashboard for run, memory, policy, and evaluation evidence."""

from __future__ import annotations

import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from recursive_computer_use.evolution import (
    annealed_edit_budget,
    verify_evaluation_evidence,
)


HOST, PORT = "127.0.0.1", 8787
FIXTURES = Path(__file__).with_name("fixtures.json")


def load_data() -> tuple[dict[str, list[dict[str, Any]]], str]:
    uri = os.environ.get("MONGODB_URI")
    if uri:
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=1500)
            client.admin.command("ping")
            db = client[os.environ.get("MONGODB_DB", "recursive_computer_use")]
            data = {}
            for name in ("runs", "experiences", "policies", "evaluations"):
                data[name] = list(
                    db[name].find({}, {"_id": 0, "embedding": 0}).sort("created_at", -1).limit(20)
                )
            client.close()
            if any(data.values()):
                return data, "MongoDB Atlas"
        except Exception:
            pass
    return json.loads(FIXTURES.read_text(encoding="utf-8")), "offline fixtures"


def metric_cards(runs: list[dict[str, Any]]) -> str:
    cards = []
    for run in sorted(runs, key=lambda item: item.get("policy_version", 0))[-2:]:
        metrics = run.get("verified_metrics", {})
        success = "PASS" if metrics.get("success_rate") else "FAIL"
        cards.append(
            f"<article><h3>Policy v{run.get('policy_version','?')} <span class='{success.lower()}'>{success}</span></h3>"
            f"<b>{metrics.get('wrong_field_entries',0)}</b> wrong fields · "
            f"<b>{metrics.get('wrong_clicks',0)}</b> wrong clicks · "
            f"<b>{metrics.get('action_count',0)}</b> actions</article>"
        )
    return "".join(cards) or "<article>No verified runs yet.</article>"


def render() -> bytes:
    data, source = load_data()
    policies = sorted(data.get("policies", []), key=lambda item: item.get("version", 0))
    newest = policies[-1] if policies else {}
    previous = policies[-2] if len(policies) > 1 else {}
    added = [rule for rule in newest.get("rules", []) if rule not in previous.get("rules", [])]
    lesson = (data.get("experiences") or [{}])[0].get("lesson", "No memory recorded")
    evaluation = (data.get("evaluations") or [{}])[0]
    evidence_sealed = verify_evaluation_evidence(evaluation)
    evidence_label = "VERIFIED" if evidence_sealed else "UNSEALED"
    evidence_class = "pass" if evidence_sealed else "muted"
    next_budget = annealed_edit_budget(int(newest.get("version", 1) or 1))
    body = f"""<!doctype html><html><head><meta charset='utf-8'><meta http-equiv='refresh' content='5'>
<title>Recursive Harness</title><style>
body{{font-family:Inter,system-ui;background:#09111f;color:#ecf2ff;margin:0;padding:36px}}main{{max-width:1050px;margin:auto}}h1{{font-size:42px;margin-bottom:4px}}.muted{{color:#94a3b8}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:18px;margin:26px 0}}article,section{{background:#111c31;border:1px solid #293854;border-radius:16px;padding:22px}}.pass{{color:#4ade80}}.fail{{color:#fb7185}}code{{color:#c4b5fd}}.flow{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.flow div{{background:#182641;border-radius:12px;padding:15px}}@media(max-width:800px){{.grid,.flow{{grid-template-columns:1fr}}}}
</style></head><body><main><div class='muted'>LIVE SOURCE: {html.escape(source)}</div><h1>Recursive Harness</h1><p class='muted'>Local execution. Redacted Atlas memory. Verified policy evolution.</p>
<div class='grid'>{metric_cards(data.get('runs', []))}</div>
<div class='flow'><div>1. Verified failure</div><div>2. Atlas memory</div><div>3. Policy candidate</div><div>4. Regression gate</div></div>
<div class='grid'><section><h2>Retrieved lesson</h2><p>{html.escape(str(lesson))}</p></section>
<section><h2>Policy diff</h2><p>v{previous.get('version','?')} → v{newest.get('version','?')}</p><p class='pass'>+ {html.escape(' | '.join(added) or 'No added rule')}</p></section>
<section><h2>Evaluation</h2><h3 class='{str(evaluation.get('decision','')).lower()}'>{html.escape(str(evaluation.get('decision','pending')).upper())}</h3><p>{html.escape(str(evaluation.get('reason','Awaiting a candidate trial.')))}</p><p class='{evidence_class}'>Evidence: {evidence_label}</p></section>
<section><h2>Regularization</h2><p>Next proposal budget: <b>{next_budget}</b> independent edit(s).</p><p>Task-specific literals and bundled mature-policy changes are rejected before execution.</p></section>
<section><h2>Privacy boundary</h2><p>Atlas receives no screenshot bytes or typed content, only redacted summaries, metrics, policies, and text selected for Atlas Automated Embedding. Selected screenshots may transit to the configured model.</p></section></div>
</main></body></html>"""
    return body.encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/":
            self.send_error(404)
            return
        body = render()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    print(f"Dashboard: http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
