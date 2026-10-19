# moodle2Vikunja

Syncs every dated item from a Moodle calendar (assignments, quizzes, course
events, video classes, ...) into a Vikunja project.

- **Created**: every Moodle calendar event in the sync window becomes a Vikunja
  task with its due date, a label for the course, and links to the course, the
  activity, the submission page and the calendar entry.
- **Modified**: when the event changes in Moodle (name, date, description) the
  task is updated. Unchanged tasks are left alone, so your own edits stick
  until Moodle changes the event.
- **Deleted**: when an event disappears from Moodle, its task title is struck
  through (t̶h̶i̶s̶ ̶w̶a̶y̶) and a note is added. If it comes back, it is restored.
- **Submitted**: when you hand in an assignment, finish a quiz or complete an
  activity, the task is marked as done.
- **Graded**: once a grade shows up in your Moodle grade report, it is added
  at the top of the task description with its range and the teacher's
  feedback, and the task is marked as done.

Course labels are short forms of the course name: the initials of its
meaningful words, keeping Roman numerals and existing acronyms
("Itinerari personal per a l'ocupabilitat I" -> `IPO I`, "Implantació de
sistemes operatius" -> `ISO`). The full name is in the label description.

Works with Moodle sites that have the mobile web service disabled: it logs in
like a browser and uses Moodle's AJAX endpoint plus the activity pages.

No local state is kept: each task carries a `moodle-event:<id>:<hash>` marker at
the end of its description, which is how the tool finds it again. Don't remove
that line.

## AI categories and priorities

With `OPENAI_API_KEY` set, each new task is sent to an OpenAI model
(`OPENAI_MODEL`, default `gpt-6-luna`), which picks a category label and a
priority:

| Priority | What |
|----------|------|
| 4 (urgent) | graded work to hand in (assignments, tasks) |
| 3 (high) | quizzes and exams |
| 2 (medium) | video calls, live classes, sessions to attend |
| 1 (low) | informational dates (period start/end, quiz opening, ...) |

Category labels are created with the description `m2v:category`. Every request
includes the categories already in use, so the model reuses them instead of
creating new ones each time. `AI_CATEGORIES` seeds the list on the first run.
Tasks are classified once; a task is only sent again if you remove its
category label. Priorities you change by hand are kept.
`python -m m2v --recategorize` classifies every task again (and overwrites
priorities), e.g. after changing the model or `AI_CATEGORIES`.

## Setup

```sh
pip install -r requirements.txt
cp .env.example .env   # fill in credentials; .env is git-ignored
python -m m2v --dry-run   # preview
python -m m2v             # sync once
python -m m2v --loop 30   # sync every 30 minutes
```

Settings can also come from real environment variables (they take precedence
over `.env`).

### Cron

```cron
*/30 * * * * cd /path/to/moodle2Vikunja && python3 -m m2v >> sync.log 2>&1
```

### Docker

Every push to GitHub builds `ghcr.io/pegoku/moodle2vikunja` (`latest` follows
`main`). With a filled-in `.env` next to `docker-compose.yml`:

```sh
docker compose up -d
```

Or build it locally:

```sh
docker build -t moodle2vikunja .
docker run -d --restart unless-stopped --env-file .env moodle2vikunja
```

## How "done" is detected

| Activity | Done when |
|----------|-----------|
| Assignment | submission status is *Submitted* |
| Quiz | a finished attempt exists |
| Anything else | all completion conditions met, or marked done manually in Moodle |
| Any graded activity | it has a grade in the user grade report |

Course/site events (video classes, term start/end, ...) have nothing to submit,
so they are never marked done automatically.
