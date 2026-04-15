#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script to collect tags from GitHub repositories listed in the downstream_repo_info table.
Saves tag information in the tags table and stores raw JSON files in the specified directory.
"""

import os
import json
from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utilities.GHConfig import GitHubConfig
from utilities.RawFileConfig import RawFileConfig
from utilities import GHConfig as GHConfig

if __name__ == "__main__":
    # Initialize database and GitHub configurations
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # Initialize raw file configuration
    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    TAGS_DIR = raw_file_config.get_tag_folder()
    print(f"Tags files path: {TAGS_DIR}")

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.TAG_COLLECTION_STATUS} IN (0, -1)
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

        # create directory for the repo if it doesn't exist
        repo_path_name = str(repo_id) + "_" + repo_name.split("/")[-1]
        repo_dir = os.path.join(TAGS_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(repo_dir)

        # Prepare the API URL for listing tags
        api_url = GHConfig.LIST_TAGS_URL.format(repo_name)
        page = 1
        url, headers, params = gh_config.prepare_list_releases(api_url, page)
        response = gh_config.send_request(url, headers=headers, params=params)
        last_page = gh_config.extract_last_page(response)
        print(f"Total pages of tags: {last_page}")

        while page <= last_page:
            print(f"\n========Fetching tags from page {page}...")
            url, headers, params = gh_config.prepare_list_releases(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)

            if response.status_code == 200:
                tags = response.json()
                if not tags:
                    print(f"No tags found for {repo_name} on page {page}.")
                    break

                for tag in tags:
                    id = tag.get("id")
                    tag_name = tag.get("name")
                    node_id = tag.get("node_id")
                    commit = tag.get("commit", {})
                    commit_sha = commit.get("sha")

                    commit_id_db = db_config.select_from_db(
                        DBT.COMMITS.value,
                        columns=[DBF.Commits.ID],
                        where=f"{DBF.Commits.COMMIT_ID} = %s AND {DBF.Commits.REPO_ID} = %s",
                        params=(commit_sha, repo_id),
                        fetch_one=True
                    )
                    commit_id_db = commit_id_db[DBF.Commits.ID] if commit_id_db else None
                    
                    release_id_db = db_config.select_from_db(
                        DBT.RELEASES.value,
                        columns=[DBF.Releases.ID],
                        where=f"{DBF.Releases.TAG_NAME} = %s AND {DBF.Releases.REPO_ID} = %s",
                        params=(tag_name, repo_id),
                        fetch_one=True
                    )
                    release_id_db = release_id_db[DBF.Releases.ID] if release_id_db else None

                    data_to_insert = {
                        DBF.Tags.ID: id,
                        DBF.Tags.NAME: tag_name,
                        DBF.Tags.NODE_ID: node_id,
                        DBF.Tags.COMMIT_SHA: commit_sha,
                        DBF.Tags.COMMIT_ID: commit_id_db,
                        DBF.Tags.RELEASE_ID: release_id_db,
                        DBF.Tags.REPO_ID: repo_id,
                    }

                    db_config.insert_to_db(
                        DBT.TAGS.value,
                        data_dict=data_to_insert,
                    )
                    print(f"Inserted tag {tag_name} (ID: {id}) into the database.")

                    # write tag json to file
                    # get id of the inserted row
                    tag_id_db = db_config.select_from_db(
                        DBT.TAGS.value,
                        columns=[DBF.Tags.ID],
                        where=f"{DBF.Tags.NAME} = %s AND {DBF.Tags.REPO_ID} = %s",
                        params=(tag_name, repo_id),
                        fetch_one=True
                    )
                    if tag_id_db:
                        tag_id_db = tag_id_db[DBF.Tags.ID]
                        tag_file_path = os.path.join(repo_dir, f"{tag_id_db}.json")
                        with open(tag_file_path, "w", encoding="utf-8") as tf:
                            json.dump(tag, tf, indent=4)
                        print(f"Saved tag JSON to {tag_file_path}")
                    else:
                        print(f"Failed to retrieve DB ID for tag ID {tag_id_db}")
                        collected_status = -1

                print(f"Completed processing page {page} for tags of {repo_name}.")
            else:
                print(f"Failed to fetch tags for {repo_name} on page {page}. Status code: {response.status_code}")
                collected_status = -1
                break

            page += 1

        # update tag collection status in downstream_repo_info table
        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={DBF.DownstreamRepoInfo.TAG_COLLECTION_STATUS: collected_status},
            where=f"{DBF.DownstreamRepoInfo.ID} = %s",
            params=(repo_id,)
        )
        print(f"Updated tag collection status to {collected_status} for repository ID {repo_id}")

    print("\nAll done.")