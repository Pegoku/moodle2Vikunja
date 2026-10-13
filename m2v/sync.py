import hashlib
import html
import logging
import re
from datetime import date, datetime, timezone

from .config import Config
from .moodle import Moodle
from .vikunja import Vikunja

log = logging.getLogger("m2v")

MARKER_RE = re.compile(r"moodle-event:(\d+):([0-9a-f]+)")
STRIKE = "̶"
NULL_DATE = "0001-01-01T00:00:00Z"


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


def render(ev: dict) -> dict:
    """Build the Vikunja task fields for a Moodle event (without the marker)."""
    course = ev.get("course") or {}
    action = ev.get("action") or {}
    parts = []
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
    def __init__(self, cfg: Config, dry_run: bool = False):
        self.cfg = cfg
        self.dry = dry_run
        self.stats = dict(created=0, updated=0, done=0, deleted=0, restored=0)

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

        log.info("done: %s", ", ".join(f"{k}={v}" for k, v in self.stats.items()))
        return self.stats

    def sync_event(self, ev: dict, task: dict | None, pending: bool) -> None:
        fields = render(ev)
        if task is None:
            task = with_marker(fields, ev["id"])
            task["done"] = self.is_done(ev, pending)
            log.info("create %s%s", task["title"], " [done]" if task["done"] else "")
            self.stats["created"] += 1
            if self.dry:
                return
            created = self.vik.create_task(self.project_id, task)
            self.label(created, ev)
            return

        changed = False
        m = MARKER_RE.search(task.get("description") or "")
        if STRIKE in task["title"] or not m or m.group(2) != fingerprint(fields):
            if STRIKE in task["title"]:
                self.stats["restored"] += 1
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
        return self.moodle.activity_state(ev["url"]).done

    def label(self, task: dict, ev: dict) -> None:
        course = ev.get("course") or {}
        name = course.get("shortname")
        if not name:
            return
        label = self.label_cache.get(name)
        if label is None:
            label = self.vik.create_label(name, label_color(name))
            self.label_cache[name] = label
        self.vik.add_label(task["id"], label["id"])
