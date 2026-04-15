"""
First-parent traversal to identify release lines (branch-based lines).

Input:
  - Table: release_branch_reachability
      (repo_id, release_id, branch_id, tag_name, tag_commit_sha, branch_ref,
       reachable, compare_status, commits_ahead, fp_commits_ahead, checked_at)

For each repo_id, collect candidate branches with reachable=1 plus their tags. For each candidate branch_ref, walk the first-parent chain in the local clone.

Output table:
  - release_line_assignment
      id (PK auto), repo_id, release_id, branch_id, branch_ref, tag_name, tag_commit_sha,
      assigned_branch, assigned_reason, fp_index, fp_found, ambiguous, computed_at
"""

import os
import pandas as pd
import subprocess
from collections import defaultdict

from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utilities.RawFileConfig import RawFileConfig
from utilities.GitUtils import ensure_repo_cloned, _run_git

STOP_WHEN_COVERED = True

# restrict to a slice of repos (optional)
REPO_SLICE = None

def fp_walk_order(repo_dir: str, branch_ref: str, targets=None):
    """
    Return:
      - fp_list: list of SHAs along first-parent from tip → older.
      - fp_index: dict SHA -> index (0-based). Missing if not on the first-parent path.
      - found_set: set of target SHAs found on the FP chain (if 'targets' provided).
    """
    # This is the linear release-line view from the branch tip, ignoring merged side branches.
    out = _run_git(["rev-list", "--first-parent", branch_ref], cwd=repo_dir)
    fp_list = [ln.strip() for ln in out.splitlines() if ln.strip()]
    fp_index = {sha: i for i, sha in enumerate(fp_list)}

    found_set = set()
    if targets:
        for sha in fp_list:
            if sha in targets:
                found_set.add(sha)
                if STOP_WHEN_COVERED and found_set == targets:
                    # Truncate only for clarity once all branch-local targets have been seen.
                    end = fp_index[sha] + 1
                    fp_list = fp_list[:end]
                    fp_index = {h: i for i, h in enumerate(fp_list)}
                    break

    return fp_list, fp_index, found_set

def pref_rank(branch_ref: str) -> tuple:
    """
    Rank key for branch selection:
      0: release/*, 1: main/master (with or without origin/), 2: others
    """
    b = branch_ref
    # Normalize short name (strip 'origin/' for policy check)
    short = b.split("/", 1)[1] if b.startswith("origin/") else b
    if short.startswith("release/"):
        return (0, short)
    if short in ("main", "master"):
        return (1, short)
    return (2, short)

# -------------- DB helpers ---------------------
def ensure_output_table(cur):
    cur.execute("""
    CREATE TABLE IF NOT EXISTS release_line_assignment (
        id INT AUTO_INCREMENT PRIMARY KEY,
        repo_id INT NOT NULL,
        release_id INT NOT NULL,
        branch_id INT NOT NULL,
        branch_ref VARCHAR(255) NOT NULL,
        tag_name VARCHAR(255) NOT NULL,
        tag_commit_sha CHAR(40) NOT NULL,
        assigned_branch VARCHAR(255) NOT NULL,
        assigned_reason VARCHAR(64) NOT NULL,
        fp_index INT NULL,
        fp_found TINYINT(1) NOT NULL,
        ambiguous TINYINT(1) NOT NULL,
        computed_at DATETIME NOT NULL,
        UNIQUE KEY uniq_repo_rel_branch_tag (repo_id, release_id, branch_id, tag_name)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
    """)

def fetch_repo_rows(conn):
    """
    Load the filtered repository list that should enter release-line extraction.
    """
    repo_df = pd.read_csv('data_files/final_repos_filtered_os.csv')
    if REPO_SLICE:
        repo_ids = repo_df['repo_id'].unique().tolist()
        repo_ids = repo_ids[REPO_SLICE[0]:REPO_SLICE[1]]
        repo_df = repo_df[repo_df['repo_id'].isin(repo_ids)]
    return repo_df

