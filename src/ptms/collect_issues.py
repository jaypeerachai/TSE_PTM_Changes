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

    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    ISSUES_DIR = raw_file_config.get_issue_folder()
    print(f"Issue files path: {ISSUES_DIR}")

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.ISSUE_COLLECTION_STATUS} in (0, -1)
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
        repo_dir = os.path.join(ISSUES_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(repo_dir)

        # Prepare the API URL for issues
        api_url = GHConfig.LIST_ISSUES_URL.format(repo_name)
        page = 1
        url, headers, params = gh_config.prepare_list_issues(api_url, page)
        response = gh_config.send_request(url, headers=headers, params=params)
        last_page = gh_config.extract_last_page(response)
        print(f"Total pages of issues: {last_page}")

        while page <= last_page:
            print(f"\n========Fetching issues from page {page}...")
            url, headers, params = gh_config.prepare_list_issues(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)

            if response.status_code == 200:
                issues = response.json()
                if not issues:
                    print(f"No issues found for {repo_name} on page {page}.")
                    break

                for issue in issues:
                    issue_id = issue.get("id")
                    issue_number = issue.get("number")
                    issue_url = GHConfig.ISSUE_INFO_URL.format(repo_name, issue_number)
                    headers = gh_config.prepare_info_request()
                    issue_response = gh_config.send_request(issue_url, headers=headers)

                    if issue_response.status_code == 200:
                        issue_data = issue_response.json()
                        issue_id = issue_data.get("id")
                        issue_url = issue_data.get("html_url", "")
                        node_id = issue_data.get("node_id", "")
                        number = issue_data.get("number")
                        title = issue_data.get("title", "")
                        user = issue_data.get("user", {})
                        user_name = user.get("login", "")
                        user_id = user.get("id")
                        user_type = user.get("type", "")
                        body = issue_data.get("body", "")
                        labels = issue_data.get("labels", [])
                        label_count = len(labels)
                        state = issue_data.get("state", "")
                        locked = issue_data.get("locked", False)
                        assignees = issue_data.get("assignees", [])
                        assignee_count = len(assignees)
                        milestone = issue_data.get("milestone", "")
                        if isinstance(milestone, dict):
                            milestone = json.dumps(milestone)
                        comment_count = issue_data.get("comments", 0)
                        created_at = convert_datetime(issue_data.get("created_at"))
                        updated_at = convert_datetime(issue_data.get("updated_at"))
                        closed_at = convert_datetime(issue_data.get("closed_at"))
                        closed_by = issue_data.get("closed_by") or {}
                        closed_by_id = closed_by.get("id")
                        closed_by_name = closed_by.get("login", "")
                        closed_by_type = closed_by.get("type", "")
                        author_association = issue_data.get("author_association", "")
                        issue_type_data = issue_data.get("type") or {}
                        issue_type_id = issue_type_data.get("id", None)
                        issue_type_node_id = issue_type_data.get("node_id", "")
                        issue_type_name = issue_type_data.get("name", "")
                        issue_type_description = issue_type_data.get("description", "")
                        issue_type_color = issue_type_data.get("color", "")
                        issue_type_created_at = convert_datetime(issue_type_data.get("created_at"))
                        issue_type_updated_at = convert_datetime(issue_type_data.get("updated_at"))
                        active_lock_reason = issue_data.get("active_lock_reason", "")
                        is_pull_request = True if issue_data.get("pull_request") else False
                        sub_issue_summary = issue_data.get("sub_issue_summary", {})
                        sub_issue_summary_total = sub_issue_summary.get("total", 0)
                        sub_issue_summary_completed = sub_issue_summary.get("completed", 0)
                        sub_issue_summary_percent_completed = sub_issue_summary.get("percent_completed", 0)
                        reactions = issue_data.get("reactions", {})
                        reaction_total_count = reactions.get("total_count", 0)
                        reaction_plus1 = reactions.get("+1", 0)
                        reaction_minus1 = reactions.get("-1", 0)
                        reaction_laugh = reactions.get("laugh", 0)
                        reaction_hooray = reactions.get("hooray", 0)
                        reaction_confused = reactions.get("confused", 0)
                        reaction_heart = reactions.get("heart", 0)
                        reaction_rocket = reactions.get("rocket", 0)
                        reaction_eyes = reactions.get("eyes", 0)
                        state_reason = issue_data.get("state_reason", "")
                        repo_id = repo_id

                        print(f"issue_id: {issue_id}, title: {title}, state: {state}, created_at: {created_at}, closed_at: {closed_at}")

                        # prepare data for database insertion
                        issue_data_to_insert = {
                            DBF.Issues.ISSUE_ID: issue_id,
                            DBF.Issues.URL: issue_url,
                            DBF.Issues.NODE_ID: node_id,
                            DBF.Issues.NUMBER: number,
                            DBF.Issues.TITLE: title,
                            DBF.Issues.BODY: body,
                            DBF.Issues.USER_NAME: user_name,
                            DBF.Issues.USER_ID: user_id,
                            DBF.Issues.USER_TYPE: user_type,
                            DBF.Issues.LABEL_COUNT: label_count,
                            DBF.Issues.LABELS: json.dumps(labels),
                            DBF.Issues.STATE: state,
                            DBF.Issues.LOCKED: locked,
                            DBF.Issues.ASSIGNEE_COUNT: assignee_count,
                            DBF.Issues.ASSIGNEES: json.dumps(assignees),
                            DBF.Issues.MILESTONE: milestone,
                            DBF.Issues.COMMENT_COUNT: comment_count,
                            DBF.Issues.CREATED_AT: created_at,
                            DBF.Issues.UPDATED_AT: updated_at,
                            DBF.Issues.CLOSED_AT: closed_at,
                            DBF.Issues.CLOSED_BY_ID: closed_by_id,
                            DBF.Issues.CLOSED_BY_NAME: closed_by_name,
                            DBF.Issues.CLOSED_BY_TYPE: closed_by_type,
                            DBF.Issues.AUTHOR_ASSOCIATION: author_association,
                            DBF.Issues.TYPE_ID: issue_type_id,
                            DBF.Issues.TYPE_NODE_ID: issue_type_node_id,
                            DBF.Issues.TYPE_NAME: issue_type_name,
                            DBF.Issues.TYPE_DESCRIPTION: issue_type_description,
                            DBF.Issues.TYPE_COLOR: issue_type_color,
                            DBF.Issues.TYPE_CREATED_AT: issue_type_created_at,
                            DBF.Issues.TYPE_UPDATED_AT: issue_type_updated_at,
                            DBF.Issues.ACTIVE_LOCK_REASON: active_lock_reason,
                            DBF.Issues.IS_PULL_REQUEST: is_pull_request,
                            DBF.Issues.SUB_ISSUE_SUMMARY_TOTAL: sub_issue_summary_total,
                            DBF.Issues.SUB_ISSUE_SUMMARY_COMPLETED: sub_issue_summary_completed,
                            DBF.Issues.SUB_ISSUE_SUMMARY_PERCENT_COMPLETED:
                                sub_issue_summary_percent_completed,
                            DBF.Issues.REACTION_TOTAL_COUNT: reaction_total_count,
                            DBF.Issues.REACTION_PLUS1: reaction_plus1,
                            DBF.Issues.REACTION_MINUS1: reaction_minus1,
                            DBF.Issues.REACTION_LAUGH: reaction_laugh,
                            DBF.Issues.REACTION_HOORAY: reaction_hooray,
                            DBF.Issues.REACTION_CONFUSED: reaction_confused,
                            DBF.Issues.REACTION_HEART: reaction_heart,
                            DBF.Issues.REACTION_ROCKET: reaction_rocket,
                            DBF.Issues.REACTION_EYES: reaction_eyes,
                            DBF.Issues.STATE_REASON: state_reason,
                            DBF.Issues.REPO_ID: repo_id
                        }

                        # insert issue data into the database
                        db_config.insert_to_db(
                            DBT.ISSUES.value,
                            data_dict=issue_data_to_insert,
                        )
                        print(f"Inserted issue {issue_id} into database for repository {repo_name}.")
                    else:
                        print(f"Failed to fetch issue details for {issue_url} in repository {repo_name}: {issue_response.status_code}")
                        collected_status = -1
                        break

                    # write issue data to file
                    # get id from database
                    issue_id_db = db_config.select_from_db(
                        DBT.ISSUES.value,
                        columns=[DBF.Issues.ID],
                        where=f"{DBF.Issues.ISSUE_ID} = %s AND {DBF.Issues.REPO_ID} = %s",
                        params=(issue_id, repo_id),
                        fetch_one=True
                    )
                    if issue_id_db:
                        issue_id_db = issue_id_db[DBF.Issues.ID]
                        issue_file_path = os.path.join(repo_dir, f"{issue_id_db}.json")
                        with open(issue_file_path, 'w', encoding='utf-8') as issue_file:
                            json.dump(issue_data, issue_file, indent=4)
                        print(f"Saved issue {issue_id} to file {issue_file_path}.")
                    else:
                        print(f"Failed to find issue ID {issue_id} in database for repository {repo_name}.")
                        collected_status = -1
                        break
                print(f"Processed {len(issues)} issues from page {page} for repository {repo_name}.")
            else:
                print(f"Failed to fetch commits for {repo_name} on page {page}: {response.status_code}")
                collected_status = -1
                break

            page += 1

        time.sleep(1)

        # Update the repository's issue collection status in the database
        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={DBF.DownstreamRepoInfo.ISSUE_COLLECTION_STATUS: collected_status},
            where=f"{DBF.DownstreamRepoInfo.ID} = %s",
            params=(repo_id,)
        )
        print(f"Updated issue collection status for {repo_name} to {collected_status}.")

    print("All repositories processed successfully!")