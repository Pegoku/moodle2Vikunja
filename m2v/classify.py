"""Ask an OpenAI model what kind of item each Moodle event is.

The model picks a category (stored as a Vikunja label) and a priority. Labels it
has used before are marked in their Vikunja description and sent back with every
request, so the model keeps reusing them instead of inventing near-duplicates.
"""

import html
import json
import logging
import re

import requests

log = logging.getLogger("m2v")

CATEGORY_MARKER = "m2v:category"

SYSTEM_PROMPT = """You sort a vocational-training student's Moodle calendar items into categories.
The items are usually in Catalan or Spanish.

For every item return:
- category: a short Title Case English label. Reuse one of the existing categories
  exactly as written whenever it fits. Only invent a new one if none fits at all.
- priority (Vikunja scale):
  4 = graded work the student has to hand in (assignments, tasks, projects, deliverables)
  3 = quizzes and exams
  2 = video calls, live classes, tutoring sessions, other things to attend
  1 = informational dates (delivery period opens/closes, course start, holidays)
Hand-in tasks are the most important things; everything else ranks below them.

Use the module and event_type fields, they are reliable:
- module "assign" is a hand-in task (priority 4).
- module "quiz" with event_type "close" is the quiz deadline (priority 3).
- Items without a module are course calendar entries. "Inici"/"Finalització"/"Inicio"/
  "Fin" of a "lliurament"/"entrega" marks a delivery period starting or ending: that
  is an informational date (priority 1), not a hand-in task.
Classify identical kinds of items identically."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "category", "priority"],
                "properties": {
                    "id": {"type": "integer"},
                    "category": {"type": "string"},
                    "priority": {"type": "integer", "enum": [1, 2, 3, 4]},
                },
            },
        }
    },
}


def plain(text: str, limit: int = 300) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def describe(event_id: int, ev: dict | None, title: str) -> dict:
    ev = ev or {}
    return {
        "id": event_id,
        "title": ev.get("name") or title,
        "course": (ev.get("course") or {}).get("fullname", ""),
        "module": ev.get("modulename") or "",
        "event_type": ev.get("eventtype") or "",
        "description": plain(ev.get("description", "")),
    }


class Classifier:
    def __init__(self, api_key: str, model: str, base_url: str):
        self.model = model
        self.url = f"{base_url.rstrip('/')}/chat/completions"
        self.s = requests.Session()
        self.s.headers["Authorization"] = f"Bearer {api_key}"

    def classify(self, items: list[dict], categories: list[str]) -> dict[int, dict]:
        """Return {event id: {"category": ..., "priority": ...}} for the given items."""
        out: dict[int, dict] = {}
        known = list(categories)
        for i in range(0, len(items), 25):
            batch = items[i:i + 25]
            for res in self._call(batch, known):
                out[res["id"]] = res
                if not any(res["category"].casefold() == k.casefold() for k in known):
                    known.append(res["category"])
        return out

    def _call(self, batch: list[dict], categories: list[str]) -> list[dict]:
        user = (
            f"Existing categories: {json.dumps(categories, ensure_ascii=False)}\n\n"
            f"Items:\n{json.dumps(batch, ensure_ascii=False, indent=1)}"
        )
        r = self.s.post(self.url, timeout=120, json={
            "model": self.model,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "classification", "strict": True, "schema": SCHEMA}},
        })
        if not r.ok:
            raise RuntimeError(f"OpenAI request failed: {r.status_code} {r.text[:300]}")
        content = r.json()["choices"][0]["message"]["content"]
        ids = {it["id"] for it in batch}
        return [res for res in json.loads(content)["items"] if res["id"] in ids and res["category"].strip()]
