"""
GitUtils module for handling Git repository operations.
- Provides functions to ensure a local mirror of a Git repository is cloned and up-to-date.
- Handles cloning, fetching, and safe removal of repositories.
- Implements retry logic for fetch operations to handle transient issues."""

import os
import shutil
import subprocess

def _safe_rmtree(path: str):
    """
    Safely remove a directory tree.
    Guards against accidental deletion of critical paths.
    """
    ap = os.path.abspath(path)
    # Very conservative guards
    if not os.path.isdir(ap):
        return
    if ap in ("/", os.path.expanduser("~")):
        raise RuntimeError(f"Refusing to remove dangerous path: {ap}")
    # Require a reasonably deep path (e.g., .../repos/<owner>/<repo>)
    if len(ap.strip(os.sep).split(os.sep)) < 3:
        raise RuntimeError(f"Refusing to remove shallow path: {ap}")
    shutil.rmtree(ap)

def _run_git(args, cwd, check=True, text=True):
    """Run a git command and return stdout (str). Raises on failure if check=True."""
    res = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout.strip()

def ensure_repo_cloned(repo_dir: str, remote_url: str, retry_on_fetch_failure: bool = True):
    """
    Ensure a local mirror exists and refs are up-to-date.
    If fetch fails and retry_on_fetch_failure=True, the function will remove the
    existing repo_dir and clone again from scratch, then fetch once more.
    """
    def _clone():
        os.makedirs(os.path.dirname(repo_dir), exist_ok=True)
        print(f"[git] cloning {remote_url} → {repo_dir}")
        subprocess.run(
            ["git", "clone", "--no-checkout", "--filter=blob:none", remote_url, repo_dir],
            check=True
        )

    def _fetch():
        print("[git] fetching remotes and tags")
        # prune heads and tags; refresh all remotes
        _run_git(["fetch", "--tags", "--prune", "--prune-tags", "--all"], cwd=repo_dir)

    # Clone if missing
    if not os.path.isdir(repo_dir) or not os.path.isdir(os.path.join(repo_dir, ".git")):
        _clone()
        try:
            _fetch()
            return
        except Exception as e:
            if not retry_on_fetch_failure:
                raise
            print(f"[git][warn] initial fetch failed after clone: {e}. Retrying with fresh clone...")
            _safe_rmtree(repo_dir)
            _clone()
            _fetch()
            return

    # Repo exists → try fetch; on failure, re-clone once
    try:
        _fetch()
    except Exception as e:
        if not retry_on_fetch_failure:
            raise
        print(f"[git][warn] fetch failed in existing repo: {e}. Removing and re-cloning...")
        try:
            _safe_rmtree(repo_dir)
        except Exception as re:
            raise RuntimeError(f"[git][error] failed to remove {repo_dir}: {re}") from e
        _clone()
        _fetch()