def list_releases(release_df: pd.DataFrame, repo_id, cur):
    """
    Load one repo's releases in published order and attach the stored tag SHA.
    """
    col_repo  = DBF.Releases.REPO_ID
    col_name  = DBF.Releases.TAG_NAME
    col_pubs  = DBF.Releases.PUBLISHED_AT

    df = release_df[release_df[col_repo] == repo_id].copy()
    df.sort_values(by=col_pubs, ascending=True, inplace=True)
    # Reuse the tag SHA already stored during branch reachability extraction.
    cur.execute("""
        SELECT tag_name, tag_commit_sha
        FROM release_branch_reachability
        WHERE repo_id=%s
    """, (repo_id, ))
    rows = cur.fetchall()
    tag_to_sha = {r['tag_name']: r['tag_commit_sha'] for r in rows}
    df['tag_commit_sha'] = df[col_name].map(tag_to_sha)
    print(f"[releases] considering {len(df)} tag(s) from DB.")
    return df

def tag_status(tag_sha_a: str, tag_sha_b: str, repo_dir: str) -> str:
    """
    Emulate GitHub compare status for two tag SHAs:
      - 'identical' if A == B
      - 'ahead'     if A <= B (A ancestor of B) and not identical
      - 'behind'    if B <= A
      - 'diverged'  otherwise
    """
    if tag_sha_a == tag_sha_b:
        return "identical"

    rc1 = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_sha_a, tag_sha_b],
        cwd=repo_dir,
    )
    if rc1.returncode == 0:
        return "ahead"

    rc2 = subprocess.run(
        ["git", "merge-base", "--is-ancestor", tag_sha_b, tag_sha_a],
        cwd=repo_dir,
    )
    if rc2.returncode == 0:
        return "behind"

    return "diverged"


def build_tag_linear_components(tag_sha_map: dict[str, str], repo_dir: str):
    """
    Build connected components of related tags when a repo has no branch assignment.
    """
    tags = list(tag_sha_map.keys())
    parent = {t: t for t in tags}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[parent[x]]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(len(tags)):
        for j in range(i + 1, len(tags)):
            ta, tb = tags[i], tags[j]
            sa, sb = tag_sha_map[ta], tag_sha_map[tb]
            st = tag_status(sa, sb, repo_dir)
            if st in ("ahead", "behind", "identical"):
                union(ta, tb)

    groups = defaultdict(list)
    for t in tags:
        groups[find(t)].append(t)

    return list(groups.values())

def fetch_reachability(cur, repo_id: int):
    """
    Pull only reachable pairs per repo:
      returns DataFrame with columns:
        release_id, branch_id, tag_name, tag_commit_sha, branch_ref
    """
    cur.execute("""
        SELECT release_id, branch_id, tag_name, tag_commit_sha, branch_ref
        FROM release_branch_reachability
        WHERE repo_id=%s AND reachable=1
    """, (repo_id,))
    rows = cur.fetchall()
    if not rows:
        return pd.DataFrame(columns=["release_id","branch_id","tag_name","tag_commit_sha","branch_ref"])
    # Convert rows (may be dicts or tuples)
    if isinstance(rows[0], dict):
        return pd.DataFrame(rows)
    else:
        cols = ["release_id","branch_id","tag_name","tag_commit_sha","branch_ref"]
        return pd.DataFrame([dict(zip(cols, r)) for r in rows])

def upsert_assignments(cur, conn, df_assign):
    """
    Save one release-line assignment row per retained release tag.
    """
    sql = """
    INSERT INTO release_line_assignment
      (repo_id, release_id, branch_id, branch_ref, tag_name, tag_commit_sha,
       assigned_branch, assigned_reason, fp_index, fp_found, ambiguous, computed_at)
    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
    ON DUPLICATE KEY UPDATE
      assigned_branch=VALUES(assigned_branch),
      assigned_reason=VALUES(assigned_reason),
      fp_index=VALUES(fp_index),
      fp_found=VALUES(fp_found),
      ambiguous=VALUES(ambiguous),
      computed_at=VALUES(computed_at)
    """
    params = [
        (
            int(r.repo_id), int(r.release_id), int(r.branch_id),
            str(r.branch_ref), str(r.tag_name), str(r.tag_commit_sha),
            str(r.assigned_branch), str(r.assigned_reason),
            (int(r.fp_index) if pd.notna(r.fp_index) else None),
            int(r.fp_found), int(r.ambiguous)
        )
        for _, r in df_assign.iterrows()
    ]
    if params:
        cur.executemany(sql, params)
        conn.commit()

