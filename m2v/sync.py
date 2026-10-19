import hashlib
import html
import logging
import re
from datetime import date, datetime, timezone

from .classify import CATEGORY_MARKER, Classifier, describe
from .config import Config
from .courses import abbreviate, course_labels
from .moodle import Grade, Moodle, MoodleError, activity_key
from .vikunja import Vikunja

log = logging.getLogger("m2v")

MARKER_RE = re.compile(r"moodle-event:(\d+):([0-9a-f]+)")
STRIKE = "̶"
NULL_DATE = "0001-01-01T00:00:00Z"
COURSE_MARKER = "m2v:course"


def strike(text: str) -> str:
    return "".join(c + STRIKE for c in text)


def unstrike(text: str) -> str:
    return text.replace(STRIKE, "")


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str | None) -> int | None:
    if not value or value.startswith("0001-"):
        return None
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def label_color(name: str) -> str:
    return hashlib.sha1(name.encode()).hexdigest()[:6]


def link(url: str, text: str) -> str:
    return f'<a href="{html.escape(url, quote=True)}">{html.escape(text)}</a>'


def render_grade(grade: Grade) -> str:
    out = f"<p><strong>Grade:</strong> {html.escape(grade.grade)}"
    if grade.range:
        out += f" <em>({html.escape(grade.range)})</em>"
    out += "</p>"
    if grade.feedback:
        out += f"<blockquote>{html.escape(grade.feedback).replace(chr(10), '<br>')}</blockquote>"
    return out


def render(ev: dict, grade: Grade | None = None) -> dict:
    """Build the Vikunja task fields for a Moodle event (without the marker)."""
    course = ev.get("course") or {}
    action = ev.get("action") or {}
    parts = []
    if grade:
        parts.append(render_grade(grade))
    if course:
        parts.append(f"<p><strong>Course:</strong> {link(course['viewurl'], course['fullname'])}</p>")
    if ev.get("url"):
        parts.append(f"<p><strong>Moodle:</strong> {link(ev['url'], ev.get('activityname') or ev['name'])}</p>")
    if action.get("url") and action["url"] != ev.get("url"):
        parts.append(f"<p><strong>Action:</strong> {link(action['url'], action.get('name') or 'Open')}</p>")
    if ev.get("viewurl"):
        parts.append(f"<p><strong>Calendar:</strong> {link(ev['viewurl'], 'View event in Moodle calendar')}</p>")
    if (ev.get("description") or "").strip():
        parts.append(f"<hr>{ev['description']}")

    fields = {
        "title": ev["name"][:200],
        "description": "".join(parts),
        "due_date": iso(ev["timestart"]),
        "start_date": NULL_DATE,
        "end_date": NULL_DATE,
    }
    if ev.get("timeduration"):
        fields["start_date"] = iso(ev["timestart"])
        fields["end_date"] = iso(ev["timestart"] + ev["timeduration"])
    return fields


def fingerprint(fields: dict) -> str:
    raw = "\x1f".join(str(fields[k]) for k in sorted(fields))
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def with_marker(fields: dict, event_id: int) -> dict:
    out = dict(fields)
    out["description"] += f"<p><code>moodle-event:{event_id}:{fingerprint(fields)}</code></p>"
    return out


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


