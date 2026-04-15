"""
This script performs a preliminary filter on the downstream repositories in the database.
The filtering criteria include:
- availability of repository information (INFO_COLLECTION_STATUS = 1)
- size > 0
- stargazers_count >= 5 or forks_count >= 5
- language is Python
- pushed_at >= '2025-01-01'
- fork = 0
- excluding repos with certain keywords in topics, description, or name
"""

from utilities.DBConfig import DatabaseConfig
from utilities.DBSchema import DBTableNames as DBT, DBFieldNames as DBF
from utilities import GHConfig as GHConfig
import pandas as pd

if __name__ == "__main__":
    db_config = DatabaseConfig()
    db_config.create_db_connection()

    # select all repositories
    downstream_repos = db_config.select_from_db(
        DBT.DOWNSTREAM_REPO_INFO.value,
        columns="*",
        params=None,
        order_by=f"{DBF.DownstreamRepoInfo.ID} ASC"
    )

    print(f"Found {len(downstream_repos)} downstream repositories in the database.")
    print("\nStarting preliminary filter for repositories...")

    # Create a DataFrame from the list of dictionaries
    df_repos = pd.DataFrame(downstream_repos)
    print(df_repos.head())

    # info collection_status = 1
    df_repos = df_repos[df_repos[DBF.DownstreamRepoInfo.INFO_COLLECTION_STATUS] == 1]
    print(f"Filtered repositories with INFO_COLLECTION_STATUS = 1: {len(df_repos)}")

    # fork = 0
    df_repos = df_repos[df_repos[DBF.DownstreamRepoInfo.FORK] == 0]
    print(f"Filtered repositories with FORK = 0: {len(df_repos)}")
    
    # size > 0
    df_repos = df_repos[df_repos[DBF.DownstreamRepoInfo.SIZE] > 0]
    print(f"Filtered repositories with SIZE > 0: {len(df_repos)}")

    # stargazers_count >= 5 or forks_count >= 5
    df_repos = df_repos[(df_repos[DBF.DownstreamRepoInfo.STARGAZERS_COUNT] >= 5) | 
                        (df_repos[DBF.DownstreamRepoInfo.FORKS_COUNT] >= 5)]
    print(f"Filtered repositories with STARGAZERS_COUNT >= 5 or FORKS_COUNT >= 5: {len(df_repos)}")

    # language is Python
    df_repos = df_repos[df_repos[DBF.DownstreamRepoInfo.LANGUAGE] == 'Python']
    print(f"Filtered repositories with LANGUAGE = 'Python': {len(df_repos)}")

    # pushed_at >= '2025-01-01'
    df_repos = df_repos[df_repos[DBF.DownstreamRepoInfo.PUSHED_AT] >= '2025-01-01']
    print(f"Filtered repositories with PUSHED_AT >= '2025-01-01': {len(df_repos)}")

    # filter by keywords in topics
    EXCLUDING_TOPICS = [
        'awesome-list',
        'book',
        'course',
        'coursera',
        'example',
        'examples',
        'framework',
        'handson',
        'hands-on',
        'implementation',
        'interview',
        'interview-practice',
        'interview-prep',
        'interview-preparation',
        'interview-questions',
        'interviews',
        'library',
        'paper',
        'roadmap',
        'sample',
        'samples',
        'state-of-the-art',
        'study-plan',
        'survey',
        'tool',
        'toolkit',
        'tutorial',
        'tutorial-code',
        'tutorials'
    ]

    EXCLUDING_SUBSTRINGS_FOR_DESCRIPTION = [
        'code for',
        'code of ',
        'experiment of',
        'implementation of',
        'model for',
        'state of the art',
        'state-of-the-art',
        'hand on',
        'collection of',
        'example'
    ]

    EXCLUDING_KEYWORDS_FOR_NAME = [
        'assignment',
        'book',
        'booklet',
        'cheatsheet',
        'cheatsheets',
        'cookbook',
        'course',
        'courses',
        'curriculum',
        'demo',
        'example',
        'examples',
        'exercise',
        'framework',
        'guide',
        'handson',
        'homework',
        'interview',
        'lecture',
        'library',
        'material',
        'note',
        'package',
        'paper',
        'practice',
        'presentation',
        'publication',
        'publications',
        'research',
        'resource',
        'roadmap',
        'sample',
        'stateoftheart',
        'study',
        'textbook',
        'tool',
        'toolkit',
        'tutorial',
        'tutorials',
        'workshop',
        'fork',
        'models',
        'dataset',
        'datasets',
        'notebooks'
    ]

    # filter out repositories with topics containing any of the excluding topics
    df_repos = df_repos[~df_repos[DBF.DownstreamRepoInfo.TOPICS].str.contains(
        '|'.join(EXCLUDING_TOPICS), case=False, na=False)]
    print(f"Filtered repositories excluding topics: {len(df_repos)}")

    # filter out repositories with description containing any of the excluding substrings
    df_repos = df_repos[~df_repos[DBF.DownstreamRepoInfo.DESCRIPTION].str.contains(
        '|'.join(EXCLUDING_SUBSTRINGS_FOR_DESCRIPTION), case=False, na=False)]
    print(f"Filtered repositories excluding description substrings: {len(df_repos)}")

    # filter out repositories with name containing any of the excluding keywords
    df_repos['Name'] = df_repos[DBF.DownstreamRepoInfo.FULL_NAME].str.split('/').str[1]
    df_repos = df_repos[~df_repos['Name'].str.contains(
        '|'.join(EXCLUDING_KEYWORDS_FOR_NAME), case=False, na=False)]
    print(f"Filtered repositories excluding name keywords: {len(df_repos)}")

    # update the preliminary filter status in the database
    id_list = df_repos[DBF.DownstreamRepoInfo.ID].tolist()
    if id_list:
        db_config.update_db(
            DBT.DOWNSTREAM_REPO_INFO.value,
            data_dict={DBF.DownstreamRepoInfo.PRELIMINARY_FILTER_STATUS: 1},
            where=f"{DBF.DownstreamRepoInfo.ID} IN %s",
            params=(tuple(id_list),)
        )
        print(f"Updated preliminary filter status for {len(id_list)} repositories.")
    else:
        print("No repositories passed the preliminary filter criteria.")

