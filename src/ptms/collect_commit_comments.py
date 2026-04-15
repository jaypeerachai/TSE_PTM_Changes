"""
Collect commit comments for PTM-mapped downstream repositories.

This is an optional metadata step. It stores GitHub commit comments in the
database so later manual inspection can use developer discussion around commits.
"""

from utils.DBConfig import DatabaseConfig
from utils.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utils.GHConfig import GitHubConfig
from utils import GHConfig as GHConfig
from utils.Utils import convert_datetime

def main():
    """Collect commit comments and link them back to stored commit rows."""
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    gh_config = GitHubConfig()
    gh_config.init_github_access()

    # only scan repos that passed the earlier PTM filtering steps
    repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        where=f"""
            {DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.STATIC_ANALYSIS_STATUS} = 1
            AND {DBF.DownstreamRepoInfo.COMMIT_COMMENT_COLLECTION_STATUS} in (0, -1)
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
        print(f"\n===============Processing repository: {repo_name} (ID: {repo_id})")

        # walk through the repo's commit comments page by page
        api_url = GHConfig.COMMIT_COMMENTS_URL.format(repo_name)
        page = 1
        url, headers, params = gh_config.prepare_list_comments(api_url, page)
        response = gh_config.send_request(url, headers=headers, params=params)
        last_page = gh_config.extract_last_page(response)
        print(f"Total pages of commit comments: {last_page}")

        while page <= last_page:
            print(f"\n========Fetching commit comments from page {page}...")
            url, headers, params = gh_config.prepare_list_comments(api_url, page)
            response = gh_config.send_request(url, headers=headers, params=params)

            if response.status_code == 200:
                comments = response.json()
                if not comments:
                    print(f"No commit comments found for {repo_name} on page {page}.")
                    break

                for comment in comments:
                    comment_id = comment.get("id")
                    node_id = comment.get("node_id")
                    url = comment.get("html_url")
                    commit_sha = comment.get("commit_id")
                    created_at = convert_datetime(comment.get("created_at"))
                    author = comment.get("user") or {}
                    created_by_name = author.get("login", "")
                    created_by_id = author.get("id")
                    updated_at = convert_datetime(comment.get("updated_at"))
                    author_association = comment.get("author_association", "")
                    position = comment.get("position")
                    line = comment.get("line")
                    file_path = comment.get("path")
                    body = comment.get("body", "")

                    # Link the GitHub comment back to the local commit row.
                    commit_db_id = db_config.select_from_db(
                        DBT.COMMITS.value,
                        columns=[DBF.Commits.ID],
                        where=f"{DBF.Commits.COMMIT_ID} = %s AND {DBF.Commits.REPO_ID} = %s",
                        params=(commit_sha, repo_id),
                        fetch_one=True
                    )

                    if commit_db_id:
                        commit_db_id = commit_db_id[DBF.Commits.ID]
                    else:
                        print(f"Commit {commit_sha} not found in database for repository {repo_name}. Skipping comment.")
                        continue

                    db_config.insert_to_db(
                        DBT.COMMIT_COMMENTS.value,
                        data_dict={
                            DBF.CommitComments.COMMENT_ID: comment_id,
                            DBF.CommitComments.NODE_ID: node_id,
                            DBF.CommitComments.URL: url,
                            DBF.CommitComments.COMMIT_SHA: commit_sha,
                            DBF.CommitComments.CREATED_AT: created_at,
                            DBF.CommitComments.CREATED_BY_NAME: created_by_name,
                            DBF.CommitComments.CREATED_BY_ID: created_by_id,
                            DBF.CommitComments.UPDATED_AT: updated_at,
                            DBF.CommitComments.AUTHOR_ASSOCIATION: author_association,
                            DBF.CommitComments.POSITION: position,
                            DBF.CommitComments.LINE: line,
                            DBF.CommitComments.FILE_PATH: file_path,
                            DBF.CommitComments.BODY: body,
                            DBF.CommitComments.COMMIT_ID: commit_db_id
                        }
                    )
                    print(f"Inserted commit comment for commit {commit_sha} in repository {repo_name}.\n")

            else:
                print(f"Failed to fetch commit comments for {repo_name} on page {page}. Status code: {response.status_code}")
                collected_status = -1
                break

            page += 1

        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={DBF.DownstreamRepoInfo.COMMIT_COMMENT_COLLECTION_STATUS: collected_status},
            where=f"{DBF.DownstreamRepoInfo.ID} = %s",
            params=(repo_id,)
        )
        print(f"\n\nUpdated commit comment collection status for repository {repo_name} to {collected_status}.")

    print("Commit comment collection completed.")


if __name__ == "__main__":
    main()
