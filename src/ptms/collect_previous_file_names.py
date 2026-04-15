"""
This script collects the previous filenames for renamed files in commits and updates the database accordingly.
"""

import os
import json
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils import GHConfig as GHConfig
from utils.RawFileConfig import RawFileConfig

if __name__ == "__main__":
    # Initialize database and GitHub configurations
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # Initialize raw file configuration
    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    COMMITS_DIR = raw_file_config.get_commit_folder()
    print(f"Commit files path: {COMMITS_DIR}")
    
    commit_files = db_config.select_from_db(
        table_name=DBT.COMMIT_FILES.value,
        columns="id, commit_id, file_sha, file_name, status",
        where=f"""
            {DBF.CommitFiles.STATUS} = 'renamed'
            AND NOT EXISTS (
                SELECT 1
                FROM {DBT.COMMIT_PREVIOUS_FILENAMES.value} cpf
                WHERE cpf.{DBF.CommitPreviousFilenames.COMMIT_FILE_ID} = {DBT.COMMIT_FILES.value}.{DBF.CommitFiles.ID}
            )
        """
    )
    print(f"Total renamed files still missing previous_filename: {len(commit_files)}")

    for files in commit_files:
        commit_file_id = files['id']
        commit_id = files['commit_id']
        file_sha = files['file_sha']
        file_name = files['file_name']
        status = files['status']
        print(f"Processing commit file ID: {commit_file_id}, Commit ID: {commit_id}, File SHA: {file_sha}, File Name: {file_name}, Status: {status}")

        # get repo_id from commits table and repo_name from repositories table
        commit_data = db_config.select_from_db(
            table_name=DBT.COMMITS.value,
            columns="repo_id",
            where=f"id = {commit_id}",
            fetch_one=True
        )
        repo_id = commit_data['repo_id']
        repo_data = db_config.select_from_db(
            table_name=DBT.REPOS.value,
            columns="name",
            where=f"id = {repo_id}",
            fetch_one=True
        )
        repo_name = repo_data['name']

        # Load the commit data from the corresponding JSON file
        repo_path_name = str(repo_id) + "_" + repo_name
        repo_dir = os.path.join(COMMITS_DIR, repo_path_name)
        commit_file_path = os.path.join(repo_dir, f"{commit_id}.json")

        with open(commit_file_path, 'r') as f:
            commit_data = json.load(f)

        # Find the specific file in the commit data
        file_data = next((f for f in commit_data.get('files', []) if f['sha'] == file_sha and f['filename'] == file_name), None)
        # if not file_data:
        #     print(f"File with SHA {file_sha} not found in commit {commit_id}. Skipping.")
        #     continue

        previous_filename = file_data.get('previous_filename')
        # if not previous_filename:
        #     print(f"No previous filename found for file with SHA {file_sha} in commit {commit_id}. Skipping.")
        #     continue

        # Insert the previous filename into the database (commit_previous_filenames table)
        db_config.insert_to_db(
            table_name=DBT.COMMIT_PREVIOUS_FILENAMES.value,
            data_dict={
                "commit_file_id": commit_file_id,
                "previous_filename": previous_filename
            }
        )
        print(f"Inserted previous filename '{previous_filename}' for commit file ID {commit_file_id}.")
        # time.sleep(0.1)
    print("Completed processing all renamed files. [collect_previous_file_names.py]")
