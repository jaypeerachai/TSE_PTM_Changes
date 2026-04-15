"""
RawFileConfig class for managing raw file storage configuration.
"""

import os

class RawFileConfig():

    def __init__(self, db=None):
        self._db = db
        self._folder = "<path_to_raw_files>" # Set this to the desired folder name for raw files

        self._root_path = os.path.join(os.path.dirname(os.path.dirname(os.getcwd())), "<root_directory>", self._folder)
        self.create_folder_if_not_exists(self._root_path)

    @property
    def db(self):
        return self._db
    
    @property
    def folder(self):
        return self._folder
    
    @property
    def root_path(self):
        return self._root_path
    
    def __str__(self):
        return "RawFileConfig = db: {}, folder: {}".format(self._db, self._folder)
    
    def create_folder_if_not_exists(self, path):
        if not os.path.exists(path):
            os.makedirs(path)
            print(f"Created folder: {path} ...")
        else:
            print(f"Folder already exists: {path}")
        return path


    def get_commit_folder(self):
        commit_folder = os.path.join(self._root_path, "commits")
        self.create_folder_if_not_exists(commit_folder)
        return commit_folder
    
    def get_issue_folder(self):
        issue_folder = os.path.join(self._root_path, "issues")
        self.create_folder_if_not_exists(issue_folder)
        return issue_folder

    def get_pr_folder(self):
        pr_folder = os.path.join(self._root_path, "pulls")
        self.create_folder_if_not_exists(pr_folder)
        return pr_folder

    def get_reused_file_folder(self):
        reused_file_folder = os.path.join(self._root_path, "reused_files")
        self.create_folder_if_not_exists(reused_file_folder)
        return reused_file_folder
    
    def get_discussion_folder(self):
        discussion_folder = os.path.join(self._root_path, "discussions")
        self.create_folder_if_not_exists(discussion_folder)
        return discussion_folder
    
    def get_release_folder(self):
        release_folder = os.path.join(self._root_path, "releases")
        self.create_folder_if_not_exists(release_folder)
        return release_folder
    
    def get_tag_folder(self):
        tag_folder = os.path.join(self._root_path, "tags")
        self.create_folder_if_not_exists(tag_folder)
        return tag_folder

    def get_issue_comment_folder(self):
        issue_comment_folder = os.path.join(self._root_path, "issue_comments")
        self.create_folder_if_not_exists(issue_comment_folder)
        return issue_comment_folder
    
    def get_model_info_folder(self):
        model_info_folder = os.path.join(self._root_path, "model_info")
        self.create_folder_if_not_exists(model_info_folder)
        return model_info_folder
    
    def get_branch_folder(self):
        branch_folder = os.path.join(self._root_path, "branches")
        self.create_folder_if_not_exists(branch_folder)
        return branch_folder
    
    def get_repo_folder(self):
        repo_folder = os.path.join(self._root_path, "repos")
        self.create_folder_if_not_exists(repo_folder)
        return repo_folder