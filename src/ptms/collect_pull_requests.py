"""
Collect pull request information for downstream repositories that have passed preliminary filtering and static analysis, and save the data to the database and raw files.
"""
import os
import time
import json
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils import GHConfig as GHConfig
from utils.Utils import convert_datetime
from utils.RawFileConfig import RawFileConfig


if __name__ == "__main__":
    # Initialize database and GitHub configurations
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # Initialize raw file configuration
    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    PULLS_DIR = raw_file_config.get_pr_folder()
    print(f"Pull request files path: {PULLS_DIR}")
    COMMITS_DIR = raw_file_config.get_commit_folder()
    print(f"Commit files path: {COMMITS_DIR}")
    
    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.PULL_COLLECTION_STATUS} in (0, -1)
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

    for repo in repos:
        collected_status = 1
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        repo_name = repo[DBF.DownstreamRepoInfo.FULL_NAME]
        print(f"\n=========================Processing repository {repo_name} (ID: {repo_id})...")

        # create directory for the repo if it doesn't exist
        repo_path_name = str(repo_id) + "_" + repo_name.split("/")[-1]
        pull_repo_dir = os.path.join(PULLS_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(pull_repo_dir)

        commits_repo_dir = os.path.join(COMMITS_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(commits_repo_dir)

        # select pull requests from table issues
        pulls = db_config.select_from_db(
            DBT.ISSUES.value,
            columns="*",
            where=f"{DBF.Issues.REPO_ID} = %s AND {DBF.Issues.IS_PULL_REQUEST} = 1",
            params=(repo_id,),
            order_by=f"{DBF.Issues.CREATED_AT} ASC",
            fetch_one=False
        )

        print(f"Found {len(pulls)} pull requests for repository {repo_name}.")

        for pull in pulls:
            issue_id = pull[DBF.Issues.ID]
            pull_number = pull[DBF.Issues.NUMBER]
            pull_url = GHConfig.PULL_INFO_URL.format(repo_name, pull_number)
            headers = gh_config.prepare_info_request()
            response = gh_config.send_request(pull_url, headers=headers)

            if response.status_code == 200:
                pull_data = response.json()
                pull_id = pull_data.get("id")
                pull_url = pull_data.get("html_url", "")
                created_at = convert_datetime(pull_data.get("created_at"))
                closed_at = convert_datetime(pull_data.get("closed_at"))
                merged_at = convert_datetime(pull_data.get("merged_at"))
                merged_commit_sha = pull_data.get("merge_commit_sha", "")
                requested_reviewers = pull_data.get("requested_reviewers", [])
                requested_teams = pull_data.get("requested_teams", [])
                head = pull_data.get("head", {})
                base = pull_data.get("base", {})
                merged = pull_data.get("merged", False)
                merged_by = pull_data.get("merged_by") or {}
                merged_by_id = merged_by.get("id")
                merged_by_name = merged_by.get("login", "")
                merged_by_type = merged_by.get("type", "")
                mergeable = pull_data.get("mergeable", None)
                rebaseable = pull_data.get("rebaseable", None)
                review_comments = pull_data.get("review_comments", 0)
                commits = pull_data.get("commits", 0)
                additions = pull_data.get("additions", 0)
                deletions = pull_data.get("deletions", 0)
                changed_files = pull_data.get("changed_files", 0)
                issue_id = issue_id
                repo_id = repo_id
                print(f"Pull ID: {pull_id}, URL: {pull_url}, Created At: {created_at}, Merged: {merged}")

                # prepare data for database insertion
                pull_data_to_insert = {
                    DBF.PullRequests.PULL_ID: pull_id,
                    DBF.PullRequests.URL: pull_url,
                    DBF.PullRequests.CREATED_AT: created_at,
                    DBF.PullRequests.CLOSED_AT: closed_at,
                    DBF.PullRequests.MERGED_AT: merged_at,
                    DBF.PullRequests.MERGED_COMMIT_SHA: merged_commit_sha,
                    DBF.PullRequests.REQUESTED_REVIEWERS: json.dumps(requested_reviewers),
                    DBF.PullRequests.REQUESTED_TEAMS: json.dumps(requested_teams),
                    DBF.PullRequests.HEAD: json.dumps(head),
                    DBF.PullRequests.BASE: json.dumps(base),
                    DBF.PullRequests.MERGED: merged,
                    DBF.PullRequests.MERGED_BY_ID: merged_by_id,
                    DBF.PullRequests.MERGED_BY_NAME: merged_by_name,
                    DBF.PullRequests.MERGED_BY_TYPE: merged_by_type,
                    DBF.PullRequests.MERGEABLE: mergeable,
                    DBF.PullRequests.REBASEABLE: rebaseable,
                    DBF.PullRequests.REVIEW_COMMENTS: review_comments,
                    DBF.PullRequests.COMMITS: commits,
                    DBF.PullRequests.ADDITIONS: additions,
                    DBF.PullRequests.DELETIONS: deletions,
                    DBF.PullRequests.CHANGED_FILES: changed_files,
                    DBF.PullRequests.ISSUE_ID: issue_id,
                    DBF.PullRequests.REPO_ID: repo_id
                }

                # Insert the pull request data into the database
                db_config.insert_to_db(
                    DBT.PULL_REQUESTS.value,
                    data_dict=pull_data_to_insert,
                )
                print(f"Inserted pull request {pull_id} into database for repository {repo_name}.")

                # Write pull request data to file
                pull_id_db = db_config.select_from_db(
                    DBT.PULL_REQUESTS.value,
                    columns=[DBF.PullRequests.ID],
                    where=f"{DBF.PullRequests.PULL_ID} = %s AND {DBF.PullRequests.REPO_ID} = %s",
                    params=(pull_id, repo_id),
                    fetch_one=True
                )
                if pull_id_db:
                    pull_id_db = pull_id_db[DBF.PullRequests.ID]
                    pull_file_path = os.path.join(pull_repo_dir, f"{pull_id_db}.json")
                    with open(pull_file_path, 'w', encoding='utf-8') as pull_file:
                        json.dump(pull_data, pull_file, indent=4)
                    print(f"Saved pull request {pull_id} to file {pull_file_path}.")
                else:
                    print(f"Failed to find pull request ID {pull_id} in database for repository {repo_name}.")
                    collected_status = -1
                
                print(f"Processing commits for pull request {pull_id_db} in repository {repo_name}...")
                pull_commits_url = GHConfig.PULL_COMMITS_URL.format(repo_name, pull_number)
                commit_page = 1
                url, headers, params = gh_config.prepare_list_commits(pull_commits_url, page=commit_page)
                commit_response = gh_config.send_request(url, headers=headers, params=params)
                commit_last_page = gh_config.extract_last_page(commit_response)

                while commit_page <= commit_last_page:
                    url, headers, params = gh_config.prepare_list_commits(pull_commits_url, page=commit_page)
                    commit_response = gh_config.send_request(url, headers=headers, params=params)

                    if commit_response.status_code == 200:
                        commits_data = commit_response.json()
                        print(f"Found {len(commits_data)} commits for pull request {pull_id_db} in repository {repo_name} on page {commit_page}.")
                        if not commits_data:
                            break

                        for commit in commits_data:
                            commit_sha = commit.get("sha", "")
                            select_commit = db_config.select_from_db(
                                DBT.COMMITS.value,
                                columns=[DBF.Commits.ID],
                                where=f"{DBF.Commits.COMMIT_ID} = %s AND {DBF.Commits.REPO_ID} = %s",
                                params=(commit_sha, repo_id),
                                fetch_one=True
                            )
                            if select_commit:
                                print(f"Commit {commit_sha} already exists in the database for repository {repo_name}.")
                                commit_id = select_commit[DBF.Commits.ID]
                            else:
                                print(f"Processing commit {commit_sha} for pull request {pull_id_db} in repository {repo_name}.")
                                commit_url = GHConfig.COMMIT_INFO_URL.format(repo_name, commit_sha)
                                headers = gh_config.prepare_info_request()
                                commit_response = gh_config.send_request(commit_url, headers=headers)

                                if commit_response.status_code == 200:
                                    commit_data = commit_response.json()
                                    commit_sha = commit_data.get("sha", "")
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

                                    # Prepare data for database insertion
                                    commit_data_dict = {
                                        DBF.Commits.COMMIT_ID: commit_sha,
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
                                        DBF.Commits.PARENTS: json.dumps(parents),  # Store parents as JSON string
                                        DBF.Commits.TOTAL: total,
                                        DBF.Commits.ADDITIONS: additions,
                                        DBF.Commits.DELETIONS: deletions,
                                        DBF.Commits.CHANGED_FILE_COUNT: changed_file_count,
                                        DBF.Commits.REPO_ID: repo_id
                                    }

                                    # Insert commit data into the database
                                    db_config.insert_to_db(
                                        DBT.COMMITS.value,
                                        data_dict=commit_data_dict
                                    )
                                    print(f"Inserted commit {commit_sha} into database for repository {repo_name}.")

                                    select_commit = db_config.select_from_db(
                                        DBT.COMMITS.value,
                                        columns=[DBF.Commits.ID],
                                        where=f"{DBF.Commits.COMMIT_ID} = %s AND {DBF.Commits.REPO_ID} = %s",
                                        params=(commit_sha, repo_id),
                                        fetch_one=True
                                    )
                                    commit_id = select_commit[DBF.Commits.ID]
                                    commit_file_path = os.path.join(commits_repo_dir, f"{commit_id}.json")
                                    with open(commit_file_path, "w", encoding="utf-8") as f:
                                        json.dump(commit_data, f, ensure_ascii=False, indent=4)
                                    print(f"Commit data for {commit_id} written to {commit_file_path}.")

                            db_config.insert_to_db(
                                DBT.PULL_REQUESTS_TO_COMMITS.value,
                                data_dict={
                                    DBF.PullRequestsToCommits.PULL_ID: pull_id_db,
                                    DBF.PullRequestsToCommits.COMMIT_ID: commit_id,
                                    DBF.PullRequestsToCommits.IS_MERGED: merged
                                }
                            )
                            print(f"Inserted commit {commit_sha} for pull request {pull_id_db} in repository {repo_name}.")
                                
                    commit_page += 1

            else:
                print(f"Failed to fetch pull request details for {pull_url} in repository {repo_name}: {response.status_code}")
                collected_status = -1
                continue

        time.sleep(1)  # Sleep to avoid hitting rate limit

        # Update the repository's pull collection status in the database
        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={
                DBF.DownstreamRepoInfo.PULL_COLLECTION_STATUS: collected_status
            },
            where=f"{DBF.DownstreamRepoInfo.ID} = %s",
            params=(repo_id,)
        )
        print(f"Updated pull collection status for repository {repo_name} to {collected_status}.")

    print("All repositories processed successfully!")