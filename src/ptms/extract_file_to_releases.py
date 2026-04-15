"""
Map reused files to the releases where each file version appears.

This script resolves each reused file at release tags, follows rename history,
and stores the matching file-release rows for later release-level snapshots.
"""

import concurrent.futures
import threading
from typing import Dict, List, Optional, Tuple, Set

from utilities.DBConfig import DatabaseConfig
from utilities.GHConfig import GitHubConfig
from utilities import GHConfig as GHConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF

_file_info_cache: Dict[Tuple[str, str, str], Optional[dict]] = {}
_tag_commit_cache_global: Dict[Tuple[str, str], Optional[dict]] = {}
_cache_lock = threading.Lock()

def select_repos(db: DatabaseConfig | None = None) -> List[dict]:
    """
    Select repos that are ready for release-level file mapping.
    """
    print("\nSelecting repos to process...")
    rows = db.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND EXISTS (
                SELECT 1
                FROM {DBT.FILES} rf
                JOIN {DBT.FILE_TO_MODEL} rfm
                    ON rf.{DBF.Files.ID} = rfm.{DBF.FileToModel.FILE_ID}
                WHERE rf.{DBF.Files.REPO_ID} =
                        {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
            )
        """,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC",
        fetch_one=False
    ) or []

    print(f"Selected {len(rows)} repos to process.\n")
    return rows


def select_releases(db: DatabaseConfig, repo_id: int) -> List[Dict]:
    """
    Load all stored releases for one repo.
    """
    releases = db.select_from_db(
        DBT.RELEASES.value,
        columns="*",
        where=f"{DBF.Releases.REPO_ID} = %s",
        params=(repo_id,),
        order_by=f"{DBF.Releases.PUBLISHED_AT} DESC"
    ) or []
    print(f"Total releases: {len(releases)}")
    return releases


def select_files_for_repo(db: DatabaseConfig, repo_id: int, run_id: int | None = None) -> List[dict]:
    """
    Load reused files that already have PTM mappings for one repo.
    """
    print(f"\nSelecting files for repo {repo_id}...")
    files: List[dict] = db.select_from_db(
        DBT.FILES.value,
        columns="*",
        where=f"""
            {DBF.Files.REPO_ID} = {repo_id}
            AND EXISTS (
                SELECT 1
                FROM {DBT.FILE_TO_MODEL} rfm
                WHERE rfm.{DBF.FileToModel.FILE_ID} = {DBT.FILES}.{DBF.Files.ID}
            )
        """,
        order_by=f"{DBF.Files.ID} ASC",
        fetch_one=False
    ) or []

    print(f"Selected {len(files)} files for repo {repo_id}.\n")
    return files

def normalize_releases(
    releases: List[dict],
    *,
    include_prereleases: bool = False
) -> List[dict]:
    """
    Filter out drafts and (optionally) prereleases.
    Sort newest -> oldest by published_at (fallback created_at).
    """
    def _keep(rel: dict) -> bool:
        if rel.get("draft"):
            return False
        if not include_prereleases and rel.get("prerelease"):
            return False
        return bool(rel.get("tag_name"))

    keep = [r for r in releases if _keep(r)]
    keep.sort(key=lambda r: (r.get("published_at") or r.get("created_at") or ""), reverse=True)
    return keep

def prefetch_rename_graph(db: DatabaseConfig, repo_id: int) -> Dict[str, Set[str]]:
    """
    Load the rename graph once for one repo.
    """
    print(f"Prefetching rename graph for repo {repo_id} ...")
    rows = db.select_from_db(
        table_name=(
            f"{DBT.COMMIT_FILES.value} cf "
            f"JOIN {DBT.COMMITS.value} c ON c.id = cf.{DBF.CommitFiles.COMMIT_ID} "
            f"JOIN commit_previous_filenames cpf ON cpf.commit_file_id = cf.id"
        ),
        columns=(
            f"cf.{DBF.CommitFiles.FILE_NAME} AS new_name, "
            f"cpf.previous_filename AS old_name"
        ),
        where=(
            f"c.{DBF.Commits.REPO_ID} = %s "
            f"AND cpf.previous_filename IS NOT NULL "
            f"AND cpf.previous_filename <> ''"
        ),
        params=(repo_id,),
        fetch_one=False
    ) or []

    graph: Dict[str, Set[str]] = {}
    for r in rows:
        new_name = r.get("new_name")
        old_name = r.get("old_name")
        if not new_name or not old_name:
            continue
        s = graph.setdefault(new_name, set())
        s.add(old_name)

    print(f"Rename edges loaded: {sum(len(v) for v in graph.values())}")
    return graph


def collect_all_names_from_graph(current_path: str, rename_graph: Dict[str, Set[str]]) -> List[str]:
    """
    Walk backward through rename history starting from the current path.
    """
    names: Set[str] = {current_path}
    stack: List[str] = [current_path]

    while stack:
        cur = stack.pop()
        for old_name in rename_graph.get(cur, ()):
            if old_name not in names:
                names.add(old_name)
                stack.append(old_name)

    others = sorted(n for n in names if n != current_path)
    return [current_path] + others


def fetch_all_filenames_for_file(
    file_id: int,
    file_path: str,
    repo_id: int,
    rename_graph: Dict[str, Set[str]]
) -> List[str]:
    """
    Return all historical names for one file.
    """
    print(f"(file) repo {repo_id} file {file_id}: collecting names for path '{file_path}'")
    all_names = collect_all_names_from_graph(file_path, rename_graph)
    print(f"(file) repo {repo_id} file {file_id}: total names found={len(all_names)}")
    return all_names

def file_info_at_ref(gh_config: GitHubConfig, repo_name: str, path: str, ref: str) -> Optional[dict]:
    """
    Query the Contents API for one path at one ref, with a small positive cache.
    """
    key = (repo_name, path, ref)
    with _cache_lock:
        v = _file_info_cache.get(key)
        if v:
            return v

    file_url = GHConfig.FILE_INFO_URL.format(repo_name, path)
    file_url, headers, params = gh_config.prepare_file_info(file_url, ref)

    import time
    for i in range(4):
        r = gh_config.send_request(file_url, headers, params)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** i)
            continue
        if r.status_code == 404:
            info = None
        else:
            r.raise_for_status()
            info = r.json()
        break

    if info and info.get("type") == "file":
        with _cache_lock:
            _file_info_cache[key] = info
    return info



def tag_commit_info(gh_config: GitHubConfig, repo_name: str, ref: str) -> Optional[dict]:
    """
    Resolve one tag ref to commit info and cache it for this run.
    """
    key = (repo_name, ref)
    with _cache_lock:
        if key in _tag_commit_cache_global:
            return _tag_commit_cache_global[key]

    commit_url = GHConfig.COMMIT_INFO_URL.format(repo_name, ref)
    headers = gh_config.prepare_info_request()
    r = gh_config.send_request(commit_url, headers)
    if r.status_code == 404:
        ci = None
    else:
        r.raise_for_status()
        j = r.json()
        date = ((j.get("commit") or {}).get("committer") or {}).get("date")
        ci = {"sha": j.get("sha"), "date": date}

    with _cache_lock:
        _tag_commit_cache_global[key] = ci
    return ci


def _tree_lookup_by_sha(gh: GitHubConfig, repo: str, commit_sha: str, wanted_names: List[str]) -> Optional[dict]:
    """
    Use the Trees API to find a matching path at one commit.
    """
    headers = gh.prepare_info_request()

    tree_url = f"https://api.github.com/repos/{repo}/git/trees/{commit_sha}?recursive=1"

    import time
    for i in range(4):
        r = gh.send_request(tree_url, headers)
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(2 ** i)
            continue
        if r.status_code == 404:
            return None
        r.raise_for_status()
        j = r.json()
        break

    items = {e["path"]: e for e in j.get("tree", []) if e.get("type") == "blob"}

    for name in wanted_names:
        e = items.get(name)
        if e:
            blob_sha = e.get("sha")
            size = e.get("size") or 0
            download_url = f"https://raw.githubusercontent.com/{repo}/{commit_sha}/{name}"
            html_url = f"https://github.com/{repo}/blob/{commit_sha}/{name}"
            return {"path": name, "sha": blob_sha, "size": size, "download_url": download_url, "html_url": html_url}
    return None


def resolve_path_and_blob_at_tag(
    gh_config: GitHubConfig,
    repo_name: str,
    tag: str,
    candidate_names: List[str]
) -> Optional[dict]:
    """
    Resolve the file path and blob metadata for a tag, trying known historical names.
    """
    # First try Contents API by tag name
    for name in candidate_names:
        info = file_info_at_ref(gh_config, repo_name, name, tag)
        if info and info.get("type") == "file":
            return {
                "path": name,
                "sha": info.get("sha"),
                "size": info.get("size"),
                "download_url": info.get("download_url"),
                "html_url": info.get("html_url"),
            }

    # Fall back to a tree lookup at the tag commit if Contents API misses.
    ci = tag_commit_info(gh_config, repo_name, tag)
    if not ci or not ci.get("sha"):
        res = _resolve_tag_to_commit_sha(gh_config, repo_name, tag)
        if not res:
            return None
        commit_sha, _ = res
    else:
        commit_sha = ci["sha"]

    return _tree_lookup_by_sha(gh_config, repo_name, commit_sha, candidate_names)

def get_commit_row_by_sha(db: DatabaseConfig, repo_id: int, sha: str) -> Optional[dict]:
    """
    Load one stored commit row by SHA.
    """
    return db.select_from_db(
        DBT.COMMITS.value,
        columns=f"{DBF.Commits.ID} AS id, {DBF.Commits.COMMITTED_AT} AS committed_at",
        where=f"{DBF.Commits.REPO_ID} = %s AND {DBF.Commits.COMMIT_ID} = %s",
        params=(repo_id, sha),
        fetch_one=True
    )


def find_last_touch_before(
    db: DatabaseConfig,
    repo_id: int,
    names: List[str],
    cutoff_iso: str
) -> Optional[dict]:
    """
    Find the last stored commit that touched any historical name before a cutoff.
    """
    if not names:
        return None
    placeholders = ", ".join(["%s"] * len(names))
    params = (repo_id, *names, cutoff_iso)

    row = db.select_from_db(
        table_name=(
            f"{DBT.COMMIT_FILES.value} cf "
            f"JOIN {DBT.COMMITS.value} c ON c.id = cf.{DBF.CommitFiles.COMMIT_ID}"
        ),
        columns=(
            f"c.{DBF.Commits.ID} AS id, "
            f"c.{DBF.Commits.COMMIT_ID} AS commit_sha, "
            f"cf.{DBF.CommitFiles.ID} AS commit_file_id, "
            f"c.{DBF.Commits.COMMITTED_AT} AS committed_at"
        ),
        where=(
            f"c.{DBF.Commits.REPO_ID} = %s "
            f"AND cf.{DBF.CommitFiles.FILE_NAME} IN ({placeholders}) "
            f"AND cf.{DBF.CommitFiles.STATUS} IN ('added','modified','renamed','removed') "
            f"AND c.{DBF.Commits.COMMITTED_AT} <= %s"
        ),
        params=params,
        order_by=f"c.{DBF.Commits.COMMITTED_AT} DESC, c.{DBF.Commits.ID} DESC",
        fetch_one=True
    )
    return row


def prefetch_commit_rows_for_tags(db: DatabaseConfig, repo_id: int, tag_shas: List[str]) -> Dict[str, dict]:
    """
    Bulk fetch commit rows for tag SHAs so later lookups stay cheap.
    """
    if not tag_shas:
        return {}
    uniq = list({s for s in tag_shas if s})
    placeholders = ", ".join(["%s"] * len(uniq))
    params = (repo_id, *uniq)
    rows = db.select_from_db(
        DBT.COMMITS.value,
        columns=f"{DBF.Commits.COMMIT_ID} AS commit_sha, {DBF.Commits.ID} AS id, {DBF.Commits.COMMITTED_AT} AS committed_at",
        where=f"{DBF.Commits.REPO_ID} = %s AND {DBF.Commits.COMMIT_ID} IN ({placeholders})",
        params=params,
        fetch_one=False
    ) or []
    return {r["commit_sha"]: r for r in rows}

def _resolve_tag_to_commit_sha(gh: GitHubConfig, repo: str, tag: str) -> Optional[tuple[str, str]]:
    """
    Resolve a tag to its commit SHA and commit date.
    """
    headers = gh.prepare_info_request()

    def _req(url):
        import time
        for i in range(4):
            r = gh.send_request(url, headers)
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(2 ** i)
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        return None

    # Try the refs API first so annotated tags get dereferenced correctly.
    ref_url = f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}"
    ref = _req(ref_url)
    if ref and isinstance(ref, dict) and ref.get("object"):
        obj = ref["object"]
        if obj.get("type") == "tag" and obj.get("url"):
            tagobj = _req(obj["url"])
            if tagobj and tagobj.get("object") and tagobj["object"].get("sha"):
                commit_sha = tagobj["object"]["sha"]
            else:
                commit_sha = obj.get("sha")
        else:
            commit_sha = obj.get("sha")

        if commit_sha:
            commit = _req(f"https://api.github.com/repos/{repo}/commits/{commit_sha}")
            date_iso = ((commit or {}).get("commit") or {}).get("committer", {}).get("date")
            return commit_sha, date_iso

    commit_url = GHConfig.COMMIT_INFO_URL.format(repo, tag)   # .../repos/{repo}/commits/{ref}
    commit = _req(commit_url)
    if commit and commit.get("sha"):
        date_iso = ((commit.get("commit") or {}).get("committer") or {}).get("date")
        return commit["sha"], date_iso

    return None

if __name__ == "__main__":
    # Run the release-level file mapping pipeline.
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = select_repos(db_config)

    for repo in repos:
        repo_id = int(repo["id"])
        repo_full_name = repo.get("full_name")

        print(f"\n=============== Processing repository: {repo_full_name} (ID: {repo_id})")

        # Load releases first so we can reuse the tag metadata below.
        releases_raw = select_releases(db_config, repo_id=repo_id)

        releases = normalize_releases(
            releases_raw,
            include_prereleases=False,
        )

        if releases:

            # Resolve release tags to commits once, then reuse that cache for all files.
            tag_names = [rel.get("tag_name") for rel in releases if rel.get("tag_name")]
            def _resolve_tag(t: str) -> Tuple[str, Optional[dict]]:
                out = _resolve_tag_to_commit_sha(gh_config, repo_full_name, t)
                if not out:
                    return t, None
                sha, date_iso = out
                return t, {"sha": sha, "date": date_iso}

            tag_commit_cache: Dict[str, Dict[str, str]] = {}
            if tag_names:
                with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                    futs = {ex.submit(_resolve_tag, t): t for t in tag_names}
                    for f in concurrent.futures.as_completed(futs):
                        t = futs[f]
                        try:
                            _, ci = f.result()
                        except Exception:
                            ci = None
                        if ci:
                            tag_commit_cache[t] = ci

            # Skip releases whose tags cannot be resolved reliably.
            for rel in releases:
                t = rel.get("tag_name")
                if not t or t not in tag_commit_cache:
                    print(f"  ! SKIP release {rel.get('id')} tag={t}: cannot resolve tag→commit (API miss or rate-limit).")
                    rel["_skip"] = True
                else:
                    rel["_skip"] = False

            tag_shas = [ci["sha"] for ci in tag_commit_cache.values() if ci and ci.get("sha")]
            sha_to_row = prefetch_commit_rows_for_tags(db_config, repo_id, tag_shas)

            # Load files and rename history once per repo.
            files = select_files_for_repo(db_config, repo_id=repo_id)
            rename_graph = prefetch_rename_graph(db_config, repo_id=repo_id)

            for file in files:
                file_id = int(file["id"])
                file_path = file["path"]
                all_names = fetch_all_filenames_for_file(
                    file_id=file_id,
                    file_path=file_path,
                    repo_id=repo_id,
                    rename_graph=rename_graph
                )
                print(f"File ID {file_id} has names: {all_names}")

                # Check the file against each retained release tag.
                for rel in releases:
                    if rel.get("_skip"):
                        continue

                    tag_name = rel.get("tag_name")
                    release_id = rel.get("id")

                    print(f"Checking release ID {release_id} tag {tag_name} for file {file_id}")

                    # Try all known historical paths at this tag until one matches.
                    fi = resolve_path_and_blob_at_tag(gh_config, repo_full_name, tag_name, all_names)
                    if not fi:
                        names_preview = all_names[:3] if all_names else []
                        print(
                            f"    - MISS: contents+tree lookup failed "
                            f"(repo={repo_full_name}, tag={tag_name}, file_id={file_id}, names={names_preview}...)"
                        )
                        continue

                    blob_sha = fi["sha"]
                    blob_size = int(fi.get("size") or 0)
                    path_at_tag = fi["path"]
                    download_url = fi.get("download_url") or ""

                    tag_ci = tag_commit_cache[tag_name]
                    tag_sha = tag_ci["sha"]
                    tag_date_iso = tag_ci["date"]

                    # Prefer the stored commit row for the exact cutoff time if we have it.
                    tag_commit_row = sha_to_row.get(tag_sha)
                    is_exact_commit = False
                    if not tag_commit_row:
                        print(f"    ! Tag commit {tag_sha} not found in DB; using API date cutoff.")
                        cutoff_iso = tag_date_iso
                        is_exact_commit = False
                    else:
                        cutoff_iso = str(tag_commit_row["committed_at"])
                        is_exact_commit = True

                    # Link the release file back to the latest known touch before the tag.
                    touch = find_last_touch_before(db_config, repo_id, all_names, cutoff_iso)
                    commit_id_int = touch["id"] if touch else None
                    commit_sha = touch["commit_sha"] if touch else None
                    commit_file_id = touch["commit_file_id"] if touch else None

                    data_to_insert = {
                        "repo_id": repo_id,
                        "file_id": file_id,
                        "file_sha": blob_sha,
                        "path": path_at_tag,
                        "size": blob_size,
                        "release_id": int(release_id),
                        "download_url": download_url,
                        "commit_id": commit_id_int,
                        "commit_sha": commit_sha,
                        "commit_file_id": commit_file_id,
                        "is_exact_commit": 1 if is_exact_commit else 0,
                    }

                    db_config.insert_to_db(
                        DBT.REUSED_FILES_TO_RELEASES.value,
                        data_dict=data_to_insert
                    )
                    print(f"    + Inserted: file_sha={blob_sha[:12]}..., path={path_at_tag}, commit={commit_sha}, release_pk={release_id}")
                print("")

        print(f"=============== Finished repository: {repo_full_name} (ID: {repo_id})\n")

    db_config.close_db_connection()
    print("Done.")