class Syncer:
    def __init__(self, cfg: Config, dry_run: bool = False, recategorize: bool = False):
        self.cfg = cfg
        self.dry = dry_run
        self.recategorize = recategorize
        self.stats = dict(created=0, updated=0, done=0, graded=0, deleted=0, restored=0, relabelled=0, categorized=0)

    def run(self) -> dict:
        cfg = self.cfg
        moodle = Moodle(cfg.moodle_url, cfg.moodle_username, cfg.moodle_password)
        vik = Vikunja(cfg.vikunja_url, cfg.vikunja_token, cfg.vikunja_username, cfg.vikunja_password)
        self.moodle, self.vik = moodle, vik

        today = date.today()
        start = add_months(today.replace(day=1), -cfg.lookback_months)
        window_start = int(datetime(start.year, start.month, 1, tzinfo=timezone.utc).timestamp())
        months = cfg.lookback_months + cfg.lookahead_months + 1

        events = moodle.events(start, months)
        pending = moodle.action_events(window_start)
        # Pending action events carry the action link; prefer them over the calendar copy.
        events.update(pending)
        log.info("moodle: %d events (%d pending actions)", len(events), len(pending))

        self.project_id = vik.ensure_project(cfg.vikunja_project)
        tracked: dict[int, dict] = {}
        for t in vik.tasks(self.project_id):
            m = MARKER_RE.search(t.get("description") or "")
            if m:
                tracked[int(m.group(1))] = t
        log.info("vikunja: %d tracked tasks in project %r", len(tracked), cfg.vikunja_project)
        self.label_cache = vik.labels()
        courses = moodle.courses()
        self.course_names = course_labels(courses)
        # Earlier versions labelled tasks with the course short name (GS_0369).
        self.legacy_course_labels = {c["shortname"] for c in courses}
        self.grades: dict[str, Grade] = {}
        for c in courses:
            try:
                self.grades.update(moodle.grades(c["id"]))
            except (MoodleError, OSError) as exc:  # grades are extra; the sync goes on without them
                log.warning("grades for %s: %s", c["shortname"], exc)
        log.info("moodle: %d graded activities", len(self.grades))

        for eid, ev in sorted(events.items(), key=lambda kv: kv[1]["timestart"]):
            self.sync_event(ev, tracked.get(eid), pending=eid in pending)

        for eid, task in tracked.items():
            if eid in events or task["done"] or STRIKE in task["title"]:
                continue
            due = parse_iso(task.get("due_date"))
            if due is not None and due < window_start:
                continue  # outside the window we fetched; leave it alone
            ev = moodle.event_by_id(eid)
            if ev:
                self.sync_event(ev, task, pending=False)
            else:
                self.handle_missing(task)

        if cfg.openai_api_key:
            try:
                self.categorize(events)
            except Exception as exc:  # categories are a nice-to-have; retried next run
                log.warning("AI categorization failed: %s", exc)
        else:
            log.info("OPENAI_API_KEY not set; skipping AI categories")

        log.info("done: %s", ", ".join(f"{k}={v}" for k, v in self.stats.items()))
        return self.stats

    def sync_event(self, ev: dict, task: dict | None, pending: bool) -> None:
        grade = self.grades.get(activity_key(ev.get("url")))
        fields = render(ev, grade)
        if task is None:
            task = with_marker(fields, ev["id"])
            task["done"] = self.is_done(ev, pending)
            log.info("create %s%s", task["title"], " [done]" if task["done"] else "")
            self.stats["created"] += 1
            if self.dry:
                return
            created = self.vik.create_task(self.project_id, task)
            self.course_label(created, ev)
            return

        changed = False
        m = MARKER_RE.search(task.get("description") or "")
        if STRIKE in task["title"] or not m or m.group(2) != fingerprint(fields):
            if STRIKE in task["title"]:
                self.stats["restored"] += 1
            if grade and render_grade(grade) not in (task.get("description") or ""):
                log.info("grade %s: %s", fields["title"], grade.grade)
                self.stats["graded"] += 1
            task = {**task, **with_marker(fields, ev["id"])}
            log.info("update %s", task["title"])
            self.stats["updated"] += 1
            changed = True
        if not task["done"] and self.is_done(ev, pending):
            task = {**task, "done": True}
            log.info("mark done %s", task["title"])
            self.stats["done"] += 1
            changed = True
        if changed and not self.dry:
            self.vik.update_task(task)
        if not self.dry:
            self.course_label(task, ev)

    def handle_missing(self, task: dict) -> None:
        """The event vanished from Moodle: either it was completed or deleted."""
        url = re.search(r'<strong>Moodle:</strong> <a href="([^"]+)"', task.get("description") or "")
        if url:
            state = self.moodle.activity_state(html.unescape(url.group(1)))
            if state.exists and state.done:
                log.info("mark done (event gone) %s", task["title"])
                self.stats["done"] += 1
                if not self.dry:
                    self.vik.update_task({**task, "done": True})
                return
        log.info("strike %s", task["title"])
        self.stats["deleted"] += 1
        note = f"<p><strong>Removed from Moodle on {date.today().isoformat()}.</strong></p>"
        if not self.dry:
            self.vik.update_task({**task, "title": strike(task["title"])[:250], "description": note + task["description"]})

    def is_done(self, ev: dict, pending: bool) -> bool:
        # Pending action events are by definition not done yet; anything else that
        # points at an activity gets its page checked (submission / completion).
        if pending or (ev.get("action") or {}).get("actionable") or not ev.get("url"):
            return False
        if "/mod/" not in ev["url"]:
            return False
        if activity_key(ev["url"]) in self.grades:
            return True
        return self.moodle.activity_state(ev["url"]).done

    def categorize(self, events: dict[int, dict]) -> None:
        """Give every task without a category label one, picked by the AI model.

        With recategorize set, every task is classified again and relabelled.
        """
        categories = {l["title"]: l for l in self.label_cache.values()
                      if CATEGORY_MARKER in (l.get("description") or "")}
        todo = []
        for t in self.vik.tasks(self.project_id):
            m = MARKER_RE.search(t.get("description") or "")
            if not m or STRIKE in t["title"]:
                continue
            if not self.recategorize and any(l["title"] in categories for l in t.get("labels") or []):
                continue
            todo.append((int(m.group(1)), t))
        if not todo:
            return
        log.info("categorizing %d tasks with %s", len(todo), self.cfg.openai_model)
        if self.dry:
            return

        used = {c.casefold() for c in categories}
        known = list(categories) + [c for c in self.cfg.categories if c.casefold() not in used]
        classifier = Classifier(self.cfg.openai_api_key, self.cfg.openai_model, self.cfg.openai_base_url)
        results = classifier.classify([describe(eid, events.get(eid), t["title"]) for eid, t in todo], known)

        for eid, task in todo:
            res = results.get(eid)
            if not res:
                continue
            label = self.category_label(res["category"].strip(), categories)
            current = [l for l in task.get("labels") or [] if l["title"] in categories]
            if task.get("priority") == res["priority"] and [l["id"] for l in current] == [label["id"]]:
                continue
            if task.get("priority") != res["priority"]:
                self.vik.update_task({**task, "priority": res["priority"]})
            for old in current:
                if old["id"] != label["id"]:
                    self.vik.remove_label(task["id"], old["id"])
            if label["id"] not in [l["id"] for l in current]:
                self.vik.add_label(task["id"], label["id"])
            log.info("categorize %s -> %s (priority %d)", task["title"], label["title"], res["priority"])
            self.stats["categorized"] += 1

    def category_label(self, name: str, categories: dict[str, dict]) -> dict:
        for title, label in categories.items():
            if title.casefold() == name.casefold():
                return label
        # Use the configured spelling for suggested categories ("task" -> "Task").
        name = next((c for c in self.cfg.categories if c.casefold() == name.casefold()), name)
        label = self.vik.create_label(name, label_color(name), CATEGORY_MARKER)
        categories[label["title"]] = label
        self.label_cache[label["title"]] = label
        return label

    def course_label(self, task: dict, ev: dict) -> None:
        """Make sure the task carries its course's short-name label and no other course label."""
        course = ev.get("course") or {}
        if not course:
            return
        name = self.course_names.get(course["id"]) or abbreviate(course["fullname"])
        current = [l for l in task.get("labels") or []
                   if COURSE_MARKER in (l.get("description") or "") or l["title"] in self.legacy_course_labels]
        if [l["title"] for l in current] == [name]:
            return
        label = next((l for l in self.label_cache.values()
                      if l["title"] == name and COURSE_MARKER in (l.get("description") or "")), None)
        if label is None:
            label = self.vik.create_label(name, label_color(name), f"{course['fullname']}\n\n{COURSE_MARKER}")
            self.label_cache[label["title"]] = label
        for old in current:
            if old["id"] != label["id"]:
                self.vik.remove_label(task["id"], old["id"])
        if label["id"] not in [l["id"] for l in current]:
            self.vik.add_label(task["id"], label["id"])
        if current:
            log.info("relabel %s -> %s", task["title"], name)
            self.stats["relabelled"] += 1
