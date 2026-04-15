"""
This script collects branch information for downstream repositories that have passed preliminary filtering and static analysis, but have not yet had their branches collected
"""
import os
import json
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils.RawFileConfig import RawFileConfig
from utils import GHConfig as GHConfig

def _ensure_branch_root_folder(raw_file_config: RawFileConfig) -> str:
    # If your RawFileConfig already has get_branch_folder(), use it; else fall back to "<root>/branches"
    if hasattr(raw_file_config, "get_branch_folder"):
        branch_dir = raw_file_config.get_branch_folder()
    else:
        branch_dir = os.path.join(raw_file_config.root_path, "branches")
        raw_file_config.create_folder_if_not_exists(branch_dir)
    return branch_dir

def _per_repo_folder(raw_file_config: RawFileConfig, base_dir: str, repo_id: int, repo_full_name: str) -> str:
    repo_path_name = f"{repo_id}_{repo_full_name.split('/')[-1]}"
    repo_dir = os.path.join(base_dir, repo_path_name)
    raw_file_config.create_folder_if_not_exists(repo_dir)
    return repo_dir

if __name__ == "__main__":
    # Initialize database and GitHub configurations
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    BRANCH_DIR = _ensure_branch_root_folder(raw_file_config)
    print(f"Branch files path: {BRANCH_DIR}")

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.BRANCH_COLLECTION_STATUS} in (0, -1)
            AND EXISTS (
                SELECT 1
                FROM {DBT.FILES} rf
                JOIN {DBT.FILE_TO_MODEL} rfm ON rf.{DBF.Files.ID} = rfm.{DBF.FileToModel.FILE_ID}
                WHERE rf.{DBF.Files.REPO_ID} = {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
            )
        """,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC",
        fetch_one=False
    )

    print(f"Found {len(repos)} downstream repositories in the database.")

    for repo in repos:
        collected_status = 1
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        repo_name = repo[DBF.DownstreamRepoInfo.FULL_NAME]
        print(f"\n========================= Processing repository {repo_name} (ID: {repo_id})...")

        repo_dir = _per_repo_folder(raw_file_config, BRANCH_DIR, repo_id, repo_name)

        # --- Prepare GitHub API for branches ---
        # Expect GHConfig.LIST_BRANCHES_URL = "https://api.github.com/repos/{}/branches"
        api_url = getattr(GHConfig, "LIST_BRANCHES_URL", f"https://api.github.com/repos/{repo_name}/branches")
        api_url = api_url.format(repo_name) if "{}" in api_url else api_url

        page = 1
        url, headers, params = gh_config.prepare_list_branches(api_url, page)

        response = gh_config.send_request(url, headers=headers, params=params)
        last_page = gh_config.extract_last_page(response)
        print(f"Total pages of branches: {last_page}")

        while page <= last_page:
            print(f"\n======== Fetching branches from page {page}...")
            url, headers, params = gh_config.prepare_list_branches(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)

            if response.status_code == 200:
                branches = response.json()
                if not branches:
                    print(f"No branches found for {repo_name} on page {page}.")
                    break

                for br in branches:
                    # GitHub branches payload (core fields)
                    # {
                    #   "name": "main",
                    #   "protected": false,
                    #   "commit": {"sha": "...", "url": "..."},
                    #   ...
                    # }
                    name = br.get("name")
                    protected = br.get("protected")
                    commit_obj = br.get("commit") or {}
                    commit_sha = commit_obj.get("sha")
                    commit_url = commit_obj.get("url")

                    # try to get commit_id from commits table
                    commit_row = db_config.select_from_db(
                        DBT.COMMITS.value,
                        columns=[DBF.Commits.ID],
                        where=f"{DBF.Commits.COMMIT_ID} = %s AND {DBF.Commits.REPO_ID} = %s",
                        params=(commit_sha, repo_id),
                        fetch_one=True
                    )
                    commit_id = commit_row[DBF.Commits.ID] if commit_row else None

                    # Insert into DB
                    data_to_insert = {
                        DBF.Branches.NAME: name,
                        DBF.Branches.PROTECTED: protected,
                        DBF.Branches.COMMIT_ID: commit_id,
                        DBF.Branches.COMMIT_SHA: commit_sha,
                        DBF.Branches.COMMIT_URL: commit_url,
                        DBF.Branches.REPO_ID: repo_id
                    }

                    try:
                        db_config.insert_to_db(DBT.BRANCHES.value, data_dict=data_to_insert)
                        print(f"Inserted branch '{name}' ({commit_sha})")
                    except Exception as e:
                        # Handle duplicates or transient failures without stopping the entire run
                        print(f"[warn] Insert failed for branch '{name}' in repo {repo_name}: {e}")
                        collected_status = -1

                    # Retrieve DB row id to save raw JSON (unique by (repo_id, name))
                    branch_row = db_config.select_from_db(
                        DBT.BRANCHES.value,
                        columns=[DBF.Branches.ID],
                        where=f"{DBF.Branches.REPO_ID} = %s AND {DBF.Branches.NAME} = %s",
                        params=(repo_id, name),
                        fetch_one=True
                    )
                    if branch_row:
                        branch_id_db = branch_row[DBF.Branches.ID]
                        branch_file_path = os.path.join(repo_dir, f"{branch_id_db}.json")
                        with open(branch_file_path, "w", encoding="utf-8") as bf:
                            json.dump(br, bf, indent=4)
                        print(f"Saved branch JSON to {branch_file_path}")
                    else:
                        print(f"[warn] Could not fetch DB id for branch '{name}' (repo_id={repo_id}).")
                        collected_status = -1

                print(f"Completed processing page {page} for branches of {repo_name}.")
            else:
                print(f"[error] Failed to fetch branches for {repo_name} on page {page}. "
                      f"Status code: {response.status_code}")
                collected_status = -1
                break

            page += 1

        # Update collection status on the repo record
        try:
            db_config.update_db(
                DBT.DOWNSTREAM_REPO_INFO.value,
                data_dict={DBF.DownstreamRepoInfo.BRANCH_COLLECTION_STATUS: collected_status},
                where=f"{DBF.DownstreamRepoInfo.ID} = %s",
                params=(repo_id,)
            )
            print(f"Updated branch collection status to {collected_status} for repository ID {repo_id}")
        except Exception as e:
            print(f"[warn] Failed to update BRANCH_COLLECTION_STATUS for repo_id={repo_id}: {e}")

    print("\nAll done.")
