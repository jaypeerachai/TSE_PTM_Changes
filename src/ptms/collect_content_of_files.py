"""
Download and store the raw content of reused files listed in the database.
"""

import os
import requests
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.RawFileConfig import RawFileConfig


def html_to_raw_url(html_url):
    parts = html_url.replace("https://github.com/", "").split("/blob/")
    return f"https://raw.githubusercontent.com/{parts[0]}/{parts[1]}"

def write_to_file(file, full_file_path):
    raw_url = html_to_raw_url(file['html_url'])
    print(f"Fetching content from: {raw_url}")

    response = requests.get(raw_url)
    response.raise_for_status()

    source_code = response.text
    with open(full_file_path, "w", encoding="utf-8") as f:
        f.write(source_code)

    print(f"Content written to {full_file_path}")

if __name__ == "__main__":
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # Initialize raw file configuration
    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    REUSED_FILES_DIR = raw_file_config.get_reused_file_folder()
    print(f"Reused files path: {REUSED_FILES_DIR}")

    print("Fetching reused files from the database...")
    downstream_repos = db_config.select_from_db(
        DBT.REPOS.value, columns="*", where=None, params=None, 
        # order_by=f"{DBF.Repos.ID} ASC",
        order_by = f"{DBF.Repos.ID} DESC",
        fetch_one=False)

    print(f"Found {len(downstream_repos)} downstream repositories in the database.")
    for repo in downstream_repos:
        # get only id and name
        repo_id = repo[DBF.Repos.ID]
        repo_name = repo[DBF.Repos.NAME]
        print(f"Processing repository: {repo_name} (ID: {repo_id})")
        reused_files = db_config.select_from_db(
            DBT.FILES.value,
            columns="*",
            where=f"{DBF.Files.REPO_ID} = %s AND {DBF.Files.CONTENT_COLLECTION_STATUS} in (0, -1)",
            params=(repo_id,),
            order_by=DBF.Files.ID,
            fetch_one=False
        )

        # create directory for the repo if it doesn't exist
        repo_path_name = str(repo_id) + "_" + repo_name
        repo_dir = os.path.join(REUSED_FILES_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(repo_dir)

        for file in reused_files:
            collection_status = -1
            try:
                file_path_name = str(file[DBF.Files.ID]) + "_" + file[DBF.Files.NAME]
                full_file_path = os.path.join(repo_dir, file_path_name)
                write_to_file(file, full_file_path)
                collection_status = 1  # Successfully collected content
            except Exception as e:
                print(f"Error processing file {file['name']} in repository {repo_name}: {e}")
            # update content_collection_status in the database to 1 (update_db(table_name, data_dict, where))
            db_config.update_db(
                DBT.FILES.value,
                data_dict={DBF.Files.CONTENT_COLLECTION_STATUS: collection_status},
                where=f"{DBF.Files.ID} = %s",
                params=(file[DBF.Files.ID],)
            )
        print(f"Finished processing repository: {repo_name}")
        print(f"Total reused files in {repo_name}: {len(reused_files)}")
        print("-" * 40)
        
    db_config.close_db_connection()
