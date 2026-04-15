"""
Collect reused files (with repos and owners' information) for PTM-related signatures using GitHub code search.

This script looks up import and call signatures, stores the matched
repositories and files in the local database, and saves the reused text
matches linked to each signature-file pair.

Before running:
- make sure to set up the database first
- make sure GitHub access is ready (key) 
- make sure the signatures table is already filled
- make sure you know the start/end index and prefix choice
"""

import pandas as pd
import pymysql
import time
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHSearchConfig import GitHubSearchConfig

def insert_owner_to_db(owner_result):
    """
    Insert owner information into the local db
    """
    existing_owner = db_config.select_from_db(
        DBT.OWNERS.value,
        columns="1",
        where=f"{DBF.Owners.OWNER_ID} = %s",
        params=(owner_result['id'],),
        fetch_one=True
    )
    if existing_owner:
        print(f"   🧑‍💻 ⚠️ Owner {owner_result['login']} already exists in the database.")
        return

    data = {
        DBF.Owners.OWNER_ID: owner_result['id'],
        DBF.Owners.NAME: owner_result['login'],
        DBF.Owners.API_URL: owner_result['url'],
        DBF.Owners.HTML_URL: owner_result['html_url'],
        DBF.Owners.TYPE: owner_result['type']
    }
    db_config.insert_to_db(DBT.OWNERS.value, data)


def insert_downstream_repo_to_db(repos_result):
    """
    Insert downstream repository information into the local db
    """
    existing_repo = db_config.select_from_db(
        DBT.REPOS.value,
        columns="1",
        where=f"{DBF.Repos.REPO_ID} = %s",
        params=(repos_result['id'],),
        fetch_one=True
    )
    if existing_repo:
        print(f"   📦 ⚠️ Repository {repos_result['full_name']} already exists in the database.")
        return

    owner = db_config.select_from_db(
        DBT.OWNERS.value,
        columns=DBF.Owners.ID,
        where=f"{DBF.Owners.OWNER_ID} = %s",
        params=(repos_result['owner']['id'],),
        fetch_one=True
    )

    data = {
        DBF.Repos.REPO_ID: repos_result['id'],
        DBF.Repos.NAME: repos_result['name'],
        DBF.Repos.FULL_NAME: repos_result['full_name'],
        DBF.Repos.FORK: repos_result['fork'],
        DBF.Repos.API_URL: repos_result['url'],
        DBF.Repos.HTML_URL: repos_result['html_url'],
        DBF.Repos.OWNER_ID: owner[DBF.Owners.ID]
    }
    db_config.insert_to_db(DBT.REPOS.value, data)


def insert_reused_files_to_db(file_result):
    """
    Insert reused file information into the local db
    """
    repo = db_config.select_from_db(
        DBT.REPOS.value,
        columns = DBF.Repos.ID,
        where = f"{DBF.Repos.REPO_ID} = %s",
        params = (file_result['repository']['id'],),
        fetch_one = True
    )
    repo_id = repo[DBF.Repos.ID]
    existing_file = db_config.select_from_db(
        DBT.FILES.value,
        columns = "1",
        where = f"{DBF.Files.PATH} = %s AND {DBF.Files.REPO_ID} = %s",
        params = (file_result['path'], repo_id),
        fetch_one = True
    )
    if existing_file:
        print(f"   📄 ⚠️ File {file_result['path']} already exists in the database.")
    else:    
        data = {
            DBF.Files.NAME: file_result['name'],
            DBF.Files.PATH: file_result['path'],
            DBF.Files.SHA: file_result['sha'],
            DBF.Files.API_URL: file_result['url'],
            DBF.Files.HTML_URL: file_result['html_url'],
            DBF.Files.REPO_ID: repo_id
        }
        db_config.insert_to_db(DBT.FILES.value, data)

    # ---------- insert signature to file mapping ----------
    sig = db_config.select_from_db(
        DBT.SIGNATURES.value,
        columns = "id",
        where = f"`{DBF.Signatures.IMPORT}` = %s AND `{DBF.Signatures.CALL}` = %s",
        params = (import_signature, call_signature),
        fetch_one=True
    )

    file_entry = db_config.select_from_db(
        DBT.FILES.value,
        columns = "id",
        where = f"{DBF.Files.PATH} = %s AND {DBF.Files.REPO_ID} = %s",
        params = (file_result['path'], repo_id),
        fetch_one=True
    )

    if sig and file_entry:
        db_config.insert_to_db(DBT.SIG_TO_FILE.value, {
            DBF.SigToFile.SIGNATURE_ID: sig['id'],
            DBF.SigToFile.FILE_ID: file_entry['id']
        })
    else:
        print(f"   📄 ❌ Missing signature or file reference for {file_result['path']}")

    # ---------- insert reused text matches ----------
    sig_to_file_entry = db_config.select_from_db(
        DBT.SIG_TO_FILE.value,
        columns = DBF.SigToFile.ID,
        where = f"{DBF.SigToFile.SIGNATURE_ID} = %s AND {DBF.SigToFile.FILE_ID} = %s",
        params = (sig['id'], file_entry['id']),
        fetch_one=True
    )
    try:
        insert_reused_text_matches_to_db(file_result.get("text_matches", []), sig_to_file_entry[DBF.SigToFile.ID])
    # except for duplicate entries
    except pymysql.IntegrityError as e:
        print(f"   📄 ⚠️ Duplicate text matches for file {file_result['path']}: {e}")


