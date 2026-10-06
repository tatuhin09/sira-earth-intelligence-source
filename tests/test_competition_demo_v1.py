from pathlib import Path
import tempfile, unittest
from unittest.mock import patch
from sira.competition_demo import build_public_snapshot, render_html, run_demo_server
class FakeControl:
    def __init__(self,root): pass
    def overview(self): return {"core":{"identity":{"name":"SIRA"},"runtime":{"effective_state":"stopped","state_health":"ok","generation":70,"worker_state":"stopped","state_file":"/home/private/runtime.json"},"release":{"release_ready":True,"required_check_count":19,"required_checks_passed":19},"knowledge":{"verified_count":42,"conflicted_count":1,"stale_sample_count":2},"learning":{"active_goal_count":3,"goals":[{"topic":"private topic"}]},"providers":{"known_count":5,"available_count":3,"cooling_count":1,"entries":[{"credential_state":"secret-key"}]},"capabilities":[{"name":"research","state":"partially_demonstrated","scope":"bounded research","evidence_refs":["private-id"]}],"last_task":{"task_id":"private-task","intent":"private request","status":"completed","route":"verified_memory","verification_status":"verified"},"promotion_authorized":False,"paid_spending_authorized":False,"skill_activated":False}}
class DemoTests(unittest.TestCase):
    def test_snapshot_sanitized(self):
        with tempfile.TemporaryDirectory() as t, patch("sira.competition_demo.DesktopControl",FakeControl): d=build_public_snapshot(Path(t))
        s=repr(d); self.assertEqual(d["runtime"]["generation"],70); self.assertEqual(d["knowledge"]["verified_count"],42)
        for x in ("private topic","private request","secret-key","/home/private","private-id","private-task"): self.assertNotIn(x,s)
        self.assertFalse(d["authority"]["public_mutation_endpoints"])
    def test_html_no_owner_controls(self):
        with tempfile.TemporaryDirectory() as t, patch("sira.competition_demo.DesktopControl",FakeControl): h=render_html(build_public_snapshot(Path(t))).lower()
        for x in ("/api/runtime/start","/api/runtime/stop","/api/chat","/api/research"): self.assertNotIn(x,h)
    def test_non_loopback_refused(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(ValueError): run_demo_server(Path(t),host="0.0.0.0",port=0)
if __name__=="__main__": unittest.main()
