"""Moodle client using a normal browser-style session.

The IEDIB site has the mobile web service disabled, so instead of a token we
log in through the login form and call the AJAX endpoint (lib/ajax/service.php)
that Moodle's own JavaScript uses. Only AJAX-enabled functions are available
there, which is why submission status is read from the activity pages.
"""

import re
from dataclasses import dataclass
from datetime import date

import requests

USER_AGENT = "moodle2vikunja/1.0"


class MoodleError(Exception):
    pass


@dataclass
class ActivityState:
    exists: bool
    done: bool


class Moodle:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url
        self.s = requests.Session()
        self.s.headers["User-Agent"] = USER_AGENT
        self._login(username, password)
        self._activity_cache: dict[str, ActivityState] = {}

    def _login(self, username: str, password: str) -> None:
        r = self.s.get(f"{self.base}/login/index.php", timeout=30)
        r.raise_for_status()
        m = re.search(r'name="logintoken" value="([^"]+)"', r.text)
        r = self.s.post(
            f"{self.base}/login/index.php",
            data={"username": username, "password": password, "logintoken": m.group(1) if m else ""},
            timeout=30,
        )
        r.raise_for_status()
        m = re.search(r'"sesskey":"([^"]+)"', r.text)
        if not m or "/login/" in r.url:
            raise MoodleError("Moodle login failed (check MOODLE_USERNAME / MOODLE_PASSWORD)")
        self.sesskey = m.group(1)

    def call(self, function: str, args: dict):
        r = self.s.post(
            f"{self.base}/lib/ajax/service.php",
            params={"sesskey": self.sesskey, "info": function},
            json=[{"index": 0, "methodname": function, "args": args}],
            timeout=60,
        )
        r.raise_for_status()
        res = r.json()
        if isinstance(res, dict):  # whole-request failure, e.g. expired session
            raise MoodleError(f"{function}: {res.get('error') or res}")
        res = res[0]
        if res.get("error"):
            exc = res.get("exception") or {}
            raise MoodleError(f"{function}: {exc.get('errorcode')}: {exc.get('message')}")
        return res["data"]

    def courses(self) -> list[dict]:
        data = self.call(
            "core_course_get_enrolled_courses_by_timeline_classification",
            {"classification": "all", "limit": 0, "offset": 0},
        )
        return data["courses"]

    def events(self, start: date, months: int) -> dict[int, dict]:
        """All calendar events visible to the user for `months` months from `start`."""
        found: dict[int, dict] = {}
        y, m = start.year, start.month
        for _ in range(months):
            data = self.call(
                "core_calendar_get_calendar_monthly_view",
                {"year": y, "month": m, "courseid": 1, "categoryid": 0,
                 "includenavigation": False, "mini": False},
            )
            for week in data["weeks"]:
                for day in week["days"]:
                    for ev in day["events"]:
                        found[ev["id"]] = ev
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        return found

    def action_events(self, since: int) -> dict[int, dict]:
        """Pending action events (things to submit/attempt), including overdue ones."""
        found: dict[int, dict] = {}
        after = None
        while True:
            args = {"timesortfrom": since, "limitnum": 50}
            if after:
                args["aftereventid"] = after
            evs = self.call("core_calendar_get_action_events_by_timesort", args)["events"]
            for ev in evs:
                found[ev["id"]] = ev
            if len(evs) < 50:
                return found
            after = evs[-1]["id"]

    def event_by_id(self, event_id: int) -> dict | None:
        try:
            return self.call("core_calendar_get_calendar_event_by_id", {"eventid": event_id})["event"]
        except MoodleError:
            return None

    def activity_state(self, url: str) -> ActivityState:
        """Scrape an activity page to see whether it still exists and whether it's done."""
        m = re.search(r"/mod/(\w+)/view\.php\?id=(\d+)", url or "")
        if not m:
            return ActivityState(exists=True, done=False)
        modname, cmid = m.groups()
        key = f"{modname}:{cmid}"
        if key in self._activity_cache:
            return self._activity_cache[key]
        r = self.s.get(f"{self.base}/mod/{modname}/view.php", params={"id": cmid}, timeout=60)
        if r.status_code == 404 or (r.ok and 'class="errorcode"' in r.text) or "/login/" in r.url:
            state = ActivityState(exists=False, done=False)
        else:
            r.raise_for_status()
            state = ActivityState(exists=True, done=_page_done(modname, r.text))
        self._activity_cache[key] = state
        return state


_FINISHED_WORDS = re.compile(r"\b(Acabat|Finished|Finalizado|Terminado|Beendet|Terminé)\b")


def _page_done(modname: str, page: str) -> bool:
    if modname == "assign":
        # Status cell gets class submissionstatussubmitted once the work is handed in.
        if "submissionstatussubmitted" in page:
            return True
    elif modname == "quiz":
        if "/mod/quiz/review.php?attempt=" in page:
            return True
        summary = re.search(r'quizattemptsummary.*?</table>', page, re.S)
        if summary and _FINISHED_WORDS.search(summary.group(0)):
            return True
    # Manual completion toggled to "done".
    if 'data-toggletype="manual:undo"' in page:
        return True
    # Automatic completion: every requirement badge marked as fulfilled.
    block = re.search(r'data-region\s*="completionrequirements".*?</div>\s*</div>', page, re.S)
    if block:
        badges = re.findall(r'<div class="badge[^"]*"', block.group(0))
        if badges and all("alert-success" in b for b in badges):
            return True
    return False
