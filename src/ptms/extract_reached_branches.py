"""
Extract release-to-branch reachability using local git history.

This script checks which branches contain each release tag, then stores the
reachability result and a few distance diagnostics for later release-line work.
"""

import os
import subprocess
import concurrent.futures
import itertools
from collections import defaultdict

import pandas as pd

from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utilities.RawFileConfig import RawFileConfig
from utilities.GitUtils import ensure_repo_cloned, _run_git

COMPARE_WORKERS = 24
START_FROM = None
REPO_CSV_PATH = "<path_to_filtered_repo_csv>"
RELEASE_CSV_PATH = "<path_to_filtered_release_csv>"

def list_releases(release_df: pd.DataFrame, repo_id):
    """
    Load one repo's tags in published order.
    """
    col_repo  = DBF.Releases.REPO_ID
    col_name  = DBF.Releases.TAG_NAME
    col_pubs  = DBF.Releases.PUBLISHED_AT

    tags = release_df[release_df[col_repo] == repo_id].copy()
    tags.sort_values(by=col_pubs, ascending=True, inplace=True)
    tags = tags[col_name].tolist()
    print(f"[releases] considering {len(tags)} tag(s) from DB.")
    return tags

def list_branches(repo_id):
    """
    Load known branch names for one repo from the database.
    """
    br_tbl   = DBT.BRANCHES.value
    col_repo = DBF.Branches.REPO_ID
    col_name = DBF.Branches.NAME
    sql = f"""
        SELECT {col_name}
        FROM {br_tbl}
        WHERE {col_repo} = %s
        ORDER BY {col_name} ASC
    """
    cur.execute(sql, (repo_id,))
    names = [r["name"] if isinstance(r, dict) else r[0] for r in cur.fetchall()]
    print(f"[branches] considering {len(names)} branch(es) from DB.")
    return names

def get_release_id_from_tag(repo_id, tag_name: str) -> int:
    """
    Resolve one tag name to the stored release id.
    """
    rel_tbl = DBT.RELEASES.value
    col_repo = DBF.Releases.REPO_ID
    col_tag  = DBF.Releases.TAG_NAME
    col_id   = DBF.Releases.ID
    sql = f"SELECT {col_id} FROM {rel_tbl} WHERE {col_repo}=%s AND {col_tag}=%s LIMIT 1"
    cur.execute(sql, (repo_id, tag_name))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"release_id not found for repo_id={repo_id}, tag={tag_name}")
    return int(row["id"] if isinstance(row, dict) else row[0])

def get_branch_id_from_name(repo_id: int, branch_name: str) -> int:
    """
    Resolve one branch name to the stored branch id.
    """
    br_tbl   = DBT.BRANCHES.value
    col_repo = DBF.Branches.REPO_ID
    col_name = DBF.Branches.NAME
    col_id   = DBF.Branches.ID
    sql = f"SELECT {col_id} FROM {br_tbl} WHERE {col_repo}=%s AND {col_name}=%s LIMIT 1"
    cur.execute(sql, (repo_id, branch_name))
    row = cur.fetchone()
    if not row:
        raise RuntimeError(f"branch_id not found for repo_id={repo_id}, branch={branch_name}")
    return int(row["id"] if isinstance(row, dict) else row[0])


def branch_ref(branch_name, repo_dir) -> str | None:
    """
    Resolve a branch name to a usable ref.
    Prefer remote-tracking (origin/<name>) without checkout.
    Return None if the branch does not exist locally or remotely.
    """
    # Check for the remote-tracking ref first to avoid a checkout.
    proc = subprocess.run(["git", "show-ref", "--verify", f"refs/remotes/origin/{branch_name}"],
                          cwd=repo_dir)
    if proc.returncode == 0:
        return f"origin/{branch_name}"

    # Fall back to a local branch if it already exists.
    proc = subprocess.run(["git", "show-ref", "--verify", f"refs/heads/{branch_name}"],
                          cwd=repo_dir)
    if proc.returncode == 0:
        return branch_name

    # Last try: look for the branch on origin and fetch only that ref.
    proc = subprocess.run(["git", "ls-remote", "--heads", "origin", branch_name],
                          cwd=repo_dir, stdout=subprocess.PIPE, text=True)
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            subprocess.run(
                ["git", "fetch", "origin",
                 f"+refs/heads/{branch_name}:refs/remotes/origin/{branch_name}"],
                cwd=repo_dir, check=True)
            return f"origin/{branch_name}"
        except subprocess.CalledProcessError:
            pass

    return None


