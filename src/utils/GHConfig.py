"""
GitHubConfig class for managing GitHub API access and requests.
- Handles multiple tokens and rate limit management.
- Provides methods to prepare requests for various GitHub API endpoints.
"""

import time
import json
import time
import requests
from requests.structures import CaseInsensitiveDict
import Utils

CONFIG_PATH = "config.json"
REPO_URL = "https://api.github.com/repos/{}" # {} = {}/{} --> full name
LIST_ISSUES_URL = "https://api.github.com/repos/{}/issues"
ISSUE_COUNT_URL = "https://api.github.com/repos/{}/issues?state=all&per_page=1"
ISSUE_INFO_URL = "https://api.github.com/repos/{}/issues/{}"
ISSUE_COMMENTS_URL = "https://api.github.com/repos/{}/issues/{}/comments"
LIST_PULLS_URL = "https://api.github.com/repos/{}/pulls"
PULL_COUNT_URL = "https://api.github.com/repos/{}/pulls?state=all&per_page=1"
PULL_INFO_URL = "https://api.github.com/repos/{}/pulls/{}"
PULL_COMMITS_URL = "https://api.github.com/repos/{}/pulls/{}/commits"
LIST_COMMITS_URL = "https://api.github.com/repos/{}/commits"
COMMIT_COUNT_URL = "https://api.github.com/repos/{}/commits?per_page=1"
COMMIT_INFO_URL = "https://api.github.com/repos/{}/commits/{}"
COMMIT_COMMENTS_URL = "https://api.github.com/repos/{}/comments"
LIST_DISCUSSIONS_URL = "https://api.github.com/repos/{}/discussions"
DISCUSSIONS_COUNT_URL = "https://api.github.com/repos/{}/discussions?per_page=1"
DISCUSSIONS_INFO_URL = "https://api.github.com/repos/{}/discussions/{}"
DISCUSSIONS_COMMENTS_URL = "https://api.github.com/repos/{}/discussions/{}/comments"
LIST_RELEASES_URL = "https://api.github.com/repos/{}/releases"
RELEASES_COUNT_URL = "https://api.github.com/repos/{}/releases?per_page=1"
RELEASES_INFO_URL = "https://api.github.com/repos/{}/releases/{}"
LIST_TAGS_URL = "https://api.github.com/repos/{}/tags"
CODE_SEARCH_URL = "https://api.github.com/search/code"
COMMIT_COMPARE_URL = "https://api.github.com/repos/{}/compare/{}...{}"
FILE_INFO_URL = "https://api.github.com/repos/{}/contents/{}"
LIST_BRANCHES_URL = "https://api.github.com/repos/{}/branches"

