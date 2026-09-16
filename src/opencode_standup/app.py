#!/usr/bin/env python3
"""
opencode-standup

A daily command-center for opencode sessions across all your repos:
  - Standup tab: sessions grouped by project for a chosen time scope
    (Today / Last workday / This week / All time), with cost & token
    rollups (including subagent spend).
  - GitHub tab: your opened, authored-and-merged, reviewed, and commented
    pull requests for the same time scope.

Actions (see the in-app help with `?`):
  e  rename the selected task/session (via opencode's REST API)
  x  archive the selected task/session (via opencode's REST API)
  A  toggle showing archived sessions
  u  unarchive the selected (archived) session -- direct DB write, since
     opencode's API does not currently expose an "unarchive" operation
  o  open/resume the selected session in the real opencode TUI
  1-5  switch time scope (Standup tab)
  [ ]  switch between the Standup and GitHub tabs
  c  set up GitHub credentials
  r  refresh
  q  quit
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import netrc
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from packaging.version import InvalidVersion, Version
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    Tree,
)
from textual.widgets.tree import TreeNode
from textual.worker import Worker, WorkerState

DEFAULT_DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")
PACKAGE_NAME = "opencode-standup"
GITHUB_OWNER = "RAGessler"
GITHUB_REPOSITORY = f"{GITHUB_OWNER}/{PACKAGE_NAME}"
GITHUB_RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
GITHUB_USER_URL = "https://api.github.com/user"
GITHUB_SEARCH_URL = "https://api.github.com/search/issues"
GITHUB_ACTIVITY_ORG = "ShamrockTrading"
GITHUB_ORGANIZATION_REPOSITORIES_URL = f"https://api.github.com/orgs/{GITHUB_ACTIVITY_ORG}/repos"
UPDATE_CHECK_TIMEOUT = 3
UPDATE_CACHE_TTL = 24 * 60 * 60
GITHUB_ACTIVITY_TIMEOUT = 15
GITHUB_ACTIVITY_LIMIT = 100
GITHUB_ACTIVITY_MAX_DAYS = 366
GITHUB_ACTIVITY_CACHE_TTL = 5 * 60
GITHUB_SUMMARY_ACTIVITY_LIMIT = 40
GITHUB_SUMMARY_MAX_CHARS = 4000
GITHUB_TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo&description=opencode-standup"
SUMMARY_SESSION_TITLE = "opencode-standup AI summary"
LEGACY_SUMMARY_TITLE_PREFIX = "Create a factual standup summary for"


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except ValueError:
        return default


SUMMARY_MAX_CHARS = _env_int("OPENCODE_STANDUP_SUMMARY_MAX_CHARS", 12000)
SUMMARY_SESSION_MAX_CHARS = _env_int("OPENCODE_STANDUP_SUMMARY_SESSION_MAX_CHARS", 2600)
SUMMARY_COMMAND_TIMEOUT = _env_int("OPENCODE_STANDUP_SUMMARY_TIMEOUT", 120)
JIRA_COMMAND_TIMEOUT = _env_int("OPENCODE_STANDUP_JIRA_TIMEOUT", 20)
JIRA_KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b", re.IGNORECASE)


def app_version() -> str:
    """Return the installed distribution version, with a source-tree fallback."""
    try:
        return importlib.metadata.version(PACKAGE_NAME)
    except importlib.metadata.PackageNotFoundError:
        from opencode_standup import __version__

        return __version__


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    release_url: str
    asset_url: str


class GitHubActivityError(RuntimeError):
    pass


_github_activity_cache: dict[tuple[str, str, date | None], tuple[float, GitHubActivityResult]] = {}


class GitHubActivityKind(Enum):
    OPENED = "opened"
    MERGED = "merged"
    REVIEWED = "reviewed"
    COMMENTED = "commented"


@dataclass(frozen=True)
class GitHubActivity:
    kind: GitHubActivityKind
    repository: str
    number: int
    title: str
    url: str
    occurred_at: str
    detail: str = ""


@dataclass(frozen=True)
class GitHubActivityResult:
    activities: tuple[GitHubActivity, ...] = ()
    available: bool = True
    error: str | None = None
    login: str | None = None


def update_cache_path() -> Path:
    cache_root = os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
    return Path(cache_root) / PACKAGE_NAME / "update.json"


def _read_update_cache() -> dict[str, object]:
    try:
        payload = json.loads(update_cache_path().read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_update_cache(payload: dict[str, object]) -> None:
    path = update_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        # An update check must never prevent the application from starting.
        pass


def should_check_for_update(now: float | None = None) -> bool:
    checked_at = _read_update_cache().get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return True
    return (now or time.time()) - checked_at >= UPDATE_CACHE_TTL


def mark_update_checked() -> None:
    cache = _read_update_cache()
    cache["checked_at"] = time.time()
    _write_update_cache(cache)


def mark_update_prompted(version: str) -> None:
    cache = _read_update_cache()
    cache["prompted_version"] = version
    _write_update_cache(cache)


def should_prompt_for_update(update: UpdateInfo | None) -> bool:
    if update is None:
        return False
    prompted_version = _read_update_cache().get("prompted_version")
    return prompted_version != update.version


def github_credentials_path() -> Path:
    config_root = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
    return Path(config_root) / PACKAGE_NAME / "github.json"


def _read_saved_github_credentials() -> tuple[str, str] | None:
    path = github_credentials_path()
    try:
        if not path.is_file() or path.stat().st_mode & 0o077:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    username = payload.get("username")
    token = payload.get("token")
    if not isinstance(username, str) or not isinstance(token, str) or not username or not token:
        return None
    return username, token


def save_github_credentials(username: str, token: str) -> None:
    path = github_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix="github-", delete=False
        ) as credentials_file:
            json.dump({"username": username, "token": token}, credentials_file)
            credentials_file.write("\n")
            credentials_file.flush()
            os.fchmod(credentials_file.fileno(), 0o600)
            temporary_path = credentials_file.name
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass


def github_credentials() -> tuple[str, str] | None:
    username = os.environ.get("OPENCODE_STANDUP_GITHUB_USERNAME")
    token = os.environ.get("OPENCODE_STANDUP_GITHUB_TOKEN")
    if username and token:
        return username, token
    try:
        credentials = netrc.netrc().authenticators("github.com")
    except (OSError, netrc.NetrcParseError):
        credentials = None
    if credentials is not None and credentials[0] and credentials[2]:
        return credentials[0], credentials[2]
    return _read_saved_github_credentials()


def github_request_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": f"{PACKAGE_NAME}/{app_version()}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Authorization": f"Bearer {token}",
    }


def validate_github_credentials(credentials: tuple[str, str]) -> tuple[bool, str]:
    request = urllib.request.Request(
        GITHUB_USER_URL,
        headers=github_request_headers(credentials[1]),
    )
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_ACTIVITY_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "GitHub rejected this token. Check that it is valid and has not expired."
        if exc.code == 403:
            return False, "GitHub denied the request or rate-limited the token."
        return False, f"GitHub returned HTTP {exc.code}."
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return False, f"Could not validate the GitHub token: {exc}"
    login = payload.get("login") if isinstance(payload, dict) else None
    if not isinstance(login, str) or not login:
        return False, "GitHub returned an unexpected identity response."
    try:
        _github_organization_access(github_request_headers(credentials[1]))
    except (OSError, UnicodeError, json.JSONDecodeError, GitHubActivityError) as exc:
        return False, str(exc)
    return True, login


def _github_error_message(exc: urllib.error.HTTPError, operation: str) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        payload = {}
    detail = payload.get("message") if isinstance(payload, dict) else None
    if exc.code == 401:
        return "GitHub authentication failed. Check your token."
    if exc.code == 403:
        if isinstance(detail, str) and "rate limit" in detail.casefold():
            reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
            if isinstance(reset, str) and reset.isdigit():
                when = datetime.fromtimestamp(int(reset)).astimezone().strftime("%H:%M")
                return f"GitHub rate limit reached; try again after {when}."
            return "GitHub rate limit reached; try again later."
        return (
            f"GitHub denied {operation}. Ensure the token can read private "
            f"{GITHUB_ACTIVITY_ORG} repositories and is SSO-authorized."
        )
    suffix = f": {detail}" if isinstance(detail, str) and detail else ""
    return f"GitHub {operation} failed (HTTP {exc.code}){suffix}"


def _github_get_json(url: str, headers: dict[str, str], operation: str) -> object:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=GITHUB_ACTIVITY_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise GitHubActivityError(_github_error_message(exc, operation)) from exc


def _github_organization_access(headers: dict[str, str]) -> None:
    payload = _github_get_json(
        f"{GITHUB_ORGANIZATION_REPOSITORIES_URL}?{urllib.parse.urlencode({'type': 'all', 'per_page': 1})}",
        headers,
        f"{GITHUB_ACTIVITY_ORG} repository access",
    )
    if not isinstance(payload, list):
        raise GitHubActivityError("GitHub returned an unexpected repository access response.")
    if not payload:
        raise GitHubActivityError(
            f"GitHub cannot access private {GITHUB_ACTIVITY_ORG} repositories. Ensure the token "
            "has repository access and is SSO-authorized."
        )


def _github_iso_datetime(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _github_scope_dates(scope: Scope) -> tuple[str, str]:
    start_ms, end_ms = scope.bounds_ms()
    start = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    now = datetime.now(timezone.utc)
    start = max(start, now - timedelta(days=GITHUB_ACTIVITY_MAX_DAYS))
    end = min(end, now)
    return start.isoformat().replace("+00:00", "Z"), end.isoformat().replace("+00:00", "Z")


def _github_activity_from_pull_request(
    kind: GitHubActivityKind,
    pull_request: object,
    occurred_at: str,
    detail: str = "",
) -> GitHubActivity | None:
    if not isinstance(pull_request, dict):
        return None
    repository = pull_request.get("repository")
    if not isinstance(repository, dict):
        return None
    number = pull_request.get("number")
    title = pull_request.get("title")
    url = pull_request.get("url")
    name = repository.get("nameWithOwner")
    if not isinstance(number, int) or not all(isinstance(value, str) and value for value in (title, url, name)):
        return None
    if not name.split("/", 1)[0].casefold() == GITHUB_ACTIVITY_ORG.casefold():
        return None
    return GitHubActivity(kind, name, number, title, url, occurred_at, detail)


def _github_scope_dates_for_search(scope: Scope) -> tuple[str, str]:
    start, end = _github_scope_dates(scope)
    end_date = (datetime.fromisoformat(end.replace("Z", "+00:00")) - timedelta(microseconds=1)).date()
    return start[:10], end_date.isoformat()


def _github_search_url(query: str, per_page: int) -> str:
    return f"{GITHUB_SEARCH_URL}?{urllib.parse.urlencode({'q': query, 'per_page': per_page, 'sort': 'updated', 'order': 'desc'})}"


def _github_search_items(
    query: str, headers: dict[str, str], limit: int, operation: str
) -> list[dict[str, object]]:
    payload = _github_get_json(_github_search_url(query, limit), headers, operation)
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise GitHubActivityError("GitHub returned an unexpected search response.")
    return [item for item in payload["items"][:limit] if isinstance(item, dict)]


def _github_in_scope(value: str | None, scope: Scope) -> bool:
    timestamp = _github_iso_datetime(value)
    if timestamp is None:
        return False
    start_ms, end_ms = scope.bounds_ms()
    timestamp_ms = int(timestamp.timestamp() * 1000)
    return start_ms <= timestamp_ms < end_ms


def _github_add_activity(
    activities: dict[tuple[str, str, int, str], GitHubActivity],
    kind: GitHubActivityKind,
    pull_request: object,
    occurred_at: str | None,
    detail: str = "",
) -> None:
    if not isinstance(occurred_at, str):
        return
    activity = _github_activity_from_pull_request(kind, pull_request, occurred_at, detail)
    if activity:
        activities[(activity.kind.value, activity.repository, activity.number, activity.occurred_at)] = activity


def _github_pr_from_search_item(item: dict[str, object]) -> dict[str, object]:
    repository = item.get("repository")
    full_name = repository.get("full_name") if isinstance(repository, dict) else None
    if not isinstance(full_name, str):
        repository_url = item.get("repository_url")
        full_name = repository_url.removeprefix("https://api.github.com/repos/") if isinstance(repository_url, str) else None
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "url": item.get("html_url"),
        "repository": {"nameWithOwner": full_name},
    }


def _github_pr_from_item_with_comments(item: dict[str, object]) -> dict[str, object]:
    pull_request = _github_pr_from_search_item(item)
    if not pull_request["repository"]["nameWithOwner"]:
        repository = item.get("repository")
        if isinstance(repository, dict):
            pull_request["repository"] = {"nameWithOwner": repository.get("full_name")}
    return pull_request


def load_github_review_activities(
    login: str, scope: Scope, headers: dict[str, str]
) -> tuple[GitHubActivity, ...]:
    start_date, end_date = _github_scope_dates_for_search(scope)
    items = _github_search_items(
        f"org:{GITHUB_ACTIVITY_ORG} is:pr reviewed-by:{login} updated:{start_date}..{end_date}",
        headers,
        GITHUB_ACTIVITY_LIMIT,
        "reviewed pull request search",
    )
    activities: dict[tuple[str, str, int, str], GitHubActivity] = {}
    for item in items:
        repository = item.get("repository_url")
        number = item.get("number")
        if not isinstance(repository, str) or not isinstance(number, int):
            continue
        reviews_url = f"{repository}/pulls/{number}/reviews"
        reviews = _github_get_json(
            f"{reviews_url}?{urllib.parse.urlencode({'per_page': 100})}", headers, "pull request reviews"
        )
        if not isinstance(reviews, list):
            raise GitHubActivityError("GitHub returned an unexpected reviews response.")
        for review in reviews:
            if not isinstance(review, dict):
                continue
            author = review.get("user")
            submitted_at = review.get("submitted_at")
            if not isinstance(author, dict) or author.get("login") != login:
                continue
            if not _github_in_scope(submitted_at, scope):
                continue
            _github_add_activity(
                activities,
                GitHubActivityKind.REVIEWED,
                _github_pr_from_search_item(item),
                submitted_at,
                str(review.get("state", "Review")),
            )
    return tuple(sorted(activities.values(), key=lambda item: item.occurred_at, reverse=True))


def parse_github_comment_search(payload: object, scope: Scope, login: str) -> tuple[GitHubActivity, ...]:
    if not isinstance(payload, dict):
        raise GitHubActivityError("GitHub returned an unexpected comment response.")
    items = payload.get("items")
    if not isinstance(items, list):
        raise GitHubActivityError("GitHub returned an unexpected comment response.")
    start_ms, end_ms = scope.bounds_ms()
    activities: dict[tuple[str, str, int, str], GitHubActivity] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        comments = item.get("comments", [])
        if not isinstance(comments, list):
            continue
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            author = comment.get("user")
            occurred_at = comment.get("created_at")
            if not isinstance(author, dict) or author.get("login") != login:
                continue
            timestamp = _github_iso_datetime(occurred_at)
            if timestamp is None:
                continue
            timestamp_ms = int(timestamp.timestamp() * 1000)
            if not start_ms <= timestamp_ms < end_ms:
                continue
            activity = _github_activity_from_pull_request(
                GitHubActivityKind.COMMENTED,
                {
                    "number": item.get("number"),
                    "title": item.get("title"),
                    "url": item.get("html_url"),
                "repository": {
                    "nameWithOwner": (
                        item.get("repository", {}).get("full_name")
                        if isinstance(item.get("repository"), dict)
                        else None
                    )
                },
                },
                occurred_at,
                "Commented",
            )
            if activity:
                activities[(activity.kind.value, activity.repository, activity.number, activity.occurred_at)] = activity
    return tuple(sorted(activities.values(), key=lambda item: item.occurred_at, reverse=True))


def load_github_comments(
    login: str, scope: Scope, headers: dict[str, str]
) -> tuple[GitHubActivity, ...]:
    start, end = _github_scope_dates(scope)
    start_date = start[:10]
    end_date = (datetime.fromisoformat(end.replace("Z", "+00:00")) - timedelta(microseconds=1)).date()
    items = _github_search_items(
        f"org:{GITHUB_ACTIVITY_ORG} commenter:{login} is:pr updated:{start_date}..{end_date}",
        headers,
        GITHUB_ACTIVITY_LIMIT,
        "pull request conversation comment search",
    )
    items_with_comments: list[dict[str, object]] = []
    for item in items:
        comments_url = item.get("comments_url")
        if not isinstance(comments_url, str):
            continue
        comments = _github_get_json(
            f"{comments_url}?{urllib.parse.urlencode({'per_page': 100})}",
            headers,
            "pull request conversation comments",
        )
        item_with_comments = dict(item)
        item_with_comments["comments"] = comments
        items_with_comments.append(item_with_comments)
    return parse_github_comment_search({"items": items_with_comments}, scope, login)


def load_github_activity(
    scope: Scope,
    force_refresh: bool = False,
    credentials: tuple[str, str] | None = None,
) -> GitHubActivityResult:
    credentials = credentials or github_credentials()
    if credentials is None:
        return GitHubActivityResult(available=False, error="GitHub credentials are not configured.")
    headers = github_request_headers(credentials[1])
    try:
        user_payload = _github_get_json(GITHUB_USER_URL, headers, "authenticated user lookup")
        login = user_payload.get("login") if isinstance(user_payload, dict) else None
        if not isinstance(login, str) or not login:
            raise GitHubActivityError("GitHub did not return the authenticated user.")
        cache_key = (login, scope.kind.value, scope.custom_date)
        cached = _github_activity_cache.get(cache_key)
        if not force_refresh and cached and time.monotonic() - cached[0] < GITHUB_ACTIVITY_CACHE_TTL:
            return cached[1]
        _github_organization_access(headers)

        start_date, end_date = _github_scope_dates_for_search(scope)
        activities: dict[tuple[str, str, int, str], GitHubActivity] = {}
        opened_items = _github_search_items(
            f"org:{GITHUB_ACTIVITY_ORG} is:pr author:{login} created:{start_date}..{end_date}",
            headers,
            GITHUB_ACTIVITY_LIMIT,
            "opened pull request search",
        )
        for item in opened_items:
            created_at = item.get("created_at")
            if _github_in_scope(created_at, scope):
                _github_add_activity(activities, GitHubActivityKind.OPENED, _github_pr_from_search_item(item), created_at)

        merged_items = _github_search_items(
            f"org:{GITHUB_ACTIVITY_ORG} is:pr is:merged author:{login} updated:{start_date}..{end_date}",
            headers,
            GITHUB_ACTIVITY_LIMIT,
            "merged pull request search",
        )
        for item in merged_items:
            pull_request_url = item.get("pull_request", {}).get("url") if isinstance(item.get("pull_request"), dict) else None
            if not isinstance(pull_request_url, str):
                continue
            detail = _github_get_json(pull_request_url, headers, "pull request details")
            merged_at = detail.get("merged_at") if isinstance(detail, dict) else None
            if _github_in_scope(merged_at, scope):
                _github_add_activity(activities, GitHubActivityKind.MERGED, _github_pr_from_search_item(item), merged_at, "Merged")

        for activity in load_github_review_activities(login, scope, headers):
            activities[(activity.kind.value, activity.repository, activity.number, activity.occurred_at)] = activity
        comments = load_github_comments(login, scope, headers)
        for activity in comments:
            activities[(activity.kind.value, activity.repository, activity.number, activity.occurred_at)] = activity
        result = GitHubActivityResult(
            tuple(sorted(activities.values(), key=lambda item: item.occurred_at, reverse=True)),
            True,
            None,
            login,
        )
        _github_activity_cache[cache_key] = (time.monotonic(), result)
        return result
    except (OSError, UnicodeError, json.JSONDecodeError, GitHubActivityError) as exc:
        return GitHubActivityResult(available=False, error=str(exc))


def check_for_update(force: bool = False) -> UpdateInfo | None:
    if not force and not should_check_for_update():
        return None

    request = urllib.request.Request(
        GITHUB_RELEASES_URL,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{PACKAGE_NAME}/{app_version()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    credentials = github_credentials()
    if credentials is not None:
        request.add_header("Authorization", f"Bearer {credentials[1]}")
    try:
        with urllib.request.urlopen(request, timeout=UPDATE_CHECK_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        tag_name = payload["tag_name"]
        remote_version = tag_name.removeprefix("v")
        Version(remote_version)
        wheel = next(
            asset
            for asset in payload["assets"]
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and asset["name"].endswith(".whl")
        )
    except (
        OSError,
        KeyError,
        StopIteration,
        TypeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        InvalidVersion,
    ):
        return None
    finally:
        mark_update_checked()

    if Version(remote_version) <= Version(app_version()):
        return None
    return UpdateInfo(
        version=remote_version,
        release_url=payload["html_url"],
        asset_url=wheel["url"],
    )


def install_update(update: UpdateInfo) -> tuple[bool, str]:
    credentials = github_credentials()
    request = urllib.request.Request(
        update.asset_url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": f"{PACKAGE_NAME}/{app_version()}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    if credentials is not None:
        request.add_header("Authorization", f"Bearer {credentials[1]}")
    wheel_path: str | None = None
    try:
        with urllib.request.urlopen(request, timeout=UPDATE_CHECK_TIMEOUT) as response:
            with tempfile.NamedTemporaryFile(suffix=".whl", delete=False) as wheel_file:
                wheel_file.write(response.read())
                wheel_path = wheel_file.name
        completed = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", wheel_path],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, urllib.error.URLError) as exc:
        return False, str(exc)
    finally:
        try:
            if wheel_path is not None:
                os.unlink(wheel_path)
        except OSError:
            pass
    if completed.returncode != 0:
        return False, completed.stderr.strip() or "pip exited unsuccessfully"
    return True, completed.stdout.strip() or "Updated successfully."


def restart_application() -> None:
    os.execv(sys.executable, [sys.executable, "-m", "opencode_standup.app", *sys.argv[1:]])


# --------------------------------------------------------------------------
# Time scopes
# --------------------------------------------------------------------------


class ScopeKind(Enum):
    TODAY = "today"
    LAST_WORKDAY = "last_workday"
    WEEK = "week"
    SPRINT = "sprint"
    ALL = "all"
    CUSTOM = "custom"


SCOPE_LABELS = {
    ScopeKind.TODAY: "Today",
    ScopeKind.LAST_WORKDAY: "Last workday",
    ScopeKind.WEEK: "This week",
    ScopeKind.SPRINT: "Current sprint",
    ScopeKind.ALL: "All time",
    ScopeKind.CUSTOM: "Custom date",
}

CURRENT_SPRINT_START = date(2026, 9, 14)
CURRENT_SPRINT_END = date(2026, 9, 25)


def last_workday(today: date) -> date:
    weekday = today.weekday()  # Monday=0 ... Sunday=6
    if weekday == 0:
        delta = 3
    elif weekday == 6:
        delta = 2
    else:
        delta = 1
    return today - timedelta(days=delta)


def day_bounds_ms(day: date) -> tuple[int, int]:
    """Return [start, end) epoch-ms bounds for the given local calendar day."""
    start_dt = datetime(day.year, day.month, day.day)
    end_dt = start_dt + timedelta(days=1)
    return int(start_dt.timestamp() * 1000), int(end_dt.timestamp() * 1000)


@dataclass
class Scope:
    kind: ScopeKind
    custom_date: date | None = None

    def label(self) -> str:
        if self.kind == ScopeKind.CUSTOM and self.custom_date:
            return self.custom_date.strftime("%A, %B %d, %Y")
        return SCOPE_LABELS[self.kind]

    def bounds_ms(self) -> tuple[int, int]:
        today = date.today()
        if self.kind == ScopeKind.TODAY:
            return day_bounds_ms(today)
        if self.kind == ScopeKind.LAST_WORKDAY:
            return day_bounds_ms(last_workday(today))
        if self.kind == ScopeKind.WEEK:
            monday = today - timedelta(days=today.weekday())
            start_ms, _ = day_bounds_ms(monday)
            _, end_ms = day_bounds_ms(today)
            return start_ms, end_ms
        if self.kind == ScopeKind.SPRINT:
            start_ms, _ = day_bounds_ms(CURRENT_SPRINT_START)
            _, end_ms = day_bounds_ms(CURRENT_SPRINT_END)
            return start_ms, end_ms
        if self.kind == ScopeKind.CUSTOM and self.custom_date:
            return day_bounds_ms(self.custom_date)
        # ALL
        return 0, int(datetime(2999, 1, 1).timestamp() * 1000)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass
class SessionRow:
    id: str
    parent_id: str | None
    project_id: str
    directory: str
    title: str
    agent: str | None
    time_created: int
    time_archived: int | None
    cost: float
    tokens_input: int
    tokens_output: int
    tokens_reasoning: int
    tokens_cache_read: int
    tokens_cache_write: int
    additions: int
    deletions: int
    files: int


@dataclass
class TaskTotals:
    cost: float = 0.0
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_reasoning: int = 0
    tokens_cache_read: int = 0
    tokens_cache_write: int = 0
    additions: int = 0
    deletions: int = 0
    files: int = 0
    subagent_count: int = 0

    def add(self, s: SessionRow, is_root: bool) -> None:
        self.cost += s.cost
        self.tokens_input += s.tokens_input
        self.tokens_output += s.tokens_output
        self.tokens_reasoning += s.tokens_reasoning
        self.tokens_cache_read += s.tokens_cache_read
        self.tokens_cache_write += s.tokens_cache_write
        self.additions += s.additions
        self.deletions += s.deletions
        self.files += s.files
        if not is_root:
            self.subagent_count += 1

    @property
    def total_tokens(self) -> int:
        return (
            self.tokens_input
            + self.tokens_output
            + self.tokens_reasoning
            + self.tokens_cache_read
            + self.tokens_cache_write
        )


@dataclass
class Task:
    root: SessionRow
    totals: TaskTotals = field(default_factory=TaskTotals)


@dataclass
class ProjectGroup:
    name: str
    tasks: list[Task] = field(default_factory=list)

    @property
    def totals(self) -> TaskTotals:
        agg = TaskTotals()
        for t in self.tasks:
            agg.cost += t.totals.cost
            agg.tokens_input += t.totals.tokens_input
            agg.tokens_output += t.totals.tokens_output
            agg.tokens_reasoning += t.totals.tokens_reasoning
            agg.tokens_cache_read += t.totals.tokens_cache_read
            agg.tokens_cache_write += t.totals.tokens_cache_write
            agg.additions += t.totals.additions
            agg.deletions += t.totals.deletions
            agg.files += t.totals.files
            agg.subagent_count += t.totals.subagent_count
        return agg


@dataclass(frozen=True)
class JiraIssue:
    key: str
    summary: str = ""
    status: str = ""
    url: str = ""


@dataclass(frozen=True)
class SummaryContext:
    workday: date
    text: str
    jira_issues: tuple[JiraIssue, ...]
    jira_available: bool
    jira_error: str | None = None
    github_activities: tuple[GitHubActivity, ...] = ()
    github_available: bool = True
    github_error: str | None = None


@dataclass(frozen=True)
class SummaryResult:
    text: str
    jira_available: bool
    jira_error: str | None = None
    github_available: bool = True
    github_error: str | None = None


class SummaryGenerationError(RuntimeError):
    pass


def is_summary_session(session: SessionRow) -> bool:
    title = session.title.strip()
    return title == SUMMARY_SESSION_TITLE or title.startswith(LEGACY_SUMMARY_TITLE_PREFIX)


# --------------------------------------------------------------------------
# DB access (reads are always via a read-only connection)
# --------------------------------------------------------------------------


def _ro_connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def load_sessions(db_path: str) -> list[SessionRow]:
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, parent_id, project_id, directory, title, agent, time_created,
                   time_archived, cost, tokens_input, tokens_output, tokens_reasoning,
                   tokens_cache_read, tokens_cache_write,
                   summary_additions, summary_deletions, summary_files
            FROM session
            """
        ).fetchall()
    finally:
        conn.close()

    return [
        SessionRow(
            id=r["id"],
            parent_id=r["parent_id"],
            project_id=r["project_id"],
            directory=r["directory"],
            title=r["title"] or "(untitled session)",
            agent=r["agent"],
            time_created=r["time_created"],
            time_archived=r["time_archived"],
            cost=r["cost"] or 0.0,
            tokens_input=r["tokens_input"] or 0,
            tokens_output=r["tokens_output"] or 0,
            tokens_reasoning=r["tokens_reasoning"] or 0,
            tokens_cache_read=r["tokens_cache_read"] or 0,
            tokens_cache_write=r["tokens_cache_write"] or 0,
            additions=r["summary_additions"] or 0,
            deletions=r["summary_deletions"] or 0,
            files=r["summary_files"] or 0,
        )
        for r in rows
    ]