def tag_to_commit_sha(tag, repo_dir) -> str:
    """
    Resolve a tag to the commit SHA behind it.
    """
    try:
        sha = _run_git(["rev-parse", f"{tag}^{{commit}}"], cwd=repo_dir)
    except RuntimeError:
        sha = _run_git(["rev-list", "-n", "1", tag], cwd=repo_dir)
    if not sha:
        raise RuntimeError(f"Cannot resolve tag to commit: {tag}")
    return sha

def compare_contains(tag_or_sha, branch, repo_dir) -> bool:
    """
    Check whether a tag commit is reachable from one branch tip.
    """
    tag_sha = _run_git(["rev-parse", tag_or_sha], cwd=repo_dir)
    b_ref   = branch_ref(branch, repo_dir)
    if b_ref is None:
        return False
    b_sha   = _run_git(["rev-parse", b_ref], cwd=repo_dir)

    proc = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_sha, b_sha],
        cwd=repo_dir,
    )
    return proc.returncode == 0

def _cmp_one(args):
    """
    Compare one tag-branch pair inside the thread pool.
    """
    t, b = args
    try:
        ok = compare_contains(tag_sha[t], b, repo_dir)
        return (t, b, ok)
    except Exception:
        return (t, b, False)

def classify_status(tag_sha: str, branch_name: str, repo_dir: str) -> str:
    """
    Classify the tag-branch relation with a small local git status label.
    """
    b_ref = branch_ref(branch_name, repo_dir)
    if b_ref is None:
        return "unknown"
    b_sha = _run_git(["rev-parse", b_ref], cwd=repo_dir)

    if tag_sha == b_sha:
        return "identical"

    # Tag is behind the branch tip but still on its history.
    rc = subprocess.run(["git", "merge-base", "--is-ancestor", tag_sha, b_sha], cwd=repo_dir)
    if rc.returncode == 0:
        return "ahead"

    # Branch is behind the tag.
    rc2 = subprocess.run(["git", "merge-base", "--is-ancestor", b_sha, tag_sha], cwd=repo_dir)
    if rc2.returncode == 0:
        return "behind"

    return "diverged"

def commits_ahead_count(tag_sha, branch_name, repo_dir) -> int | None:
    """
    Count how many commits the branch is ahead of the tag.
    """
    b_ref = branch_ref(branch_name, repo_dir)
    if b_ref is None:
        return None
    try:
        return int(_run_git(["rev-list","--count",f"{tag_sha}..{b_ref}"], cwd=repo_dir))
    except Exception:
        return None

def fp_commits_ahead_count(tag_sha, branch_name, repo_dir) -> int | None:
    """
    Count first-parent commits from the tag to the branch tip.
    """
    b_ref = branch_ref(branch_name, repo_dir)
    if b_ref is None:
        return None
    try:
        return int(_run_git(["rev-list","--first-parent","--count",f"{tag_sha}..{b_ref}"], cwd=repo_dir))
    except Exception:
        return None

def upsert_reachability(repo_id: int, release_id: int, branch_id: int,
                        tag_name: str, tag_sha: str, branch_name: str,
                        reachable: bool, status: str,
                        commits_ahead: int | None, fp_commits_ahead: int | None, repo_dir: str):
    """
    Insert or update one release-branch reachability row.
    """
    sql = """
    INSERT INTO release_branch_reachability
      (repo_id, release_id, branch_id, tag_name, tag_commit_sha,
       branch_ref, reachable, compare_status, commits_ahead, fp_commits_ahead, checked_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON DUPLICATE KEY UPDATE
      tag_name=VALUES(tag_name),
      tag_commit_sha=VALUES(tag_commit_sha),
      branch_ref=VALUES(branch_ref),
      reachable=VALUES(reachable),
      compare_status=VALUES(compare_status),
      commits_ahead=VALUES(commits_ahead),
      fp_commits_ahead=VALUES(fp_commits_ahead),
      checked_at=VALUES(checked_at)
    """
    params = (
        repo_id, release_id, branch_id, tag_name, tag_sha,
        branch_ref(branch_name, repo_dir), int(reachable), status,
        commits_ahead, fp_commits_ahead
    )
    cur.execute(sql, params)
    conn.commit()


