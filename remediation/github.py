"""Issues and pull requests, on GitHub or on a local stand-in.

Two implementations of one small interface:

  GitHubTracker  the real thing, through the `gh` CLI (authenticated as
                 whoever runs it: a person locally, GITHUB_TOKEN in Actions)
  LocalTracker   a folder of JSON and Markdown that behaves the same way, so
                 the whole flow can run, and be inspected, before anything is
                 pushed. Every item is also rendered as <kind>-<n>.md.

Items are found again by a hidden marker in their body, e.g.
<!-- quarantine: extract.stripe_payments -->, never by title, so a human can
retitle an issue without breaking deduplication.
"""

import json
import re
import subprocess
from pathlib import Path

from remediation.config import REPO_ROOT

LOCAL_TRACKER_DIR = REPO_ROOT / ".remediation-github"

# Label -> (colour, description); created on demand on GitHub.
LABELS = {
    "ai-remediation": ("5319e7", "Opened by the AI remediation layer"),
    "risk:auto": ("0e8a16", "Lineage reaches no revenue or customer model"),
    "risk:quarantine": ("d93f0b", "Touches revenue or customer data: a human must merge"),
    "quarantine": ("b60205", "Source records held out of staging until resolved"),
    "data-issue": ("fbca04", "Source data is wrong; fix upstream, not in this repo"),
    "needs-human": ("c5def5", "Needs a decision the automation can't make"),
    "ai-remediation:auto-merge": ("1d76db", "Will merge itself once required checks pass"),
    "ai-remediation:blocked": ("000000", "The remediation loop stopped; a human takes it from here"),
}


def marker(key: str, value: str) -> str:
    return f"<!-- {key}: {value} -->"


def read_marker(body: str, key: str) -> str | None:
    m = re.search(rf"<!-- {re.escape(key)}: (.*?) -->", body or "")
    return m.group(1) if m else None


class LocalTracker:
    def __init__(self, directory: Path = LOCAL_TRACKER_DIR):
        self.directory = directory
        self.path = directory / "items.json"

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def _save(self, items: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, indent=2) + "\n")
        for item in items:
            comments = "".join(f"\n---\n**Comment:** {c}\n" for c in item["comments"])
            (self.directory / f"{item['kind']}-{item['number']}.md").write_text(
                f"# #{item['number']} [{item['state']}] {item['title']}\n\n"
                f"Labels: {', '.join(item['labels'])}"
                + (f"  \nBranch: `{item['head']}` -> `{item['base']}`" if item["kind"] == "pr" else "")
                + f"\n\n{item['body']}\n{comments}")

    def _item(self, number: int) -> tuple[list[dict], dict]:
        items = self._load()
        return items, next(i for i in items if i["number"] == number)

    def is_open(self, number: int) -> bool:
        return self._item(number)[1]["state"] == "open"

    def find_open(self, kind: str, label: str, key: str, value: str) -> dict | None:
        return next((i for i in self._load() if i["kind"] == kind and i["state"] == "open"
                     and label in i["labels"] and read_marker(i["body"], key) == value), None)

    def list_open(self, kind: str, label: str) -> list[dict]:
        return [i for i in self._load() if i["kind"] == kind and i["state"] == "open" and label in i["labels"]]

    def _create(self, **item) -> dict:
        items = self._load()
        item.update(number=max((i["number"] for i in items), default=0) + 1, state="open", comments=[])
        item["url"] = f"local://{item['kind']}/{item['number']}"
        items.append(item)
        self._save(items)
        return item

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        return self._create(kind="issue", title=title, body=body, labels=labels)

    def create_pr(self, head: str, base: str, title: str, body: str, labels: list[str],
                  reviewers: list[str]) -> dict:
        return self._create(kind="pr", title=title, body=body, labels=labels, head=head, base=base,
                            reviewers=reviewers)

    def update(self, number: int, title: str, body: str) -> None:
        items, item = self._item(number)
        item.update(title=title, body=body)
        self._save(items)

    def comment(self, number: int, text: str) -> None:
        items, item = self._item(number)
        item["comments"].append(text)
        self._save(items)

    def close(self, number: int, text: str) -> None:
        items, item = self._item(number)
        item["comments"].append(text)
        item["state"] = "closed"
        self._save(items)

    def add_labels(self, number: int, labels: list[str]) -> None:
        items, item = self._item(number)
        item["labels"] += [lbl for lbl in labels if lbl not in item["labels"]]
        self._save(items)

    def remove_labels(self, number: int, labels: list[str]) -> None:
        items, item = self._item(number)
        item["labels"] = [lbl for lbl in item["labels"] if lbl not in labels]
        self._save(items)

    def merge(self, number: int, method: str, head_sha: str) -> str | None:
        items, item = self._item(number)
        item.update(state="merged", merged_with=method, merged_sha=head_sha)
        self._save(items)
        return None


