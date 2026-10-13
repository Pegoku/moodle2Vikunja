import requests


class VikunjaError(Exception):
    pass


class Vikunja:
    def __init__(self, base_url: str, token: str = "", username: str = "", password: str = ""):
        self.api = f"{base_url}/api/v1"
        self.s = requests.Session()
        if not token:
            r = self.s.post(f"{self.api}/login", json={"username": username, "password": password, "long_token": True}, timeout=30)
            if not r.ok:
                raise VikunjaError(f"Vikunja login failed: {r.status_code} {r.text[:200]}")
            token = r.json()["token"]
        self.s.headers["Authorization"] = f"Bearer {token}"

    def _req(self, method: str, path: str, **kw):
        r = self.s.request(method, f"{self.api}{path}", timeout=60, **kw)
        if not r.ok:
            raise VikunjaError(f"{method} {path}: {r.status_code} {r.text[:300]}")
        return r

    def _paged(self, path: str, params: dict | None = None) -> list[dict]:
        out, page = [], 1
        while True:
            r = self._req("GET", path, params={**(params or {}), "page": page, "per_page": 50})
            out.extend(r.json() or [])
            if page >= int(r.headers.get("x-pagination-total-pages", 1)):
                return out
            page += 1

    def ensure_project(self, title: str) -> int:
        for p in self._paged("/projects", {"is_archived": "true"}):
            if p["title"] == title and not p.get("parent_project_id"):
                return p["id"]
        return self._req("PUT", "/projects", json={"title": title}).json()["id"]

    def tasks(self, project_id: int) -> list[dict]:
        return self._paged("/tasks", {"filter": f"project = {project_id}"})

    def create_task(self, project_id: int, task: dict) -> dict:
        return self._req("PUT", f"/projects/{project_id}/tasks", json=task).json()

    def update_task(self, task: dict) -> dict:
        # Vikunja replaces the whole task on update, so always send the full object.
        return self._req("POST", f"/tasks/{task['id']}", json=task).json()

    def labels(self) -> dict[str, dict]:
        return {l["title"]: l for l in self._paged("/labels")}

    def create_label(self, title: str, color: str, description: str = "") -> dict:
        return self._req("PUT", "/labels", json={"title": title, "hex_color": color, "description": description}).json()

    def add_label(self, task_id: int, label_id: int) -> None:
        self._req("PUT", f"/tasks/{task_id}/labels", json={"label_id": label_id})
