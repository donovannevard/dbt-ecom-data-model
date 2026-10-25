"""Act as the dbt-ecom-remediation GitHub App rather than as a person.

PRs, issues and comments are shown as written by whichever account opened
them. Opened with a person's `gh` login they look like that person's, and
GitHub never notifies anyone about their own actions. As the App they come
from dbt-ecom-remediation[bot], and the maintainer is notified like any
reviewer.

Configuration (environment, or .env):

    REMEDIATION_APP_ID          the App's numeric id
    REMEDIATION_APP_KEY_FILE    path to its private key (.pem), kept outside the repo
    REMEDIATION_APP_KEY         or the key's contents (for CI secrets)

The App authenticates with an RS256-signed JWT exchanged for a one-hour
installation token for the target repository. Signing uses the `openssl`
binary (present on macOS and GitHub's runners) rather than a Python crypto
dependency. The token is handed to `gh` and git via GH_TOKEN for the duration
of `acting_as_app()`, and never written to disk or into git config.
"""

import base64
import json
import os
import subprocess
import tempfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

API = "https://api.github.com"


class AppError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key_pem: str, now: int | None = None) -> str:
    now = int(now or time.time())
    # Backdated a minute for clock drift; GitHub caps the lifetime at 10 minutes.
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}).encode())
    signing_input = f"{header}.{payload}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False) as key:
        key.write(key_pem)
        key_path = key.name
    try:
        os.chmod(key_path, 0o600)
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", key_path],
                                   input=signing_input, capture_output=True, check=True).stdout
    except subprocess.CalledProcessError as exc:
        raise AppError(f"could not sign the App JWT: {exc.stderr.decode().strip()}") from None
    finally:
        os.unlink(key_path)
    return f"{header}.{payload}.{_b64url(signature)}"


def _request(method: str, path: str, bearer: str) -> dict:
    req = urllib.request.Request(f"{API}{path}", method=method, headers={
        "Authorization": f"Bearer {bearer}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "dbt-ecom-remediation"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="ignore")[:300]
        raise AppError(f"GitHub API {method} {path}: {exc.code} {detail}") from None


def installation_token(app_id: str, key_pem: str, repo: str) -> str:
    """repo: owner/name. The App must be installed on it."""
    jwt = app_jwt(app_id, key_pem)
    installation = _request("GET", f"/repos/{repo}/installation", jwt)
    return _request("POST", f"/app/installations/{installation['id']}/access_tokens", jwt)["token"]


def configured() -> tuple[str, str] | None:
    app_id = os.environ.get("REMEDIATION_APP_ID")
    key = os.environ.get("REMEDIATION_APP_KEY")
    key_file = os.environ.get("REMEDIATION_APP_KEY_FILE")
    if key_file and not key:
        path = Path(key_file).expanduser()
        if not path.exists():
            raise AppError(f"REMEDIATION_APP_KEY_FILE points at {path}, which doesn't exist")
        key = path.read_text()
    return (app_id, key) if app_id and key else None


def repo_slug(repo_dir: Path) -> str:
    url = subprocess.run(["git", "-C", str(repo_dir), "remote", "get-url", "origin"],
                         capture_output=True, text=True, check=True).stdout.strip()
    slug = url.removesuffix(".git").split("github.com")[-1].lstrip(":/")
    if slug.count("/") != 1:
        raise AppError(f"origin {url!r} is not a GitHub repository")
    return slug


@contextmanager
def acting_as_app(repo: str, required: bool = False):
    """Inside the block, `gh` and git pushes (via gh's credential helper)
    act as the App. Without App configuration they act as whoever `gh` is
    signed in as, unless required=True."""
    creds = configured()
    if creds is None:
        if required:
            raise AppError("REMEDIATION_APP_ID and REMEDIATION_APP_KEY_FILE (or _KEY) are not set")
        yield None
        return
    token = installation_token(*creds, repo)
    previous = os.environ.get("GH_TOKEN")
    os.environ["GH_TOKEN"] = token
    try:
        yield token
    finally:
        if previous is None:
            os.environ.pop("GH_TOKEN", None)
        else:
            os.environ["GH_TOKEN"] = previous