class GitHubTracker:
    """The same interface through `gh`. Run from inside the repository."""

    def __init__(self, repo_dir: Path = REPO_ROOT):
        self.repo_dir = repo_dir
        self._labels_ensured = False

    def _gh(self, *args: str, body: str | None = None) -> str:
        result = subprocess.run(["gh", *args], cwd=self.repo_dir, input=body, capture_output=True,
                                text=True, check=True)
        return result.stdout.strip()

    def _ensure_labels(self) -> None:
        if self._labels_ensured:
            return
        for name, (colour, description) in LABELS.items():
            self._gh("label", "create", name, "--color", colour, "--description", description, "--force")
        self._labels_ensured = True

    def list_open(self, kind: str, label: str) -> list[dict]:
        # No --label filter: with one, `gh` queries GitHub's search index,
        # which can lag real state by hours and returned a PR closed long
        # before. Listing by state alone reads live data; labels are
        # filtered here.
        cmd = "pr" if kind == "pr" else "issue"
        fields = "number,title,body,url,labels,state" + (",headRefName" if kind == "pr" else "")
        raw = json.loads(self._gh(cmd, "list", "--state", "open", "--limit", "200", "--json", fields))
        items = []
        for item in raw:
            item["kind"], item["labels"] = kind, [lbl["name"] for lbl in item["labels"]]
            item["head"] = item.pop("headRefName", None)
            if label in item["labels"] and item.get("state", "OPEN").upper() == "OPEN":
                items.append(item)
        return items

    def is_open(self, number: int) -> bool:
        # `gh issue view` works for PRs too and reads the item directly.
        return self._gh("issue", "view", str(number), "--json", "state", "--jq", ".state").upper() == "OPEN"

    def find_open(self, kind: str, label: str, key: str, value: str) -> dict | None:
        return next((i for i in self.list_open(kind, label) if read_marker(i["body"], key) == value), None)

    @staticmethod
    def _number(url: str) -> int:
        return int(url.rstrip("/").rsplit("/", 1)[-1])

    def create_issue(self, title: str, body: str, labels: list[str]) -> dict:
        self._ensure_labels()
        url = self._gh("issue", "create", "--title", title, "--body-file", "-",
                       *[a for lbl in labels for a in ("--label", lbl)], body=body)
        return {"number": self._number(url), "url": url, "kind": "issue", "title": title}

    def create_pr(self, head: str, base: str, title: str, body: str, labels: list[str],
                  reviewers: list[str]) -> dict:
        self._ensure_labels()
        # GitHub refuses a review request to the PR's own author, which is
        # whoever `gh` is signed in as (a person when run locally).
        # As the App there is no "user" (installation tokens can't read it),
        # and the bot can request anyone.
        try:
            me = self._gh("api", "user", "--jq", ".login")
        except subprocess.CalledProcessError:
            me = ""
        reviewers = [r for r in reviewers if r.lower() != me.lower()]
        url = self._gh("pr", "create", "--head", head, "--base", base, "--title", title, "--body-file", "-",
                       *[a for lbl in labels for a in ("--label", lbl)],
                       *[a for r in reviewers for a in ("--reviewer", r)], body=body)
        return {"number": self._number(url), "url": url, "kind": "pr", "title": title}

    def update(self, number: int, title: str, body: str) -> None:
        # `gh issue edit` also edits pull requests (they share numbering).
        self._gh("issue", "edit", str(number), "--title", title, "--body-file", "-", body=body)

    def comment(self, number: int, text: str) -> None:
        self._gh("issue", "comment", str(number), "--body-file", "-", body=text)

    def close(self, number: int, text: str) -> None:
        self._gh("issue", "close", str(number), "--comment", text)

    def add_labels(self, number: int, labels: list[str]) -> None:
        self._ensure_labels()
        self._gh("issue", "edit", str(number), *[a for lbl in labels for a in ("--add-label", lbl)])

    def remove_labels(self, number: int, labels: list[str]) -> None:
        self._gh("issue", "edit", str(number), *[a for lbl in labels for a in ("--remove-label", lbl)])

    def merge(self, number: int, method: str, head_sha: str) -> str | None:
        """Merge, but only if the PR's head is still the commit CI passed on
        (--match-head-commit), so nothing pushed after the green run slips in.
        Branch protection, where it exists, still applies. Returns why it
        didn't merge, or None."""
        try:
            self._gh("pr", "merge", str(number), f"--{method}", "--match-head-commit", head_sha)
        except subprocess.CalledProcessError as exc:
            return (exc.stderr or str(exc)).strip()
        return None