def insert_reused_text_matches_to_db(matches, sig_to_file_id):
    for match_obj in matches:
        object_url = match_obj.get("object_url")
        object_type = match_obj.get("object_type")
        property_ = match_obj.get("property")
        fragment = match_obj.get("fragment")

        for m in match_obj.get("matches", []):
            match_text = m.get("text")
            match_indices = str(m.get("indices"))

            existing_text_matches = db_config.select_from_db(
                DBT.REUSED_TEXT_MATCHES.value,
                columns = "1",
                where = f"{DBF.ReusedTextMatches.MATCH_TEXT} = %s AND {DBF.ReusedTextMatches.MATCH_INDICES} = %s AND {DBF.ReusedTextMatches.OBJECT_URL} = %s AND {DBF.ReusedTextMatches.FRAGMENT} = %s AND {DBF.ReusedTextMatches.SIG_TO_FILE_ID} = %s",
                params = (match_text, match_indices, object_url, fragment, sig_to_file_id),
                fetch_one=True
            )
            if existing_text_matches:
                print(f"   📝 ⚠️ Text match '{match_text}' already exists in the database for sig_to_file_id={sig_to_file_id}.")
            else:
                data = {
                    DBF.ReusedTextMatches.MATCH_TEXT: match_text,
                    DBF.ReusedTextMatches.MATCH_INDICES: match_indices,
                    DBF.ReusedTextMatches.PROPERTY: property_,
                    DBF.ReusedTextMatches.FRAGMENT: fragment,
                    DBF.ReusedTextMatches.OBJECT_TYPE: object_type,
                    DBF.ReusedTextMatches.OBJECT_URL: object_url,
                    DBF.ReusedTextMatches.SIG_TO_FILE_ID: sig_to_file_id
                }

                db_config.insert_to_db(DBT.REUSED_TEXT_MATCHES.value, data)
                print(f"   📝 ✅ Inserted text match '{match_text}' for sig_to_file_id={sig_to_file_id}.")

    print(f"   ✏️ Inserted {sum(len(m.get('matches', [])) for m in matches)} text matches linked to sig_to_file_id={sig_to_file_id}.")




def update_collection_status_signatures(collection_status, collection_status_col):
    # update collection status for signatures
    db_config.update_db(
        DBT.SIGNATURES.value,
        data_dict={collection_status_col: collection_status},
        where=f"`{DBF.Signatures.IMPORT}` = %s AND `{DBF.Signatures.CALL}` = %s",
        params=(import_signature, call_signature)
    )
    print(f"   ✅ Updated collection status for signature {import_signature} -> {call_signature} to {collection_status}.")