if __name__ == "__main__":
    db_config = DatabaseConfig()
    conn, cur = db_config.create_db_connection()
    _raw = RawFileConfig(db=db_config.db)
    ROOT_PATH = _raw.root_path

    # Keep the input CSVs as simple user settings at the top of the script.
    repo_df = pd.read_csv(REPO_CSV_PATH)
    release_df = pd.read_csv(RELEASE_CSV_PATH)
    print(f"Original repos: {len(repo_df)}")
    print(f"Original releases: {len(release_df)}")

    # Default to all filtered repos unless a smaller subset is set manually.
    repo_ids = repo_df['repo_id'].tolist()

    release_df = release_df[release_df['repo_id'].isin(repo_ids)]

    for repo_id in repo_ids:

        repo_row = repo_df[repo_df['repo_id'] == repo_id].iloc[0]
        OWNER = repo_row['full_name'].split('/')[0]
        REPO  = repo_row['full_name'].split('/')[1]
        print(f"\n=== Processing repo_id={repo_id} ({OWNER}/{REPO}) ===")
        repo_dir  = os.path.join(ROOT_PATH, "repos", OWNER, REPO)

        remote_url = f"https://github.com/{OWNER}/{REPO}.git"

        print(f"[path] Using local clone at: {repo_dir}")

        # Make sure the local clone exists before asking git reachability questions.
        ensure_repo_cloned(repo_dir, remote_url)

        TAGS = list_releases(release_df, repo_id)
        if not TAGS:
            raise SystemExit("No tags to process.")

        # Resolve each release tag to the commit it points to.
        tag_sha = {}
        for t in TAGS:
            try:
                tag_sha[t] = tag_to_commit_sha(t, repo_dir)
                print(f"[tag] {t} → {tag_sha[t][:12]}")
            except Exception as e:
                print(f"[warn] skip tag {t}: {e}")

        if not tag_sha:
            raise SystemExit("No valid tags resolved.")

        # First find which branches are even valid candidates in the clone.
        branches = list_branches(repo_id)
        existing_branches = []
        missing_branches  = []
        for b in branches:
            if branch_ref(b, repo_dir) is not None:
                existing_branches.append(b)
            else:
                missing_branches.append(b)

        if missing_branches:
            print(f"[warn] {len(missing_branches)} branch(es) missing remotely or locally; skipping: {missing_branches[:5]}{' ...' if len(missing_branches)>5 else ''}")

        branches = existing_branches
        contains = defaultdict(list)

        jobs = list(itertools.product(tag_sha.keys(), branches))
        print(f"[compare] scanning {len(jobs)} tag–branch pairs...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=COMPARE_WORKERS) as ex:
            for t, b, ok in ex.map(_cmp_one, jobs):
                if ok:
                    contains[b].append(t)

        candidate_branches = sorted([b for b, ts in contains.items() if ts])
        print(f"[shortlist] {len(candidate_branches)} candidate branch(es): {candidate_branches}")

        # Optional local shortcut if you want to resume from one branch name.
        if START_FROM and START_FROM in branches:
            branches_to_process = branches[branches.index(START_FROM):]
        else:
            if START_FROM:
                print(f"[info] start branch {START_FROM!r} not found; processing all branches")
            branches_to_process = branches

        # Store every checked pair so later release-line steps can filter fast.
        for b in branches_to_process:
            print(f"[store] processing branch: {b}")
            try:
                branch_id = get_branch_id_from_name(repo_id, b)
            except Exception as e:
                print(f"[warn] skip store: cannot resolve branch_id for {b}: {e}")
                continue

            for t in TAGS:
                print(f"  - tag: {t}")
                try:
                    rls_id = get_release_id_from_tag(repo_id, t)
                except Exception as e:
                    print(f"[warn] skip store: cannot resolve release_id for tag {t}: {e}")
                    continue

                is_reachable = t in contains.get(b, [])

                # These extra values help later debugging and release-line selection.
                t_sha   = tag_sha.get(t)
                status  = classify_status(t_sha, b, repo_dir) if t_sha else "unknown"
                dist    = commits_ahead_count(t_sha, b, repo_dir) if t_sha else None
                dist_fp = fp_commits_ahead_count(t_sha, b, repo_dir) if t_sha else None

                try:
                    upsert_reachability(
                        repo_id=repo_id,
                        release_id=rls_id,
                        branch_id=branch_id,
                        tag_name=t,
                        tag_sha=t_sha,
                        branch_name=b,
                        reachable=is_reachable,
                        status=status,
                        commits_ahead=dist,
                        fp_commits_ahead=dist_fp,
                        repo_dir=repo_dir,
                    )
                except Exception as e:
                    print(f"[warn] upsert failed for repo_id={repo_id}, tag={t}, branch={b}: {e}")

        print(f"=== Finished repo_id={repo_id} ({OWNER}/{REPO}) ===\n")

    print("All done.")
