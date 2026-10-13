import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without overriding existing vars."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


@dataclass
class Config:
    moodle_url: str
    moodle_username: str
    moodle_password: str
    vikunja_url: str
    vikunja_token: str
    vikunja_username: str
    vikunja_password: str
    vikunja_project: str
    lookback_months: int
    lookahead_months: int

    @classmethod
    def from_env(cls) -> "Config":
        def req(name: str) -> str:
            value = os.environ.get(name, "").strip()
            if not value:
                raise SystemExit(f"missing required setting {name}")
            return value

        cfg = cls(
            moodle_url=req("MOODLE_URL").rstrip("/"),
            moodle_username=req("MOODLE_USERNAME"),
            moodle_password=req("MOODLE_PASSWORD"),
            vikunja_url=req("VIKUNJA_URL").rstrip("/"),
            vikunja_token=os.environ.get("VIKUNJA_TOKEN", "").strip(),
            vikunja_username=os.environ.get("VIKUNJA_USERNAME", "").strip(),
            vikunja_password=os.environ.get("VIKUNJA_PASSWORD", "").strip(),
            vikunja_project=os.environ.get("VIKUNJA_PROJECT", "Moodle").strip() or "Moodle",
            lookback_months=int(os.environ.get("LOOKBACK_MONTHS", "3")),
            lookahead_months=int(os.environ.get("LOOKAHEAD_MONTHS", "12")),
        )
        if not cfg.vikunja_token and not (cfg.vikunja_username and cfg.vikunja_password):
            raise SystemExit("set VIKUNJA_TOKEN or VIKUNJA_USERNAME + VIKUNJA_PASSWORD")
        return cfg