class GitHubConfig():
    """
    GitHubConfig class for managing GitHub API access and requests.
    """

    def __init__(self, config_path=CONFIG_PATH):
        self.config_path = config_path
        self._tokens = []
        self._token_choice = None
        self._token = None
    
    @property
    def token(self):
        return self._token
    
    def init_github_access(self):
        with open(self.config_path) as f:
            config = json.load(f).get("gh", {})
        self._tokens = config.get("api", [])
        if not self._tokens:
            raise ValueError("No GitHub tokens found in config")

        print("[GHConfig] Available GitHub tokens:")
        for idx, _ in enumerate(self._tokens):
            print(f"[{idx}] Token {idx + 1}")

        choice = None
        while choice is None:
            try:
                sel = int(input("Select token index: "))
                if 0 <= sel < len(self._tokens):
                    choice = sel
                else:
                    print("[GHConfig] Invalid choice, try again.")
            except (ValueError, KeyboardInterrupt):
                print("[GHConfig] Invalid input, please enter a number.")

        self._token_choice = choice
        self._token = self._tokens[choice]

    def switch_token(self, token_choice):
        if not self._tokens:
            self.init_github_access()

        if 0 <= token_choice < len(self._tokens):
            self._token_choice = token_choice
            self._token = self._tokens[token_choice]
            time.sleep(1) 
            print("[GHConfig] Switched to token index: {}".format(token_choice))
        else:
            raise IndexError("Token choice out of range")


    def get_last_page_for_count(self, resp):
        link = resp.headers.get("Link")
        if link:
            for part in link.split(","):
                if "rel=\"last\"" in part:
                    return int(part.split("&page=")[1].split(">")[0])
        return 1


    def extract_last_page(self, resp):
        # <https://api.github.com/repositories/408150234/issues?state=all&sort=created&per_page=20&page=2>; rel="next", <https://api.github.com/repositories/408150234/issues?state=all&sort=created&per_page=20&page=3>; rel="last"
        try:
            link = resp.headers["link"]
            last_page = link.split(",")[1].split(";")[0].split("&page=")[1].split(">")[0]
            print("last page: {}".format(last_page))
            return int(last_page)
        except:
            return 1
        
    def prepare_list_issues(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "state": "all",
            "sort": "created",
            "per_page": "100",
            "page": str(page)
        }
        return url, headers, params
    
    def prepare_info_request(self):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        return headers
        
    def prepare_list_commits(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "per_page": "100",
            "page": str(page)
        }
        return url, headers, params
    
    def prepare_list_commits_touched(self, url, branch, path, util_iso, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["User-Agent"] = "commit-follow-script"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "sha": branch,
            "path": path,
            "per_page": "100",
            "page": str(page),
        }
        if util_iso:
            params["until"] = util_iso
        return url, headers, params


    def prepare_list_commit_files(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "per_page": "300",
            "page": str(page)
        }
        return url, headers, params
    
    def prepare_list_comments(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "sort": "created",
            "direction": "asc",
            "per_page": "100",
            "page": str(page)
        }
        return url, headers, params

    
    def prepare_list_discussions(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "direction": "asc",
            "per_page": "100",
            "page": str(page)
        }
        return url, headers, params

    def prepare_list_releases(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "per_page": "100",
            "page": str(page)
        }
        return url, headers, params
    
    def prepare_list_branches(self, url, page):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "per_page": "100",
            "page": str(page)
        }
        return url, headers, params
    
    def prepare_commit_compare(self, url):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "per_page": "1"
        }
        return url, headers, params
    
    def prepare_file_info(self, url, ref):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "ref": ref
        }
        return url, headers, params

    def prepare_tag_commit_file(self, url, tag_name, path):
        headers = CaseInsensitiveDict()
        headers["Accept"] = "application/vnd.github.v3+json"
        headers["Authorization"] = "token {}".format(self._token)
        params = {
            "sha": tag_name,
            "path": path,
            "per_page": "1"
        }
        return url, headers, params


    def check_req_limit_remain(self, resp=None):
        if not resp:
            url = "https://api.github.com/"
            headers = self.prepare_info_request()
            resp = requests.get(url, headers=headers, timeout=30)

        limit_remain = resp.headers.get("X-RateLimit-Remaining")
        reset_ts = resp.headers.get("X-RateLimit-Reset")
        retry_after = resp.headers.get("Retry-After")

        print("[GHConfig] Rate limit remaining: {}".format(limit_remain))

        if int(limit_remain) < 3 and len(self._tokens) > 1:
            print("[GHConfig] Rate limit is low, switching token ...")

            initial_choice = self._token_choice

            while True:
                next_choice = (self._token_choice + 1) % len(self._tokens)
                self.switch_token(next_choice)

                # If we cycled back to the starting token, all tokens are exhausted.
                if self._token_choice == initial_choice:
                    print("[GHConfig] All tokens exhausted, waiting for rate limit reset...")
                    if reset_ts:
                        reset_ts = int(reset_ts)
                        now = int(time.time())
                        wait_time = max(0, reset_ts - now + 5)  # Add a small buffer
                        print("[GHConfig] Sleeping for {} seconds until rate limit resets.".format(wait_time))
                        time.sleep(wait_time)
                    else:
                        # Fallback if no reset timestamp is present
                        default_wait = int(retry_after) if retry_after else 60
                        print("[GHConfig] No reset timestamp found, sleeping for {} seconds.".format(default_wait))
                        time.sleep(default_wait)
                    break

                # Otherwise, re-check the limit with the new token
                url = "https://api.github.com/"
                headers = self.prepare_info_request()
                resp = requests.get(url, headers=headers, timeout=30)
                limit_remain = resp.headers.get("X-RateLimit-Remaining")
                reset_ts = resp.headers.get("X-RateLimit-Reset")
                retry_after = resp.headers.get("Retry-After")

                print("[GHConfig] Rate limit remaining (new token): {}".format(limit_remain))

                if int(limit_remain) > 1:
                    break

    def send_request(self, url, headers=None, params=None, patch=None):
        while True:
            print("\n[GHConfig] Sending request to: {}".format(url))
            try:
                resp = requests.get(url, headers=headers, params=params, timeout=30)

                if headers or params:
                    self.check_req_limit_remain(resp)
                break
            except:
                print("\n[GHConfig] Request failed, retrying in 60 seconds...")
                if Utils.is_connected_to_internet():
                    continue
                time.sleep(60)

        return resp