def main_fp(conn, cur, ROOT_PATH):

    repo_df = fetch_repo_rows(conn)
    print(f"Total repos in slice: {len(repo_df)}")

    for _, repo_row in repo_df.iterrows():
        repo_id = int(repo_row['repo_id'])
        owner, repo = repo_row['full_name'].split('/', 1)
        repo_dir = os.path.join(ROOT_PATH, "repos", owner, repo)
        remote_url = f"https://github.com/{owner}/{repo}.git"

        print(f"\n=== Repo {repo_id}: {owner}/{repo} ===")
        print(f"[path] {repo_dir}")

        ensure_repo_cloned(repo_dir, remote_url)

        # Start from the branch shortlist built in the reachability step.
        reach_df = fetch_reachability(cur, repo_id)

        # Group reachable tags by candidate branch before the first-parent walk.
        by_branch = defaultdict(list)
        tag_rows = defaultdict(list)
        unique_tag_shas = set()

        for _, r in reach_df.iterrows():
            br = str(r['branch_ref'])
            tag_sha = str(r['tag_commit_sha'])
            by_branch[br].append(r)
            unique_tag_shas.add(tag_sha)
            tag_rows[tag_sha].append(
                (int(r['release_id']), str(r['tag_name']), int(r['branch_id']), br)
            )

        # This is the main release-line step from the paper: walk first-parent on each candidate branch.
        fp_index_by_branch = {}  # branch_ref -> {sha: index}
        coverage_by_branch = {}  # branch_ref -> set(found_tag_shas)
        for br, rows in by_branch.items():
            # Only tags shortlisted for this branch need to be checked on its first-parent path.
            targets = {str(r['tag_commit_sha']) for r in rows}
            print(f"[fp] {br}: targets={len(targets)}")
            try:
                _, fp_index, found = fp_walk_order(
                    repo_dir=repo_dir,
                    branch_ref=br,
                    targets=targets
                )
            except Exception as e:
                print(f"[fp][warn] {br} walk failed: {e}")
                fp_index, found = {}, set()

            fp_index_by_branch[br] = fp_index
            coverage_by_branch[br] = found
            print(f"[fp] {br}: found {len(found)} / {len(targets)}")

        # assign each tag to one release line based on where it appears on first-parent history.
        records = []
        for tag_sha in unique_tag_shas:
            candidate_entries = tag_rows[tag_sha]
            # A branch can own the tag only if the tag commit appears on its first-parent path
            owners = []
            for release_id, tag_name, branch_id, branch_ref in candidate_entries:
                if tag_sha in fp_index_by_branch.get(branch_ref, {}):
                    owners.append((release_id, tag_name, branch_id, branch_ref))

            ambiguous = 0
            assigned_branch = None
            assigned_reason = ""
            fp_idx = None
            fp_found = 0

            if owners:
                # If multiple branches contain the same tag on first-parent history,
                # resolve it with the branch preference policy.
                owners_sorted = sorted(owners, key=lambda x: pref_rank(x[3]))
                top_release_id, top_tag_name, top_branch_id, top_branch_ref = owners_sorted[0]
                if len(owners) > 1:
                    ambiguous = 1
                    assigned_reason = "policy_resolve_multi"
                else:
                    assigned_reason = "single_owner"

                assigned_branch = top_branch_ref
                fp_idx = fp_index_by_branch[top_branch_ref][tag_sha]
                fp_found = 1

                records.append(dict(
                    repo_id=repo_id,
                    release_id=top_release_id,
                    branch_id=top_branch_id,
                    branch_ref=top_branch_ref,
                    tag_name=top_tag_name,
                    tag_commit_sha=tag_sha,
                    assigned_branch=assigned_branch,
                    assigned_reason=assigned_reason,
                    fp_index=fp_idx,
                    fp_found=fp_found,
                    ambiguous=ambiguous
                ))
            else:
                # These are the incomplete or ambiguous cases where reachability alone was not enough
                for release_id, tag_name, branch_id, branch_ref in candidate_entries:
                    records.append(dict(
                        repo_id=repo_id,
                        release_id=release_id,
                        branch_id=branch_id,
                        branch_ref=branch_ref,
                        tag_name=tag_name,
                        tag_commit_sha=tag_sha,
                        assigned_branch=branch_ref,
                        assigned_reason="miss_on_fp",
                        fp_index=None,
                        fp_found=0,
                        ambiguous=0
                    ))

        if not records:
            print("[assign] Nothing to persist for this repo.")
            continue

        df_assign = pd.DataFrame.from_records(records)
        upsert_assignments(cur, conn, df_assign)

        # print the final release order inside each retained branch-based line.
        print("\n[summary]")
        per_branch = defaultdict(list)
        for _, r in df_assign[df_assign["fp_found"] == 1].iterrows():
            per_branch[r["assigned_branch"]].append((r["tag_name"], r["fp_index"]))
        for br, items in per_branch.items():
            items_sorted = sorted(items, key=lambda x: x[1])
            tags_sorted = [t for t, _ in items_sorted]
            print(f"  - {br}: {len(tags_sorted)} releases => {tags_sorted}")