def update_collection_time_signatures(collection_time, collection_time_col):
    # update collection time for signatures
    db_config.update_db(
        DBT.SIGNATURES.value,
        data_dict={collection_time_col: collection_time},
        where=f"`{DBF.Signatures.IMPORT}` = %s AND `{DBF.Signatures.CALL}` = %s",
        params=(import_signature, call_signature)
    )
    print(f"   ✅ Updated collection time for signature {import_signature} -> {call_signature} to {collection_time}.")

def select_signatures(collection_status_col):
    # Select all signatures from the database
    signatures = db_config.select_from_db(
        DBT.SIGNATURES.value,
        columns="*",
        # where=f" {collection_status_col} in ('-1', '0')", # not used --> used this instead (if signature[collection_status_col] != 1:)
        order_by=DBF.Signatures.ID,
        fetch_one=False
    )
    return signatures

if __name__ == "__main__":
    # Initialize DB connection
    db_config  = DatabaseConfig()
    connection, cursor = db_config.create_db_connection()

    # initialize GitHub search client
    gh = GitHubSearchConfig()
    gh.init_github_access()

    # input start index and end index
    start_index = int(input("Enter sig db start index: "))
    end_index = int(input("Enter sig db end index: "))
    while True:
        try:
            prefix_choice = int(input("Enter prefix import (1) from, (2) import: "))
            if prefix_choice == 1:
                prefix = "from"
                prefix_import_col = DBF.Signatures.PREFIX_1
                collection_time_col = DBF.Signatures.COLLECTION_TIME_1
                collection_status_col = DBF.Signatures.COLLECTION_STATUS_1
            elif prefix_choice == 2:
                prefix = "import"
                prefix_import_col = DBF.Signatures.PREFIX_2
                collection_time_col = DBF.Signatures.COLLECTION_TIME_2
                collection_status_col = DBF.Signatures.COLLECTION_STATUS_2
            else:
                print("Invalid choice. Please enter 1 or 2.")
                continue
            break
        except ValueError:
            print("Invalid input. Please enter a valid number.")

    print(f"Collecting signatures from db index {start_index} to {end_index}...")

    # import_signature = "transformers"
    # call_signature = "pipeline"

    # import_signature = "transformers"
    # call_signature = "from_pretrained"

    signatures = select_signatures(collection_status_col)
    if start_index < 0 or end_index >= len(signatures):
        print("Invalid index range. Please provide a valid range.")
        exit(1)

    for signature in signatures[start_index:end_index + 1]:
        if signature[collection_status_col] != 1:
            start_time = time.time()
            import_signature = signature[DBF.Signatures.IMPORT]
            prefix_import = signature[prefix_import_col]
            full_import_signature = prefix_import + " " + import_signature
            call_signature = signature[DBF.Signatures.CALL]
            full_call_signature = call_signature + "("

            print(f"\nProcessing signature: {import_signature} ({full_import_signature}) - {call_signature} ({full_call_signature})")

            # Stream results and insert immediately
            # i = 0
            update_collection_status_signatures(-1, collection_status_col)
            try:
                for item in gh.code_search_items(full_import_signature, full_call_signature):
                    insert_owner_to_db(item["repository"]["owner"])
                    # print(f"Processing item {i + 1}: owner {item['repository']['owner']['login']}")
                    insert_downstream_repo_to_db(item["repository"])
                    # print(f"Processing item {i + 1}: repository {item['repository']['full_name']}")
                    insert_reused_files_to_db(item)
                    # print(f"Processing item {i + 1}: file {item['path']}")
                    # i += 1
                end_time = time.time()
                elapsed_time = end_time - start_time
                update_collection_time_signatures(elapsed_time, collection_time_col)
                update_collection_status_signatures(1, collection_status_col)
            except Exception as e:
                print(f"Error: {e}")
                update_collection_status_signatures(-1, collection_status_col)

            print(f"Finished processing signature: {import_signature} ({full_import_signature}) - {call_signature} ({full_call_signature}) in {elapsed_time:.2f} seconds.")
            print("----------------------------------------")

    # Close DB
    db_config.close_db_connection()
