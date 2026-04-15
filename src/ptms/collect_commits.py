"""
Collect commit metadata for PTM-mapped downstream repositories.
"""

import json
import os
import time

from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils import GHConfig as GHConfig
from utils.RawFileConfig import RawFileConfig
from utils.Utils import convert_datetime

def main():
    """Collect commit records and cache the raw GitHub commit payloads."""
    # Set up DB access and the local raw commit cache.
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    COMMITS_DIR = raw_file_config.get_commit_folder()
    print(f"Commit files path: {COMMITS_DIR}")

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    # Only collect commits for repos that passed the earlier PTM filtering steps.
    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.COMMIT_COLLECTION_STATUS} in (0, -1)
            AND EXISTS (
                SELECT 1
                FROM {DBT.FILES} rf
                JOIN {DBT.FILE_TO_MODEL} rfm ON rf.{DBF.Files.ID} = rfm.{DBF.FileToModel.FILE_ID}
                WHERE rf.{DBF.Files.REPO_ID} = {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
            )
        """,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC",
        # order_by=f"{DBF.DownstreamRepoInfo.ID} DESC",
        fetch_one=False
    )   

    print(f"Found {len(repos)} downstream repositories in the database.")

    for repo in repos:
        collected_status = 1
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        repo_name = repo[DBF.DownstreamRepoInfo.FULL_NAME]
        print(f"\n=========================Processing repository {repo_name} (ID: {repo_id})...")

        # Keep one cache folder per repo.
        repo_path_name = str(repo_id) + "_" + repo_name.split("/")[-1]
        repo_dir = os.path.join(COMMITS_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(repo_dir)

        # Walk through the repo commit list page by page.
        api_url = GHConfig.LIST_COMMITS_URL.format(repo_name)
        page = 1
        url, headers, params = gh_config.prepare_list_commits(api_url, page)
        response = gh_config.send_request(url, headers=headers, params=params)
        last_page = gh_config.extract_last_page(response)
        print(f"Total pages of commits: {last_page}")

        while page <= last_page:
            print(f"\n========Fetching commits from page {page}...")
            url, headers, params = gh_config.prepare_list_commits(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)

            if response.status_code == 200:
                commits = response.json()
                if not commits:
                    print(f"No commits found for {repo_name} on page {page}.")
                    break

                for commit in commits:
                    commit_id = commit.get("sha", "")
                    commit_url = GHConfig.COMMIT_INFO_URL.format(repo_name, commit_id)
                    headers = gh_config.prepare_info_request()
                    commit_response = gh_config.send_request(commit_url, headers=headers)

                    if commit_response.status_code == 200:
                        commit_data = commit_response.json()
                        commit_id = commit_data.get("sha", "")
                        commit_node_id = commit_data.get("node_id", "")
                        committed_at = convert_datetime(commit_data.get("commit", {}).get("committer", {}).get("date", ""))
                        message = commit_data.get("commit", {}).get("message", "")
                        git_url = commit_data.get("commit", {}).get("url", "")
                        tree_sha = commit_data.get("commit", {}).get("tree", {}).get("sha", "")
                        tree_url = commit_data.get("commit", {}).get("tree", {}).get("url", "")
                        comment_count = commit_data.get("commit", {}).get("comment_count", 0)
                        html_url = commit_data.get("html_url", "")
                        verified = commit_data.get("commit", {}).get("verification", {}).get("verified", False)
                        verified_reason = commit_data.get("commit", {}).get("verification", {}).get("reason", "")
                        verified_at = convert_datetime(commit_data.get("commit", {}).get("verification", {}).get("verified_at", ""))
                        author = commit_data.get("author") or {}
                        author_name = author.get("login", "")
                        author_id = author.get("id") if author.get("id") is not None else None
                        author_type = author.get("type", "")
                        committer = commit_data.get("committer") or {}
                        committer_name = committer.get("login", "")
                        committer_id = committer.get("id") if committer.get("id") is not None else None
                        committer_type = committer.get("type", "")
                        parents = commit_data.get("parents", [])
                        parent_count = len(parents)
                        total = commit_data.get("stats", {}).get("total", 0)
                        additions = commit_data.get("stats", {}).get("additions", 0)
                        deletions = commit_data.get("stats", {}).get("deletions", 0)
                        changed_file_count = commit_data.get("files", [])
                        changed_file_count = len(changed_file_count)

                        # Store the normalized commit row in the DB first.
                        commit_data_dict = {
                            DBF.Commits.COMMIT_ID: commit_id,
                            DBF.Commits.NODE_ID: commit_node_id,
                            DBF.Commits.COMMITTED_AT: committed_at,
                            DBF.Commits.MESSAGE: message,
                            DBF.Commits.GIT_URL: git_url,
                            DBF.Commits.TREE_SHA: tree_sha,
                            DBF.Commits.TREE_URL: tree_url,
                            DBF.Commits.COMMENT_COUNT: comment_count,
                            DBF.Commits.HTML_URL: html_url,
                            DBF.Commits.VERIFIED: verified,
                            DBF.Commits.VERIFIED_REASON: verified_reason,
                            DBF.Commits.VERIFIED_AT: verified_at,
                            DBF.Commits.AUTHOR_NAME: author_name,
                            DBF.Commits.AUTHOR_ID: author_id,
                            DBF.Commits.AUTHOR_TYPE: author_type,
                            DBF.Commits.COMMITTER_NAME: committer_name,
                            DBF.Commits.COMMITTER_ID: committer_id,
                            DBF.Commits.COMMITTER_TYPE: committer_type,
                            DBF.Commits.PARENT_COUNT: parent_count,
                            DBF.Commits.PARENTS: json.dumps(parents),
                            DBF.Commits.TOTAL: total,
                            DBF.Commits.ADDITIONS: additions,
                            DBF.Commits.DELETIONS: deletions,
                            DBF.Commits.CHANGED_FILE_COUNT: changed_file_count,
                            DBF.Commits.REPO_ID: repo_id
                        }

                        db_config.insert_to_db(
                            DBT.COMMITS.value,
                            data_dict=commit_data_dict
                        )
                        print(f"Inserted commit {commit_id} into database for repository {repo_name}.")
                    else:
                        print(f"Failed to fetch commit details for {commit_id} in {repo_name}: {commit_response.status_code}")
                        collected_status = -1
                        break

                    # Keep the raw response too, so later steps can reuse it.
                    commit_id_db = db_config.select_from_db(
                        DBT.COMMITS.value,
                        columns=[DBF.Commits.ID],
                        where=f"{DBF.Commits.COMMIT_ID} = %s AND {DBF.Commits.REPO_ID} = %s",
                        params=(commit_id, repo_id),
                        fetch_one=True
                    )
                    if commit_id_db:
                        commit_id_db = commit_id_db[DBF.Commits.ID]
                        commit_file_path = os.path.join(repo_dir, f"{commit_id_db}.json")
                        with open(commit_file_path, "w", encoding="utf-8") as f:
                            json.dump(commit_data, f, ensure_ascii=False, indent=4)
                        print(f"Commit data for {commit_id} written to {commit_file_path}.")
                    else:
                        print(f"Commit {commit_id} not found in database for repository {repo_name}.")
                        collected_status = -1
                        break

                print(f"Processed {len(commits)} commits from page {page}.")
            else:
                print(f"Failed to fetch commits for {repo_name} on page {page}: {response.status_code}")
                collected_status = -1
                break

            page += 1

        time.sleep(1)

        # Mark the repo-level collection result for reruns.
        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={DBF.DownstreamRepoInfo.COMMIT_COLLECTION_STATUS: collected_status},
            where=f"{DBF.DownstreamRepoInfo.ID} = %s",
            params=(repo_id,)
        )
        
        print(f"Updated commit collection status for {repo_name} (ID: {repo_id}) to {collected_status}.")
    print("All repositories processed successfully.")


if __name__ == "__main__":
    main()