def load_project_names(db_path: str) -> dict[str, str]:
    conn = _ro_connect(db_path)
    try:
        rows = conn.execute("SELECT id, worktree, name FROM project").fetchall()
    finally:
        conn.close()
    return {
        r["id"]: (r["name"] or os.path.basename(r["worktree"].rstrip("/")) or r["worktree"])
        for r in rows
    }


def extract_jira_keys(text: str) -> set[str]:
    return {match.upper() for match in JIRA_KEY_RE.findall(text)}


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _summary_session_ids(
    sessions: list[SessionRow],
    scope: Scope,
    include_archived: bool,
) -> set[str]:
    start_ms, end_ms = scope.bounds_ms()
    roots = {
        session.id
        for session in sessions
        if session.parent_id is None
        and not is_summary_session(session)
        and start_ms <= session.time_created < end_ms
        and (include_archived or session.time_archived is None)
    }
    children: dict[str, list[str]] = defaultdict(list)
    by_id = {session.id: session for session in sessions}
    for session in sessions:
        if session.parent_id:
            children[session.parent_id].append(session.id)
    relevant_ids = set(roots)
    stack = list(roots)
    while stack:
        session_id = stack.pop()
        for child_id in children.get(session_id, []):
            child = by_id.get(child_id)
            if child is None or (not include_archived and child.time_archived is not None):
                continue
            if child_id not in relevant_ids:
                relevant_ids.add(child_id)
                stack.append(child_id)
    return relevant_ids