def main_summary(conn, cur):
    """
    Convert branch assignments into the final release_lines table.
    """
    cur.execute("SELECT * FROM release_line_assignment")
    rows = cur.fetchall()
    df_assign = pd.DataFrame(rows)

    print("[load] release_line_assignment rows:", len(df_assign))

    df_found = df_assign[df_assign["fp_found"] == 1].copy()

    release_ids = df_found["release_id"].unique().tolist()

    sql = "SELECT * FROM releases WHERE id IN (%s)" % (
        ",".join(["%s"] * len(release_ids))
    )
    cur.execute(sql, tuple(release_ids))
    df_releases = pd.DataFrame(cur.fetchall())

    print("[load] releases:", len(df_releases))

    # For fast lookup by release_id
    release_map = df_releases.set_index("id")

    insert_sql = """
    INSERT INTO release_lines
    (repo_id, release_line_id, branch_id, branch_name,
    release_id, release_name, fp_index, published_date)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """

    # --------------------------------------------------------
    # PROCESS PER REPO
    # --------------------------------------------------------
    for repo_id, df_repo in df_found.groupby("repo_id"):
        print(f"\n=== Repo {repo_id} ===")
        # each assigned branch becomes one release line.
        branch_groups = {}
        for _, r in df_repo.iterrows():
            br = r["assigned_branch"]
            if br not in branch_groups:
                branch_groups[br] = []
            branch_groups[br].append((r["tag_name"], r["fp_index"], r["release_id"], r["branch_id"]))

        release_line_id = 1

        for br, items in branch_groups.items():
            print(f"  - Line {release_line_id} via {br}")

            # The first-parent position defines the order inside the release line.
            items_sorted = sorted(items, key=lambda x: x[1])

            for tag_name, fp_index, release_id, branch_id in items_sorted:
                # look up release metadata
                release_row = release_map.loc[release_id]
                published_date = release_row["published_at"]
                cur.execute(insert_sql, (
                    repo_id,
                    release_line_id,
                    branch_id,
                    br,   # branch name
                    release_id,
                    tag_name,   # release_name
                    fp_index,
                    published_date
                ))
                conn.commit()
            print(f"    inserted {len(items_sorted)} releases into release line {release_line_id}")

            release_line_id += 1

    print("\n[done] release_lines populated.")

# -------------- Main --------------------------
def main():
    db_config = DatabaseConfig()
    conn, cur = db_config.create_db_connection()

    ensure_output_table(cur)

    raw_cfg = RawFileConfig(db=db_config.db)
    ROOT_PATH = raw_cfg.root_path  # <path_to_raw_data>

    # First-parent traversal and assignment
    main_fp(conn, cur, ROOT_PATH)

    # Overall summary population
    main_summary(conn, cur)

    print("\nDone.")

if __name__ == "__main__":
    main()
