"""
Collect issue comment information for downstream repositories that have passed preliminary filtering and static analysis, and save the"""

import os
from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils import GHConfig as GHConfig
from utils.Utils import convert_datetime
from utils.RawFileConfig import RawFileConfig

if __name__ == "__main__":
    db_config = DatabaseConfig()
    db_config.create_db_connection()
    
    # Initialize raw file configuration
    raw_file_config = RawFileConfig(db=db_config.db)
    print(f"Raw file root path: {raw_file_config.root_path}")
    ISSUE_COMMENTS_DIR = raw_file_config.get_issue_comment_folder()
    print(f"Issue comments files path: {ISSUE_COMMENTS_DIR}")

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
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
        repo_id = repo[DBF.DownstreamRepoInfo.ID]
        repo_name = repo[DBF.DownstreamRepoInfo.FULL_NAME]
        print(f"\n===============Processing repository: {repo_name} (ID: {repo_id})")
        issues = db_config.select_from_db(
            DBT.ISSUES.value,
            columns="*",
            where=f"""
                {DBF.Issues.REPO_ID} = {repo_id}
                AND {DBF.Issues.COMMENT_COLLECTION_STATUS} = 0
            """,
            order_by=f"{DBF.Issues.ID} ASC",
            fetch_one=False
        )
        repo_path_name = str(repo_id) + "_" + repo_name.split("/")[-1]
        repo_dir = os.path.join(ISSUE_COMMENTS_DIR, repo_path_name)
        raw_file_config.create_folder_if_not_exists(repo_dir)
        
        for issue in issues:
            collected_status = 1
            # issue_id = issue[DBF.Issues.ISSUE_ID]
            issue_id = issue[DBF.Issues.NUMBER] # Use NUMBER as ISSUE_ID
            issue_db_id = issue[DBF.Issues.ID]
            issue_dir = os.path.join(repo_dir, str(issue_db_id))
            if not os.path.exists(issue_dir):
                os.makedirs(issue_dir)
            api_url = GHConfig.ISSUE_COMMENTS_URL.format(repo_name, issue_id)
            page = 1
            url, headers, params = gh_config.prepare_list_comments(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)
            last_page = gh_config.extract_last_page(response)
            print(f"Total pages of comments: {last_page}")

            while page <= last_page:
                print(f"\nFetching page {page} of comments for issue {issue_id} in repository {repo_name}")
                url, headers, params = gh_config.prepare_list_comments(api_url, page)
                response = gh_config.send_request(url, headers=headers, params=params)
                print(f"Response status code: {response.status_code}")
                print(f"Response: {response.text[:100]}...")  # Print first 100 characters of response for debugging
                if response.status_code == 200:
                    comments = response.json()
                    if not comments:
                        print(f"No comments found for issue {issue_id} in repository {repo_name}")
                        break
                    
                    for comment in comments:
                        comment_id = comment.get("id")
                        comment_url = comment.get("html_url")
                        created_by_name = comment.get("user", {}).get("login")
                        created_by_id = comment.get("user", {}).get("id")
                        created_at = comment.get("created_at")
                        updated_at = comment.get("updated_at")
                        author_association = comment.get("author_association")
                        body = comment.get("body", "")
                        reactions = comment.get("reactions", {})
                        reaction_total_count = reactions.get("total_count", 0)
                        reaction_plus1 = reactions.get("+1", 0)
                        reaction_minus1 = reactions.get("-1", 0)
                        reaction_laugh = reactions.get("laugh", 0)
                        reaction_hooray = reactions.get("hooray", 0)
                        reaction_confused = reactions.get("confused", 0)
                        reaction_heart = reactions.get("heart", 0)
                        reaction_rocket = reactions.get("rocket", 0)
                        reaction_eyes = reactions.get("eyes", 0)

                        comment_data = {
                            DBF.IssueComments.COMMENT_ID: comment_id,
                            DBF.IssueComments.URL: comment_url,
                            DBF.IssueComments.CREATED_BY_NAME: created_by_name,
                            DBF.IssueComments.CREATED_BY_ID: created_by_id,
                            DBF.IssueComments.CREATED_AT: convert_datetime(created_at),
                            DBF.IssueComments.UPDATED_AT: convert_datetime(updated_at),
                            DBF.IssueComments.AUTHOR_ASSOCIATION: author_association,
                            DBF.IssueComments.BODY: body,
                            DBF.IssueComments.REACTION_TOTAL_COUNT: reaction_total_count,
                            DBF.IssueComments.REACTION_PLUS1: reaction_plus1,
                            DBF.IssueComments.REACTION_MINUS1: reaction_minus1,
                            DBF.IssueComments.REACTION_LAUGH: reaction_laugh,
                            DBF.IssueComments.REACTION_HOORAY: reaction_hooray,
                            DBF.IssueComments.REACTION_CONFUSED: reaction_confused,
                            DBF.IssueComments.REACTION_HEART: reaction_heart,
                            DBF.IssueComments.REACTION_ROCKET: reaction_rocket,
                            DBF.IssueComments.REACTION_EYES: reaction_eyes,
                            DBF.IssueComments.ISSUE_ID: issue_db_id
                        }

                        # insert comment data into the database
                        db_config.insert_to_db(
                            DBT.ISSUE_COMMENTS.value,
                            data_dict=comment_data,
                        )
                        print(f"Inserted comment data for comment ID {comment_id} in issue {issue_id}")
                else:
                    break    
                page += 1

            # Update the issue's comment collection status
            db_config.update_db(
                DBT.ISSUES.value,
                data_dict={DBF.Issues.COMMENT_COLLECTION_STATUS: collected_status},
                where=f"{DBF.Issues.ID} = %s",
                params=(issue_db_id,)
            )

            print(f"Updated comment collection status for issue DB ID {issue_db_id} (GitHub issue #{issue_id}) in repository {repo_name}")
        print(f"Completed processing repository: {repo_name} ...\n")

    print("\nAll repositories processed. Closing database connection.")