def load_session_excerpts(
    db_path: str,
    sessions: list[SessionRow],
    project_names: dict[str, str],
    scope: Scope,
    include_archived: bool = False,
) -> tuple[str, set[str]]:
    start_ms, end_ms = scope.bounds_ms()
    roots = [
        session
        for session in sessions
        if session.parent_id is None
        and not is_summary_session(session)
        and start_ms <= session.time_created < end_ms
        and (include_archived or session.time_archived is None)
    ]
    if not roots:
        return "No OpenCode sessions were recorded for this workday.", set()

    relevant_ids = _summary_session_ids(sessions, scope, include_archived)
    relevant_sessions = [session for session in sessions if session.id in relevant_ids]
    conn = _ro_connect(db_path)
    try:
        input_rows = conn.execute(
            "SELECT session_id, prompt FROM session_input "
            "WHERE session_id IN ({}) AND ? <= time_created AND time_created < ? "
            "ORDER BY time_created".format(
                ",".join("?" for _ in relevant_ids)
            ),
            (*relevant_ids, start_ms, end_ms),
        ).fetchall()
        part_rows = conn.execute(
            "SELECT p.session_id, p.data, m.data AS message_data "
            "FROM part p JOIN message m ON m.id = p.message_id "
            "WHERE p.session_id IN ({}) AND ? <= p.time_created AND p.time_created < ? "
            "ORDER BY p.time_created".format(
                ",".join("?" for _ in relevant_ids)
            ),
            (*relevant_ids, start_ms, end_ms),
        ).fetchall()
    finally:
        conn.close()

    prompts: dict[str, list[str]] = defaultdict(list)
    for row in input_rows:
        prompts[row["session_id"]].append(row["prompt"])
    assistant_text: dict[str, list[str]] = defaultdict(list)
    user_text: dict[str, list[str]] = defaultdict(list)
    for row in part_rows:
        try:
            data = json.loads(row["data"])
            message_data = json.loads(row["message_data"])
        except (TypeError, json.JSONDecodeError):
            continue
        if data.get("type") == "text" and data.get("text"):
            if message_data.get("role") == "user":
                user_text[row["session_id"]].append(data["text"])
            elif message_data.get("role") == "assistant":
                assistant_text[row["session_id"]].append(data["text"])

    sections: list[str] = []
    jira_keys: set[str] = set()
    for session in sorted(relevant_sessions, key=lambda item: item.time_created):
        project = project_names.get(session.project_id) or os.path.basename(
            session.directory.rstrip("/")
        ) or session.directory
        excerpts: list[str] = []
        session_prompts = user_text.get(session.id) or prompts.get(session.id, [])
        for prompt in session_prompts:
            excerpts.append(f"User: {_clip(prompt, 700)}")
        for response in assistant_text.get(session.id, [])[-1:]:
            excerpts.append(f"Assistant: {_clip(response, 700)}")
        raw_content = "\n".join(excerpts)
        jira_keys.update(extract_jira_keys(raw_content))
        content = raw_content
        if content:
            content = _clip(content, SUMMARY_SESSION_MAX_CHARS)
        section = (
            f"Project: {project}\n"
            f"Session: {session.title}\n"
            f"Changes: +{session.additions}/-{session.deletions} across {session.files} files\n"
            f"Cost: {fmt_cost(session.cost)}\n"
            f"Transcript excerpts:\n{content or '(none)'}"
        )
        sections.append(section)
        jira_keys.update(extract_jira_keys(session.title))

    return _clip("\n\n---\n\n".join(sections), SUMMARY_MAX_CHARS), jira_keys


