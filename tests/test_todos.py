import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
from test_partner_desk import FakeS3, lf  # noqa: E402

TODOS = ("full-pipeline-cache", "daily-brief-todos.json")


def _client(s3):
    return mock.patch.object(lf.boto3, "client", lambda *a, **k: s3)


class TodosTests(unittest.TestCase):
    def test_first_load_seeds_and_persists(self):
        s3 = FakeS3()
        with _client(s3):
            data = lf._load_todos()
        self.assertEqual([i["text"] for i in data["items"]], lf.TODOS_SEED)
        self.assertTrue(all(not i["done"] and i["done_at"] is None for i in data["items"]))
        self.assertEqual(s3.puts[0]["ContentType"], "application/json")

    def test_broken_file_returns_empty_no_seed(self):
        s3 = FakeS3({TODOS: b"{not json"})
        with _client(s3):
            self.assertEqual(lf._load_todos(), {"items": []})
        self.assertEqual(s3.puts, [])

    def test_add_toggle_delete(self):
        s3 = FakeS3({TODOS: json.dumps({"items": []}).encode()})
        with _client(s3):
            r = lf._handle_todo({"op": "add", "text": "  hello  "})
            items = json.loads(r["body"])["items"]
            self.assertEqual(items[0]["text"], "hello")
            tid = items[0]["id"]
            r = lf._handle_todo({"op": "toggle", "id": tid, "done": True})
            it = json.loads(r["body"])["items"][0]
            self.assertTrue(it["done"])
            self.assertIsNotNone(it["done_at"])
            r = lf._handle_todo({"op": "toggle", "id": tid, "done": False})
            self.assertIsNone(json.loads(r["body"])["items"][0]["done_at"])
            r = lf._handle_todo({"op": "delete", "id": tid})
            self.assertEqual(json.loads(r["body"])["items"], [])

    def test_validation(self):
        s3 = FakeS3({TODOS: json.dumps({"items": []}).encode()})
        with _client(s3):
            self.assertEqual(lf._handle_todo({"op": "add", "text": "  "})["statusCode"], 400)
            self.assertEqual(lf._handle_todo({"op": "add", "text": "x" * 301})["statusCode"], 400)
            self.assertEqual(lf._handle_todo({"op": "toggle", "id": "nope", "done": True})["statusCode"], 400)
            self.assertEqual(lf._handle_todo({"op": "bogus"})["statusCode"], 400)

    def test_box_escapes_text(self):
        s3 = FakeS3({TODOS: json.dumps({"items": [
            {"id": "a1", "text": "<b>x</b>", "done": False, "created": "2026-10-28", "done_at": None}
        ]}).encode()})
        with _client(s3):
            html = lf._render_todos_box()
        self.assertNotIn("<b>x</b>", html)
        self.assertIn(">To-dos</h1>", html)


if __name__ == "__main__":
    unittest.main()
