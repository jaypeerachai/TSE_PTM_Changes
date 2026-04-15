"""
Collect release information for downstream repositories that have passed preliminary filtering and static analysis, and save the data to the database and raw files.
"""
import os
import json
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils.RawFileConfig import RawFileConfig
from utils import GHConfig as GHConfig
from utils.Utils import convert_datetime

if __name__ == "__main__":
    # Initialize database and GitHub configurations
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # Initialize raw file configuration
    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    RELEASE_DIR = raw_file_config.get_release_folder()
    print(f"Release files path: {RELEASE_DIR}")

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.RELEASE_COLLECTION_STATUS} in (0, -1)
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
        repo_dir = os.path.join(RELEASE_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(repo_dir)

        # Prepare the API URL for release listing
        api_url = GHConfig.LIST_RELEASES_URL.format(repo_name)
        page = 1
        url, headers, params = gh_config.prepare_list_releases(api_url, page)
        response = gh_config.send_request(url, headers=headers, params=params)
        last_page = gh_config.extract_last_page(response)
        print(f"Total pages of releases: {last_page}")

        while page <= last_page:
            print(f"\n========Fetching releases from page {page}...")
            url, headers, params = gh_config.prepare_list_releases(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)

            if response.status_code == 200:
                releases = response.json()
                if not releases:
                    print(f"No releases found for {repo_name} on page {page}.")
                    break

                for release in releases:
                    release_id = release.get("id")
                    tag_name = release.get("tag_name")
                    target_commitish = release.get("target_commitish")
                    name = release.get("name")
                    url = release.get("url")
                    html_url = release.get("html_url")
                    draft = release.get("draft")
                    immutable = release.get("immutable")
                    prerelease = release.get("prerelease")
                    body = release.get("body")
                    created_at = convert_datetime(release.get("created_at"))
                    updated_at = convert_datetime(release.get("updated_at"))
                    published_at = convert_datetime(release.get("published_at"))
                    node_id = release.get("node_id")
                    author = release.get("author") or {}
                    author_id = author.get("id")
                    author_name = author.get("login")
                    mentions_count = release.get("mentions_count")
                    tarball_url = release.get("tarball_url")
                    zipball_url = release.get("zipball_url")
                    assets = json.dumps(release.get("assets") or [])

                    data_to_insert = {
                        DBF.Releases.RELEASE_ID: release_id,
                        DBF.Releases.TAG_NAME: tag_name,
                        DBF.Releases.TARGET_COMMITISH: target_commitish,
                        DBF.Releases.NAME: name,
                        DBF.Releases.URL: url,
                        DBF.Releases.HTML_URL: html_url,
                        DBF.Releases.DRAFT: draft,
                        DBF.Releases.IMMUTABLE: immutable,
                        DBF.Releases.PRERELEASE: prerelease,
                        DBF.Releases.BODY: body,
                        DBF.Releases.CREATED_AT: created_at,
                        DBF.Releases.UPDATED_AT: updated_at,
                        DBF.Releases.PUBLISHED_AT: published_at,
                        DBF.Releases.NODE_ID: node_id,
                        DBF.Releases.AUTHOR_ID: author_id,
                        DBF.Releases.AUTHOR_NAME: author_name,
                        DBF.Releases.MENTIONS_COUNT: mentions_count,
                        DBF.Releases.TARBALL_URL: tarball_url,
                        DBF.Releases.ZIPBALL_URL: zipball_url,
                        DBF.Releases.ASSETS: assets,
                        DBF.Releases.REPO_ID: repo_id
                    }

                    db_config.insert_to_db(
                        DBT.RELEASES.value,
                        data_dict=data_to_insert,
                    )
                    print(f"Inserted release ID {release_id} - Tag: {tag_name}")

                    # write release JSON to file
                    # get id of the inserted row
                    release_id_db = db_config.select_from_db(
                        DBT.RELEASES.value,
                        columns=[DBF.Releases.ID],
                        where=f"{DBF.Releases.RELEASE_ID} = %s AND {DBF.Releases.REPO_ID} = %s",
                        params=(release_id, repo_id),
                        fetch_one=True
                    )
                    if release_id_db:
                        release_id_db = release_id_db[DBF.Releases.ID]
                        release_file_path = os.path.join(repo_dir, f"{release_id_db}.json")
                        with open(release_file_path, "w", encoding="utf-8") as rf:
                            json.dump(release, rf, indent=4)
                        print(f"Saved release JSON to {release_file_path}")
                    else:
                        print(f"Failed to retrieve DB ID for release ID {release_id}")
                        collected_status = -1

                print(f"Completed processing page {page} for releases of {repo_name}.")
            else:
                print(f"Failed to fetch releases for {repo_name} on page {page}. Status code: {response.status_code}")
                collected_status = -1
                break

            page += 1

        # update release collection status in downstream_repo_info table
        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={DBF.DownstreamRepoInfo.RELEASE_COLLECTION_STATUS: collected_status},
            where=f"{DBF.DownstreamRepoInfo.ID} = %s",
            params=(repo_id,)
        )
        print(f"Updated release collection status to {collected_status} for repository ID {repo_id}")

    print("\nAll done.")