def parse_jira_output(raw: str) -> list[JiraIssue]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SummaryGenerationError("twg returned invalid JSON.") from exc
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    items = data.get("items") if isinstance(data, dict) else None
    if isinstance(data, dict) and (
        data.get("error") or (isinstance(items, dict) and items.get("error"))
    ):
        raise SummaryGenerationError("twg returned a Jira error response.")
    sections = items.get("sections", {}) if isinstance(items, dict) else {}
    if not isinstance(sections, dict):
        raise SummaryGenerationError("twg returned an unexpected Jira response.")
    issues = sections.get("issues", []) if isinstance(sections, dict) else []
    return [
        JiraIssue(
            key=item.get("key", ""),
            summary=item.get("summary", ""),
            status=item.get("status", ""),
            url=item.get("webUrl", ""),
        )
        for item in issues
        if item.get("key")
    ]


def load_jira_context(workday: date) -> tuple[list[JiraIssue], bool, str | None]:
    command = [
        "twg",
        "work",
        "query",
        "--scope",
        "me",
        "--from",
        workday.isoformat(),
        "--to",
        (workday + timedelta(days=1)).isoformat(),
        "--types",
        "jira",
        "--activity",
        "all",
        "--hydrate",
        "summary",
        "--items-per-section",
        "50",
        "--output",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=JIRA_COMMAND_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return [], False, str(exc)
    if completed.returncode != 0:
        return [], False, completed.stderr.strip() or "twg exited unsuccessfully"
    raw_output = completed.stdout
    if not raw_output.lstrip().startswith("{"):
        match = re.search(r'^  stdout: "(.+)"$', raw_output, re.MULTILINE)
        if match:
            try:
                artifact_path = json.loads(f'"{match.group(1)}"')
                with open(artifact_path, encoding="utf-8") as artifact:
                    raw_output = artifact.read()
            except (OSError, json.JSONDecodeError) as exc:
                return [], False, f"Could not read twg output: {exc}"
    try:
        issues = parse_jira_output(raw_output)
    except SummaryGenerationError as exc:
        return [], False, str(exc)
    return issues, True, None


def build_summary_context(
    db_path: str,
    sessions: list[SessionRow],
    project_names: dict[str, str],
    scope: Scope,
    include_archived: bool = False,
) -> SummaryContext:
    workday = last_workday(date.today()) if scope.kind == ScopeKind.LAST_WORKDAY else date.today()
    transcript, chat_keys = load_session_excerpts(
        db_path, sessions, project_names, scope, include_archived
    )
    jira_issues, jira_available, jira_error = load_jira_context(workday)
    github_result = load_github_activity(scope)
    by_key = {issue.key.upper(): issue for issue in jira_issues}
    for key in sorted(chat_keys):
        by_key.setdefault(key, JiraIssue(key=key))
    jira_text = "\n".join(
        f"{issue.key}: {issue.summary or '(mentioned in chat; no Jira activity result)'}"
        f" [{issue.status}] {issue.url}".strip()
        for issue in by_key.values()
    )
    github_lines = [
        f"{activity.kind.value}: {activity.repository}#{activity.number} {activity.title} "
        f"[{activity.detail or 'activity'}] {activity.url}"
        for activity in github_result.activities[:GITHUB_SUMMARY_ACTIVITY_LIMIT]
    ]
    github_omission = "\n(additional GitHub activity omitted)"
    github_text = _clip(
        "\n".join(github_lines),
        GITHUB_SUMMARY_MAX_CHARS - (len(github_omission) if len(github_result.activities) > len(github_lines) else 0),
    )
    if len(github_result.activities) > len(github_lines):
        github_text += github_omission
    return SummaryContext(
        workday=workday,
        text=(
            f"OpenCode work:\n{transcript}\n\n"
            f"Jira context:\n{jira_text or '(none)'}\n\n"
            f"GitHub activity:\n{github_text or '(none)'}"
        ),
        jira_issues=tuple(by_key.values()),
        jira_available=jira_available,
        jira_error=jira_error,
        github_activities=github_result.activities,
        github_available=github_result.available,
        github_error=github_result.error,
    )


def parse_opencode_output(raw: str) -> str:
    response_parts: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        part = event.get("part", {}) if isinstance(event, dict) else {}
        if part.get("type") == "text" and part.get("text"):
            response_parts.append(part["text"])
        elif event.get("type") == "text" and event.get("text"):
            response_parts.append(event["text"])
    result = "\n".join(response_parts).strip()
    if not result:
        raise SummaryGenerationError("OpenCode returned no summary text.")
    return result


def generate_summary(context: SummaryContext) -> str:
    jira_note = (
        "Jira activity was unavailable; use only chat-derived Jira references."
        if not context.jira_available
        else "Jira activity was available and may be used when supported by the context."
    )
    github_note = (
        "GitHub activity was unavailable; do not infer GitHub work."
        if not context.github_available
        else "GitHub activity was available and may be used when supported by the context."
    )
    prompt = f"""Create a factual standup summary for {context.workday.isoformat()}.

Output exactly:
1. A concise 1-2 sentence narrative recap.
2. Markdown sections with bullets: Progress, Jira work, Blockers or risks, Next steps.

Use only the supplied context. Do not invent work, ticket details, blockers, or next steps.
Mention uncertainty when evidence is incomplete. Include Jira keys and links when supplied.
Do not call tools or inspect files; answer directly from this context.
{jira_note}
{github_note}

Context:
{context.text}
"""
    try:
        completed = subprocess.run(
            [
                "opencode",
                "run",
                "--format",
                "json",
                "--title",
                SUMMARY_SESSION_TITLE,
                prompt,
            ],
            capture_output=True,
            text=True,
            timeout=SUMMARY_COMMAND_TIMEOUT,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise SummaryGenerationError(str(exc)) from exc
    if completed.returncode != 0:
        raise SummaryGenerationError(completed.stderr.strip() or "opencode exited unsuccessfully")
    return parse_opencode_output(completed.stdout)


def generate_last_workday_summary(db_path: str, include_archived: bool) -> SummaryResult:
    sessions = load_sessions(db_path)
    project_names = load_project_names(db_path)
    scope = Scope(ScopeKind.LAST_WORKDAY)
    context = build_summary_context(
        db_path,
        sessions,
        project_names,
        scope,
        include_archived,
    )
    return SummaryResult(
        text=generate_summary(context),
        jira_available=context.jira_available,
        jira_error=context.jira_error,
        github_available=context.github_available,
        github_error=context.github_error,
    )


# --------------------------------------------------------------------------
# Direct-DB fallback: unarchive
#
# opencode's REST API supports archiving a session (PATCH .../session/:id
# with {"time": {"archived": <ms>}}) but does not currently expose a
# supported way to clear that field again. Since this is a deliberate,
# narrow, reversible write to a single column (and opencode's sqlite db is
# in WAL mode, so a short write transaction is safe alongside a running
# opencode instance), we fall back to a direct UPDATE for this one action.
# --------------------------------------------------------------------------


def unarchive_session(db_path: str, session_id: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 3000")
        conn.execute(
            "UPDATE session SET time_archived = NULL, time_updated = ? WHERE id = ?",
            (int(time.time() * 1000), session_id),
        )
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------
# opencode server lifecycle + REST calls (used for rename / archive)
# --------------------------------------------------------------------------


class OpencodeServerError(RuntimeError):
    pass


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpencodeServer:
    """Lazily-started, on-demand `opencode serve` instance used only for
    mutating actions (rename / archive) so they go through opencode's
    supported REST API instead of writing to the database directly."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.base_url: str | None = None

    def ensure_started(self) -> str:
        if self.proc is not None and self.proc.poll() is None and self.base_url:
            return self.base_url

        if shutil.which("opencode") is None:
            raise OpencodeServerError(
                "opencode CLI not found on PATH; rename/archive are unavailable."
            )

        port = find_free_port()
        self.proc = subprocess.Popen(
            ["opencode", "serve", "--port", str(port), "--hostname", "127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.base_url = f"http://127.0.0.1:{port}"

        deadline = time.time() + 8
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/global/health", timeout=0.5) as resp:
                    if resp.status == 200:
                        return self.base_url
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                time.sleep(0.2)

        self.stop()
        raise OpencodeServerError(f"Timed out waiting for opencode server to start: {last_err}")

    def patch_session(self, session_id: str, body: dict) -> dict:
        base_url = self.ensure_started()
        url = f"{base_url}/session/{session_id}"
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="PATCH", headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OpencodeServerError(f"opencode API error ({exc.code}): {detail}") from exc
        except urllib.error.URLError as exc:
            raise OpencodeServerError(f"Could not reach opencode server: {exc}") from exc

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.base_url = None


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def build_report(
    sessions: list[SessionRow],
    project_names: dict[str, str],
    scope: Scope,
    include_archived: bool = False,
) -> list[ProjectGroup]:
    start_ms, end_ms = scope.bounds_ms()

    by_id = {s.id: s for s in sessions}
    children: dict[str, list[str]] = defaultdict(list)
    for s in sessions:
        if s.parent_id:
            children[s.parent_id].append(s.id)

    top_level = [
        s
        for s in sessions
        if s.parent_id is None
        and not is_summary_session(s)
        and start_ms <= s.time_created < end_ms
        and (include_archived or s.time_archived is None)
    ]

    groups: dict[str, ProjectGroup] = {}

    for root in top_level:
        totals = TaskTotals()
        stack = [root.id]
        seen: set[str] = set()
        while stack:
            sid = stack.pop()
            if sid in seen:
                continue
            seen.add(sid)
            node = by_id.get(sid)
            if node is None:
                continue
            totals.add(node, is_root=(sid == root.id))
            stack.extend(children.get(sid, []))

        task = Task(root=root, totals=totals)

        project_label = (
            project_names.get(root.project_id)
            or os.path.basename(root.directory.rstrip("/"))
            or root.directory
        )

        group = groups.setdefault(project_label, ProjectGroup(name=project_label))
        group.tasks.append(task)

    ordered = sorted(groups.values(), key=lambda g: g.totals.cost, reverse=True)
    for g in ordered:
        g.tasks.sort(key=lambda t: t.root.time_created)
    return ordered


# --------------------------------------------------------------------------
# Rendering helpers
# --------------------------------------------------------------------------


def fmt_cost(v: float) -> str:
    return f"${v:,.2f}"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_time(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%H:%M")


def fmt_date(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000).strftime("%b %d")


# --------------------------------------------------------------------------
# Modal screens
# --------------------------------------------------------------------------


class RenameModal(ModalScreen[str | None]):
    CSS = """
    RenameModal {
        align: center middle;
    }
    #dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #dialog Label {
        margin-bottom: 1;
    }
    """

    def __init__(self, current_title: str) -> None:
        super().__init__()
        self.current_title = current_title

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label("Rename session (Enter to save, Escape to cancel)")
            yield Input(value=self.current_title, id="rename-input")

    def on_mount(self) -> None:
        self.query_one("#rename-input", Input).focus()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    CSS = """
    ConfirmModal {
        align: center middle;
    }
    #dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self.message)
            with Container(id="buttons"):
                yield Button("Yes", id="yes", variant="error")
                yield Button("No", id="no", variant="primary")

    @on(Button.Pressed, "#yes")
    def yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def no(self) -> None:
        self.dismiss(False)

    def on_key(self, event) -> None:
        if event.key == "escape" or event.key == "n":
            self.dismiss(False)
        elif event.key == "y" or event.key == "enter":
            self.dismiss(True)


class UpdateModal(ModalScreen[str | None]):
    CSS = """
    UpdateModal {
        align: center middle;
    }
    #dialog {
        width: 76;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #message {
        margin-bottom: 1;
    }
    #buttons {
        height: auto;
        align: center middle;
    }
    #buttons Button {
        margin: 0 1;
    }
    """

    def __init__(self, update: UpdateInfo) -> None:
        super().__init__()
        self.update = update

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(
                f"A new version of opencode-standup is available.\n\n"
                f"Current version: {app_version()}\n"
                f"Available version: {self.update.version}\n\n"
                f"Release notes: {self.update.release_url}",
                id="message",
            )
            with Container(id="buttons"):
                yield Button("Update now", id="update", variant="success")
                yield Button("Later", id="later", variant="primary")
                yield Button("Open release notes", id="release-notes")

    @on(Button.Pressed, "#update")
    def update_now(self) -> None:
        self.dismiss("update")

    @on(Button.Pressed, "#later")
    def later(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#release-notes")
    def release_notes(self) -> None:
        webbrowser.open(self.update.release_url)

    def on_key(self, event) -> None:
        if event.key in ("escape", "n"):
            self.dismiss(None)
        elif event.key in ("enter", "y"):
            self.dismiss("update")


class GitHubSetupModal(ModalScreen[str | None]):
    CSS = """
    GitHubSetupModal {
        align: center middle;
    }
    #dialog {
        width: 82;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    #instructions {
        margin-bottom: 1;
    }
    #dialog Label {
        margin-top: 1;
    }
    #buttons {
        height: auto;
        align: center middle;
        margin-top: 1;
    }
    #buttons Button {
        margin: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(
                "GitHub setup\n\n"
                "Create a GitHub personal access token with repository read access. "
                "The token is validated with GitHub and stored locally with restricted permissions. "
                "The token itself is never displayed after setup.",
                id="instructions",
            )
            yield Label("Personal access token")
            yield Input(placeholder="github_pat_...", password=True, id="github-token")
            with Container(id="buttons"):
                yield Button("Open GitHub token settings", id="open-github-token")
                yield Button("Save and validate", id="save", variant="success")
                yield Button("Cancel", id="cancel", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#github-token", Input).focus()

    @on(Button.Pressed, "#open-github-token")
    def open_github_token_settings(self) -> None:
        webbrowser.open(GITHUB_TOKEN_URL)

    @on(Button.Pressed, "#save")
    def save(self) -> None:
        token = self.query_one("#github-token", Input).value.strip()
        self.dismiss(token or None)

    @on(Input.Submitted, "#github-token")
    def submit(self, event: Input.Submitted) -> None:
        self.save()

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss(None)


HELP_TEXT = """\
[b]opencode-standup — keybindings[/b]

[b]Global[/b]
  q          Quit
  r          Refresh current tab
  U          Check for updates
  ?          Show this help
  [ / ]      Previous / next tab

[b]Standup tab — time scope[/b]
  1          Today
  2          Last workday
  3          This week
  4          All time
  5          Current sprint
  s          Generate Last workday AI summary

[b]Actions (select a task/session row first)[/b]
  o          Open/resume this session in the real opencode TUI
  e          Rename this session
  x          Archive this session (hides it from view)
  A          Toggle showing archived sessions
  u          Unarchive the selected session (only when archived
             sessions are shown; this is a direct database write,
             since opencode's API has no unarchive endpoint yet)

[b]GitHub tab[/b]
  g          Refresh GitHub activity
  c          Set up GitHub credentials
  o          Open the selected pull request in a browser

Press any key to close this help.
"""


class HelpModal(ModalScreen[None]):
    CSS = """
    HelpModal {
        align: center middle;
    }
    #dialog {
        width: 64;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Static(HELP_TEXT)

    def on_key(self, event) -> None:
        self.dismiss(None)

    def on_click(self) -> None:
        self.dismiss(None)


# --------------------------------------------------------------------------
# Textual app
# --------------------------------------------------------------------------


class StandupApp(App):
    CSS = """
    Screen {
        layout: vertical;
    }
    #summary {
        height: auto;
        padding: 1 2;
        border: round $accent;
        margin: 1 2 0 2;
    }
    #ai-summary {
        height: auto;
        max-height: 18;
        overflow-y: auto;
        padding: 1 2;
        border: round $success;
        margin: 1 2 0 2;
    }
    #tree-container {
        height: 1fr;
        margin: 1 2;
    }
    #github-summary {
        height: auto;
        padding: 1 2;
        border: round $accent;
        margin: 1 2 0 2;
    }
    #github-tree-container {
        height: 1fr;
        margin: 1 2;
    }
    Tree {
        background: $surface;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_report", "Refresh"),
        Binding("e", "rename", "Rename", show=False),
        Binding("x", "archive", "Archive", show=False),
        Binding("A", "toggle_archived", "Show archived", show=False),
        Binding("u", "unarchive", "Unarchive", show=False),
        Binding("o", "open_session", "Open", show=False),
        ("1", "set_scope_today", "Today"),
        ("2", "set_scope_last_workday", "Last workday"),
        ("3", "set_scope_week", "Week"),
        ("4", "set_scope_all", "All"),
        ("5", "set_scope_sprint", "Sprint"),
        Binding("s", "generate_summary", "Generate summary", show=False),
        Binding("g", "refresh_github", "Refresh GitHub", show=False),
        Binding("c", "setup_github", "Set up GitHub", show=False),
        Binding("[", "prev_tab", "Prev tab", show=False),
        Binding("]", "next_tab", "Next tab", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("U", "check_for_updates", "Check for updates", show=False),
    ]

    def __init__(self, db_path: str, scope: Scope):
        super().__init__()
        self.db_path = db_path
        self.scope = scope
        self.include_archived = False
        self.groups: list[ProjectGroup] = []
        self.github_activities: tuple[GitHubActivity, ...] = ()
        self.github_available = True
        self.github_error: str | None = None
        self.github_loading = False
        self.github_request_id = 0
        self.github_worker: Worker[tuple[int, GitHubActivityResult]] | None = None
        self.github_credentials_override: tuple[str, str] | None = None
        self.server = OpencodeServer()
        self.summary_text: str | None = None
        self.summary_error: str | None = None
        self.summary_loading = False
        self.summary_jira_available = True
        self.summary_jira_error: str | None = None
        self.summary_github_available = True
        self.summary_github_error: str | None = None
        self.summary_worker: Worker[tuple[int, SummaryResult]] | None = None
        self.summary_request_id = 0
        self.update_worker: Worker[UpdateInfo | None] | None = None
        self.install_worker: Worker[tuple[bool, str]] | None = None
        self._pending_update: UpdateInfo | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="standup-tab"):
            with TabPane("Standup", id="standup-tab"):
                yield Markdown(id="ai-summary")
                yield Static(id="summary")
                with VerticalScroll(id="tree-container"):
                    yield Tree("opencode standup", id="report-tree")
            with TabPane("GitHub", id="github-tab"):
                yield Static(id="github-summary")
                with VerticalScroll(id="github-tree-container"):
                    yield Tree("GitHub activity", id="github-tree")
        yield Footer(compact=True, show_command_palette=False)

    def on_mount(self) -> None:
        self.title = "opencode standup"
        self.load_and_render()
        self._start_update_check()

    def on_unmount(self) -> None:
        self.server.stop()

    # -- data loading ------------------------------------------------------

    def load_and_render(self) -> None:
        self._invalidate_summary()
        sessions = load_sessions(self.db_path)
        project_names = load_project_names(self.db_path)

        self.groups = build_report(sessions, project_names, self.scope, self.include_archived)

        self.render_summary()
        self.render_tree()
        self._start_github_activity()
        self.render_github()

    @work(thread=True, exclusive=True, group="github-activity", exit_on_error=False)
    def _load_github_activity_worker(
        self, request_id: int, scope: Scope, force_refresh: bool
    ) -> tuple[int, GitHubActivityResult]:
        return request_id, load_github_activity(
            scope, force_refresh, self.github_credentials_override
        )

    def _start_github_activity(self, force_refresh: bool = False) -> None:
        self.github_request_id += 1
        self.github_loading = True
        self.github_activities = ()
        self.github_available = True
        self.github_error = None
        if self.is_mounted:
            self.render_github()
        self.github_worker = self._load_github_activity_worker(
            self.github_request_id, self.scope, force_refresh
        )

    @on(Worker.StateChanged)
    def _github_worker_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self.github_worker:
            return
        if event.state == WorkerState.SUCCESS:
            request_id, result = event.worker.result  # type: ignore[misc]
            if request_id != self.github_request_id:
                return
            self.github_activities = result.activities
            self.github_available = result.available
            self.github_error = result.error
        elif event.state == WorkerState.ERROR:
            self.github_available = False
            self.github_error = str(event.worker.error or "GitHub activity failed.")
        else:
            return
        self.github_loading = False
        self.render_github()

    def _invalidate_summary(self) -> None:
        self.summary_request_id += 1
        self.summary_text = None
        self.summary_error = None
        self.summary_loading = False
        self.summary_jira_available = True
        self.summary_jira_error = None
        self.summary_github_available = True
        self.summary_github_error = None
        if self.is_mounted:
            self.render_ai_summary()

    @work(thread=True, exclusive=True, group="update-check", exit_on_error=False)
    def _check_update_worker(self, force: bool = False) -> UpdateInfo | None:
        return check_for_update(force=force)

    def _start_update_check(self, force: bool = False) -> None:
        self.update_worker = self._check_update_worker(force)

    @on(Worker.StateChanged)
    def _update_worker_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self.update_worker:
            return
        if event.state != WorkerState.SUCCESS:
            return
        update = event.worker.result
        if update is None:
            return
        if not should_prompt_for_update(update):
            return
        self._pending_update = update
        self.push_screen(UpdateModal(update), self._handle_update_choice)

    def _handle_update_choice(self, choice: str | None) -> None:
        if choice != "update":
            update = self._pending_update
            if update is not None:
                mark_update_prompted(update.version)
            return
        self.notify("Updating opencode-standup...", timeout=10)
        if self._pending_update is None:
            self.notify("No update is pending.", severity="error")
            return
        self.install_worker = self._install_update_worker(self._pending_update)

    @work(thread=True, exclusive=True, group="install-update", exit_on_error=False)
    def _install_update_worker(self, update: UpdateInfo) -> tuple[bool, str]:
        return install_update(update)

    @on(Worker.StateChanged)
    def _install_worker_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self.install_worker:
            return
        if event.state == WorkerState.SUCCESS:
            success, message = event.worker.result
            if success:
                if self._pending_update is not None:
                    mark_update_prompted(self._pending_update.version)
                self.notify("Updated. Restarting opencode-standup...", timeout=5)
                self.call_after_refresh(restart_application)
            else:
                self.notify(f"Update failed: {message}", severity="error", timeout=10)
        elif event.state == WorkerState.ERROR:
            self.notify(f"Update failed: {event.worker.error}", severity="error", timeout=10)

    def render_ai_summary(self) -> None:
        widget = self.query_one("#ai-summary", Markdown)
        if self.scope.kind != ScopeKind.LAST_WORKDAY:
            widget.display = False
            return
        widget.display = True
        if self.summary_loading:
            widget.update("**AI standup summary**\n\nGenerating summary…")
        elif self.summary_error:
            widget.update(f"**AI standup summary**\n\n{self.summary_error}")
        elif self.summary_text:
            note = ""
            if not self.summary_jira_available:
                detail = self.summary_jira_error or "Jira context was unavailable."
                note = f"\n\n> _Chat-only summary: {detail}_"
            if not self.summary_github_available:
                detail = self.summary_github_error or "GitHub activity was unavailable."
                note += f"\n\n> _GitHub enrichment unavailable: {detail}_"
            widget.update(f"**AI standup summary**\n\n{self.summary_text}{note}")
        else:
            widget.update("**AI standup summary**\n\nPress `s` to generate.")

    @work(
        thread=True,
        exclusive=True,
        group="summary",
        exit_on_error=False,
        description="Generate last-workday AI summary",
    )
    def _generate_summary_worker(self, request_id: int) -> tuple[int, SummaryResult]:
        return request_id, generate_last_workday_summary(
            self.db_path, self.include_archived
        )

    @on(Worker.StateChanged)
    def _summary_worker_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not self.summary_worker:
            return
        if event.state == WorkerState.SUCCESS:
            request_id, result = event.worker.result  # type: ignore[misc]
            if request_id != self.summary_request_id:
                return
            self.summary_text = result.text
            self.summary_jira_available = result.jira_available
            self.summary_jira_error = result.jira_error
            self.summary_github_available = result.github_available
            self.summary_github_error = result.github_error
            self.summary_error = None
        elif event.state == WorkerState.ERROR:
            self.summary_error = str(event.worker.error or "Summary generation failed.")
        else:
            return
        self.summary_loading = False
        self.render_ai_summary()

    # -- Standup tab rendering ----------------------------------------------

    def render_summary(self) -> None:
        self.render_ai_summary()
        scope_label = self.scope.label()
        grand = TaskTotals()
        task_count = 0
        for g in self.groups:
            t = g.totals
            grand.cost += t.cost
            grand.tokens_input += t.tokens_input
            grand.tokens_output += t.tokens_output
            grand.tokens_reasoning += t.tokens_reasoning
            grand.tokens_cache_read += t.tokens_cache_read
            grand.tokens_cache_write += t.tokens_cache_write
            grand.additions += t.additions
            grand.deletions += t.deletions
            grand.files += t.files
            grand.subagent_count += t.subagent_count
            task_count += len(g.tasks)

        widget = self.query_one("#summary", Static)
        archived_note = "  [dim](showing archived)[/dim]" if self.include_archived else ""
        header_line = f"[b]{scope_label}[/b]{archived_note}"

        if not self.groups:
            widget.update(f"{header_line}\n\n[dim]No opencode sessions found for this scope.[/dim]")
            return

        lines = [
            header_line,
            "",
            f"[b]Total cost:[/b] {fmt_cost(grand.cost)}    "
            f"[b]Tasks:[/b] {task_count}    "
            f"[b]Projects:[/b] {len(self.groups)}    "
            f"[b]Subagent calls:[/b] {grand.subagent_count}",
            f"[b]Tokens[/b]  in: {fmt_tokens(grand.tokens_input)}  "
            f"out: {fmt_tokens(grand.tokens_output)}  "
            f"reasoning: {fmt_tokens(grand.tokens_reasoning)}  "
            f"cache-read: {fmt_tokens(grand.tokens_cache_read)}  "
            f"cache-write: {fmt_tokens(grand.tokens_cache_write)}  "
            f"[b](total: {fmt_tokens(grand.total_tokens)})[/b]",
            f"[b]Diff:[/b] +{grand.additions} / -{grand.deletions} across {grand.files} files",
        ]
        widget.update("\n".join(lines))

    def render_tree(self) -> None:
        tree = self.query_one("#report-tree", Tree)
        tree.clear()
        tree.root.expand()
        tree.show_root = False

        auto_collapse = self.scope.kind == ScopeKind.ALL and sum(len(g.tasks) for g in self.groups) > 15

        for g in self.groups:
            t = g.totals
            label = (
                f"[b]{g.name}[/b]  —  {len(g.tasks)} task(s), {fmt_cost(t.cost)}, "
                f"{fmt_tokens(t.total_tokens)} tokens"
            )
            project_node: TreeNode = tree.root.add(label, expand=not auto_collapse)
            for task in g.tasks:
                tt = task.totals
                time_str = (
                    fmt_time(task.root.time_created)
                    if self.scope.kind in (ScopeKind.TODAY, ScopeKind.LAST_WORKDAY, ScopeKind.CUSTOM)
                    else f"{fmt_date(task.root.time_created)} {fmt_time(task.root.time_created)}"
                )
                sub_note = f", {tt.subagent_count} subagent call(s)" if tt.subagent_count else ""
                diff_note = (
                    f", +{tt.additions}/-{tt.deletions} ({tt.files} files)"
                    if (tt.additions or tt.deletions)
                    else ""
                )
                archived_note = " [dim](archived)[/dim]" if task.root.time_archived else ""
                task_label = (
                    f"[cyan]{time_str}[/cyan]  {task.root.title}{archived_note}  "
                    f"[green]{fmt_cost(tt.cost)}[/green]  "
                    f"[dim]{fmt_tokens(tt.total_tokens)} tokens{sub_note}{diff_note}[/dim]"
                )
                project_node.add_leaf(task_label, data=task.root)

    def render_github(self) -> None:
        summary = self.query_one("#github-summary", Static)
        tree = self.query_one("#github-tree", Tree)
        tree.clear()
        tree.root.expand()
        tree.show_root = False
        if self.github_loading:
            summary.update("[b]GitHub activity[/b]\n\n[dim]Loading…[/dim]")
            return
        if not self.github_available:
            detail = self.github_error or "Unavailable."
            setup_hint = "\n\n[dim]Press `c` to connect GitHub." if "credentials" in detail.lower() else ""
            summary.update(f"[b]GitHub activity[/b]\n\n[red]{detail}[/red]{setup_hint}")
            return
        summary.update(
            f"[b]GitHub activity[/b]\n\n"
            f"[b]{len(self.github_activities)}[/b] activity item(s) for {self.scope.label()}"
            if self.github_activities
            else f"[b]GitHub activity[/b]\n\n[dim]No activity for {self.scope.label()}.[/dim]"
        )
        labels = {
            GitHubActivityKind.OPENED: "Opened PRs",
            GitHubActivityKind.MERGED: "Merged PRs authored by you",
            GitHubActivityKind.REVIEWED: "Reviewed PRs",
            GitHubActivityKind.COMMENTED: "PR conversation comments",
        }
        groups: dict[GitHubActivityKind, list[GitHubActivity]] = defaultdict(list)
        for activity in self.github_activities:
            groups[activity.kind].append(activity)
        for kind in GitHubActivityKind:
            activities = groups.get(kind, [])
            if not activities:
                continue
            group = tree.root.add(f"[b]{labels[kind]}[/b]  —  {len(activities)}", expand=True)
            for activity in activities:
                occurred = _github_iso_datetime(activity.occurred_at)
                when = occurred.astimezone().strftime("%b %d %H:%M") if occurred else activity.occurred_at
                detail = f" [{activity.detail}]" if activity.detail else ""
                group.add_leaf(
                    f"[cyan]{when}[/cyan]  {activity.repository}#{activity.number}  "
                    f"{activity.title}{detail}",
                    data=activity,
                )

    # -- helpers -------------------------------------------------------------

    def _active_tree(self) -> Tree:
        tabbed = self.query_one(TabbedContent)
        return self.query_one(
            "#github-tree" if tabbed.active == "github-tab" else "#report-tree", Tree
        )

    def _selected_data(self) -> object | None:
        node = self._active_tree().cursor_node
        return None if node is None else node.data

    def _selected_session(self) -> SessionRow | None:
        data = self._selected_data()
        return data if isinstance(data, SessionRow) else None

    def _selected_github_activity(self) -> GitHubActivity | None:
        data = self._selected_data()
        return data if isinstance(data, GitHubActivity) else None

    def _reload_scope_selector_label(self) -> None:
        # header/summary re-render already reflects scope; nothing extra needed
        pass

    # -- actions: scope switching ---------------------------------------------

    def action_set_scope_today(self) -> None:
        self.scope = Scope(ScopeKind.TODAY)
        self.load_and_render()

    def action_generate_summary(self) -> None:
        if self.scope.kind != ScopeKind.LAST_WORKDAY:
            self.notify("AI summaries are available for Last workday only.", severity="warning")
            return
        if self.summary_loading or (
            self.summary_worker is not None and self.summary_worker.is_running
        ):
            self.notify("A summary is already being generated.", severity="warning")
            return
        self.summary_request_id += 1
        self.summary_text = None
        self.summary_error = None
        self.summary_loading = True
        self.render_ai_summary()
        self.summary_worker = self._generate_summary_worker(self.summary_request_id)

    def action_set_scope_last_workday(self) -> None:
        self.scope = Scope(ScopeKind.LAST_WORKDAY)
        self.load_and_render()

    def action_set_scope_week(self) -> None:
        self.scope = Scope(ScopeKind.WEEK)
        self.load_and_render()

    def action_set_scope_all(self) -> None:
        self.scope = Scope(ScopeKind.ALL)
        self.load_and_render()

    def action_set_scope_sprint(self) -> None:
        self.scope = Scope(ScopeKind.SPRINT)
        self.load_and_render()

    # -- actions: refresh / help -------------------------------------------------

    def action_refresh_report(self) -> None:
        if self.query_one(TabbedContent).active == "github-tab":
            self._start_github_activity(force_refresh=True)
            return
        self.load_and_render()

    def action_refresh_github(self) -> None:
        self._start_github_activity(force_refresh=True)

    def action_setup_github(self) -> None:
        def handle_token(token: str | None) -> None:
            if not token:
                return
            self.notify("Validating GitHub token...", timeout=5)
            self.github_setup_worker = self._validate_github_worker(token)

        self.push_screen(GitHubSetupModal(), handle_token)

    @work(thread=True, exclusive=True, group="github-setup", exit_on_error=False)
    def _validate_github_worker(self, token: str) -> tuple[bool, str, str]:
        valid, result = validate_github_credentials(("", token))
        return valid, result, token

    @on(Worker.StateChanged)
    def _github_setup_worker_changed(self, event: Worker.StateChanged) -> None:
        if event.worker is not getattr(self, "github_setup_worker", None):
            return
        if event.state == WorkerState.SUCCESS:
            valid, result, token = event.worker.result
            if not valid:
                self.notify(result, severity="error", timeout=10)
                return
            try:
                save_github_credentials(result, token)
            except OSError as exc:
                self.notify(f"Could not save GitHub credentials: {exc}", severity="error", timeout=10)
                return
            self.github_credentials_override = (result, token)
            self.notify(f"GitHub connected as {result}.", timeout=5)
            self._start_github_activity()
        elif event.state == WorkerState.ERROR:
            self.notify(
                f"GitHub setup failed: {event.worker.error}", severity="error", timeout=10
            )

    def action_prev_tab(self) -> None:
        tabbed = self.query_one(TabbedContent)
        tabbed.active = "github-tab" if tabbed.active == "standup-tab" else "standup-tab"

    def action_next_tab(self) -> None:
        self.action_prev_tab()

    def action_check_for_updates(self) -> None:
        if self.update_worker is not None and self.update_worker.is_running:
            self.notify("Already checking for updates.", severity="warning")
            return
        self.notify("Checking for updates...", timeout=5)
        self._start_update_check(force=True)

    def action_show_help(self) -> None:
        self.push_screen(HelpModal())

    # -- actions: rename / archive / unarchive / open --------------------------

    def action_rename(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return

        def handle_result(new_title: str | None) -> None:
            if not new_title or new_title == session.title:
                return
            try:
                self.server.patch_session(session.id, {"title": new_title})
            except OpencodeServerError as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify(f"Renamed to “{new_title}”.")
            self.load_and_render()

        self.push_screen(RenameModal(session.title), handle_result)

    def action_archive(self) -> None:
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return
        if session.time_archived:
            self.notify("Already archived.", severity="warning")
            return

        def handle_result(confirmed: bool) -> None:
            if not confirmed:
                return
            try:
                self.server.patch_session(session.id, {"time": {"archived": int(time.time() * 1000)}})
            except OpencodeServerError as exc:
                self.notify(str(exc), severity="error", timeout=8)
                return
            self.notify("Archived.")
            self.load_and_render()

        self.push_screen(ConfirmModal(f"Archive “{session.title}”?"), handle_result)

    def action_toggle_archived(self) -> None:
        self.include_archived = not self.include_archived
        self.notify("Showing archived sessions." if self.include_archived else "Hiding archived sessions.")
        self.load_and_render()

    def action_unarchive(self) -> None:
        if not self.include_archived:
            self.notify("Press 'A' to show archived sessions first.", severity="warning")
            return
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return
        if not session.time_archived:
            self.notify("This session isn't archived.", severity="warning")
            return
        unarchive_session(self.db_path, session.id)
        self.notify("Unarchived.")
        self.load_and_render()

    def action_open_session(self) -> None:
        activity = self._selected_github_activity()
        if activity is not None:
            webbrowser.open(activity.url)
            return
        session = self._selected_session()
        if session is None:
            self.notify("Select a task/session first.", severity="warning")
            return
        if shutil.which("opencode") is None:
            self.notify("opencode CLI not found on PATH.", severity="error")
            return

        with self.suspend():
            subprocess.run(["opencode", session.directory, "--session", session.id])

        self.load_and_render()


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="A daily command-center for opencode sessions across your repos."
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {app_version()}",
    )
    parser.add_argument(
        "--date",
        dest="explicit_date",
        default=None,
        help="Start on a specific date, format YYYY-MM-DD, instead of the default scope.",
    )
    parser.add_argument(
        "--days-ago",
        dest="days_ago",
        type=int,
        default=None,
        help="Start on the day N days ago (0 = today, 1 = yesterday), instead of the default scope.",
    )
    parser.add_argument(
        "--scope",
        dest="scope",
        choices=["today", "last-workday", "week", "all", "sprint"],
        default=None,
        help="Start on a specific scope. Defaults to 'last-workday'.",
    )
    parser.add_argument(
        "--db",
        dest="db_path",
        default=os.environ.get("OPENCODE_STANDUP_DB", DEFAULT_DB_PATH),
        help=f"Path to opencode's sqlite db (default: {DEFAULT_DB_PATH}).",
    )
    args = parser.parse_args()

    db_path = os.path.expanduser(args.db_path)
    if not Path(db_path).exists():
        raise SystemExit(f"opencode database not found at: {db_path}")

    if args.explicit_date:
        scope = Scope(ScopeKind.CUSTOM, datetime.strptime(args.explicit_date, "%Y-%m-%d").date())
    elif args.days_ago is not None:
        scope = Scope(ScopeKind.CUSTOM, date.today() - timedelta(days=args.days_ago))
    elif args.scope:
        scope = Scope(
            {
                "today": ScopeKind.TODAY,
                "last-workday": ScopeKind.LAST_WORKDAY,
                "week": ScopeKind.WEEK,
                "all": ScopeKind.ALL,
                "sprint": ScopeKind.SPRINT,
            }[args.scope]
        )
    else:
        scope = Scope(ScopeKind.LAST_WORKDAY)

    app = StandupApp(db_path=db_path, scope=scope)
    app.run()


if __name__ == "__main__":
    main()
