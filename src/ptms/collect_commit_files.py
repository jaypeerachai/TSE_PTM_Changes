"""
Collect changed-file records for previously cached commits. This step expands commit metadata into per-file rows.
"""

import json
import os

from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils import GHConfig as GHConfig
from utils.RawFileConfig import RawFileConfig


def main():
    """
    collect commit-file rows for commits that still need file extraction
    """
    # Set up DB access, GitHub access, and the raw commit cache location.
    db_config = DatabaseConfig()
    db_config.create_db_connection()
    gh_config = GitHubConfig()
    gh_config.init_github_access()

    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    COMMITS_DIR = raw_file_config.get_commit_folder()
    print(f"Commit files path: {COMMITS_DIR}")

    # Only process repos that already have pending commit-file work.
    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND EXISTS (  -- must have reused files mapped to models
                SELECT 1
                FROM {DBT.FILES} rf
                JOIN {DBT.FILE_TO_MODEL} rfm
                ON rf.{DBF.Files.ID} = rfm.{DBF.FileToModel.FILE_ID}
                WHERE rf.{DBF.Files.REPO_ID} = {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
            )
            AND EXISTS (  -- must have at least one pending/failed commit to process
                SELECT 1
                FROM {DBT.COMMITS} c
                WHERE c.{DBF.Commits.REPO_ID} = {DBT.DOWNSTREAM_REPO_INFO}.{DBF.DownstreamRepoInfo.ID}
                AND c.{DBF.Commits.CHANGED_FILE_EXTRACTION_STATUS} IN (0, -1)
            )
        """,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC",
        # limit=30,
        # offset=90,
        fetch_one=False
    )
    print(f"Total repositories to process: {len(repos)}")

    for repo in repos:
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        repo_name = repo[DBF.DownstreamRepoInfo.FULL_NAME]
        print(f"\n===============Processing repository: {repo_name} (ID: {repo_id})")
        commits = db_config.select_from_db(
            DBT.COMMITS.value,
            columns="*",
            where=f"""
                {DBF.Commits.REPO_ID} = {repo_id}
                AND {DBF.Commits.CHANGED_FILE_EXTRACTION_STATUS} = 0
            """,
            order_by=f"{DBF.Commits.ID} ASC",
            fetch_one=False
        )
        repo_path_name = str(repo_id) + "_" + repo_name.split("/")[-1]
        repo_dir = os.path.join(COMMITS_DIR, repo_path_name)

        for commit in commits:
            commit_id = commit[DBF.Commits.ID]
            commit_sha = commit[DBF.Commits.COMMIT_ID]
            commit_file_path = os.path.join(repo_dir, f"{commit_id}.json")
            with open(commit_file_path, "r", encoding="utf-8") as f:
                commit_data = json.load(f)
            print(f"Processing commit: {commit_data['sha']} in repository: {repo_name}")

            # Page 1 is already in the cached commit JSON; extra pages come from the API.
            api_url = GHConfig.COMMIT_INFO_URL.format(repo_name, commit_sha)
            page = 1
            url, headers, params = gh_config.prepare_list_commit_files(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)
            last_page = gh_config.extract_last_page(response)
            print(f"Total pages of commit files: {last_page}")

            if last_page == 1:
                db_config.update_db(
                    DBT.COMMITS.value,
                    data_dict={DBF.Commits.CHANGED_FILE_EXTRACTION_STATUS: 1},
                    where=f"{DBF.Commits.ID} = %s",
                    params=(commit_id,)
                )
                print(f"Updated changed file extraction status for commit {commit_data['sha']}.")
                continue

            while page <= last_page:
                print(f"Fetching commit files from page {page}...")
                if page == 1:
                    commit_files = commit_data.get("files", [])
                    page += 1
                    continue
                else:
                    url, headers, params = gh_config.prepare_list_commit_files(api_url, page)
                    response = gh_config.send_request(url, headers=headers, params=params)
                    commit_files = response.json().get("files", [])
                for file in commit_files:
                    file_sha = file.get("sha")
                    filename = file.get("filename")
                    # This pipeline only tracks Python files.
                    if not filename.endswith(".py"):
                        continue
                    status = file.get("status")
                    additions = file.get("additions", 0)
                    deletions = file.get("deletions", 0)
                    changed_file_count = file.get("changes", 0)
                    blob_url = file.get("blob_url")
                    raw_url = file.get("raw_url")
                    contents_url = file.get("contents_url")
                    patch = file.get("patch", "")

                    commit_file_data = {
                        DBF.CommitFiles.COMMIT_ID: commit_id,
                        DBF.CommitFiles.FILE_SHA: file_sha,
                        DBF.CommitFiles.FILE_NAME: filename,
                        DBF.CommitFiles.STATUS: status,
                        DBF.CommitFiles.ADDITIONS: additions,
                        DBF.CommitFiles.DELETION: deletions,
                        DBF.CommitFiles.CHANGES: changed_file_count,
                        DBF.CommitFiles.BLOB_URL: blob_url,
                        DBF.CommitFiles.RAW_URL: raw_url,
                        DBF.CommitFiles.CONTENTS_URL: contents_url,
                        DBF.CommitFiles.PATCH: patch
                    }
                    db_config.insert_to_db(
                        DBT.COMMIT_FILES.value,
                        data_dict=commit_file_data,
                    )
                    print(f"Inserted commit file data for {filename} in commit {commit_data['sha']}.")

                    if status == "renamed":
                        previous_filename = file.get("previous_filename", "")
                        if previous_filename:
                            commit_file_id = db_config.cursor.lastrowid
                            db_config.insert_to_db(
                                DBT.COMMIT_PREVIOUS_FILENAMES.value,
                                data_dict={
                                    DBF.CommitPreviousFilenames.COMMIT_FILE_ID: commit_file_id,
                                    DBF.CommitPreviousFilenames.PREVIOUS_FILENAME: previous_filename
                                }
                            )
                            print(f"Inserted previous filename '{previous_filename}' for renamed file '{filename}' in commit {commit_data['sha']}.")

                page += 1

            # Mark this commit as finished once all pages are handled.
            db_config.update_db(
                DBT.COMMITS.value,
                data_dict={DBF.Commits.CHANGED_FILE_EXTRACTION_STATUS: 1},
                where=f"{DBF.Commits.ID} = %s",
                params=(commit_id,)
            )
            print(f"Updated changed file extraction status for commit {commit_data['sha']}.")

        print(f"Finished processing repository: {repo_name} (ID: {repo_id})")

    print("All repositories processed successfully.")


if __name__ == "__main__":
    main()
