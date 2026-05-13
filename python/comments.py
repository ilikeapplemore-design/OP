#!/usr/bin/env python3
# ==============================================================================
# comments.py – Version 1.0.0
# ==============================================================================
# Generic helpers for working with GitHub issue comments via `gh api`.
#
# Every function that talks to the API receives the repository slug
# (e.g. "owner/repo") and the issue number, so this module can be used
# for any repository, any issue.
#
# Functions:
#   gh_api(*args, input_data=None, **kwargs) -> str          – raw gh api call
#   issue_comment(repo, issue_number, body) -> comment_id
#   get_all_comments(repo, issue_number) -> list[dict]       – paginated
#   find_marker_comment(comments, marker) -> dict or None
#   delete_comment(repo, comment_id) -> bool
#   edit_comment(repo, comment_id, new_body)
#   comment_exists(repo, comment_id) -> bool
#
# Usage:
#   from comments import get_all_comments, edit_comment
#   all_comments = get_all_comments("owner/repo", 4)
# ==============================================================================

import json
import re
import subprocess
from typing import Any, Dict, List, Optional


def gh_api(*args: str, input_data: Optional[str] = None, **kwargs: Any) -> str:
    """
    Run `gh api` with the given arguments and return stdout stripped.
    Raises `subprocess.CalledProcessError` on failure.
    """
    cmd = ["gh", "api"] + list(args)
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=True,
        input=input_data,
        **kwargs,
    )
    return res.stdout.strip()


def issue_comment(repo: str, issue_number: int, markdown_body: str) -> str:
    """Post a new comment and return its ID."""
    return gh_api(
        f"repos/{repo}/issues/{issue_number}/comments",
        "--method", "POST",
        "-f", f"body={markdown_body}",
        "--jq", ".id",
    )


def get_all_comments(repo: str, issue_number: int) -> List[Dict[str, str]]:
    """
    Fetch every comment of the given issue (paginated) and return a list of dicts
    with keys 'id', 'body', 'user_type'.
    """
    raw = gh_api(
        f"repos/{repo}/issues/{issue_number}/comments",
        "--jq", ".[] | {id: .id, body: .body, user_type: .user.type}",
        "--paginate",
    )
    if not raw.strip():
        return []

    comments: List[Dict[str, str]] = []
    decoder = json.JSONDecoder()
    idx = 0
    raw_len = len(raw)
    while idx < raw_len:
        while idx < raw_len and raw[idx].isspace():
            idx += 1
        if idx >= raw_len:
            break
        try:
            obj, end = decoder.raw_decode(raw, idx)
            comments.append(
                {
                    "id": str(obj.get("id", "")),
                    "body": obj.get("body", ""),
                    "user_type": obj.get("user_type", ""),
                }
            )
            idx = end
        except json.JSONDecodeError:
            # Malformed JSON at this position – skip and try the next object
            # (this should be extremely rare)
            idx += 1
    return comments


def find_marker_comment(comments: List[Dict[str, str]], marker: str) -> Optional[Dict[str, str]]:
    """Return the first comment whose body starts with `marker`, or None."""
    for c in comments:
        if c.get("body", "").startswith(marker):
            return c
    return None


def delete_comment(repo: str, comment_id: str) -> bool:
    """Delete a single comment. Returns True on success, False otherwise."""
    try:
        gh_api(f"repos/{repo}/issues/comments/{comment_id}", "--method", "DELETE")
        return True
    except subprocess.CalledProcessError:
        return False


def edit_comment(repo: str, comment_id: str, new_body: str) -> None:
    """Update the body of an existing comment (uses JSON input)."""
    gh_api(
        f"repos/{repo}/issues/comments/{comment_id}",
        "--method", "PATCH",
        "--input", "-",
        input_data=json.dumps({"body": new_body}),
    )


def comment_exists(repo: str, comment_id: str) -> bool:
    """Return True if the comment exists (HTTP 200)."""
    try:
        gh_api(f"repos/{repo}/issues/comments/{comment_id}", "--jq", ".id")
        return True
    except subprocess.CalledProcessError:
        return False


# ---------------------------------------------------------------------------
# Minimal self‑test (requires a real repo + gh CLI authenticated)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import os
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    issue = int(os.environ.get("ISSUE_NUMBER", "1"))
    if repo:
        comments = get_all_comments(repo, issue)
        print(f"Found {len(comments)} comments.")
    else:
        print("Set GITHUB_REPOSITORY and ISSUE_NUMBER to run self‑test.")
