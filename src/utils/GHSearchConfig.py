"""
GitHub code-search utility loads GitHub API tokens from the config, prepares search
request headers, and handles token switching and backoff when code-search rate
limits are hit.
"""

import json
import time
import requests
from requests.structures import CaseInsensitiveDict

# Path to JSON config file holding GitHub tokens
CONFIG_PATH = "config.json"

# api url for code search
CODE_SEARCH_URL = "https://api.github.com/search/code"

# Default rate-limit and pagination settings
RESULTS_PER_PAGE = 100
MAX_PAGES = 10
MAX_RETRIES = 5
BACKOFF_BASE = 6    # seconds (6 * 10 pages = 60 seconds)
BACKOFF_MAX = 3600   # seconds 

class GitHubSearchConfig:
    def __init__(self, config_path=CONFIG_PATH):
        self.config_path = config_path
        self._tokens = []
        self._token_choice = None
        self._token = None
        self.headers = CaseInsensitiveDict({
            "Accept": "application/vnd.github.text-match+json"
        })

    def init_github_access(self):
        with open(self.config_path) as f:
            config = json.load(f).get("gh", {})
        self._tokens = config.get("api", [])
        if not self._tokens:
            raise ValueError("No GitHub tokens found in config")

        print("[GHSearchConfig] Available GitHub tokens:")
        for idx, _ in enumerate(self._tokens):
            print(f"[{idx}] Token {idx + 1}")

        choice = None
        while choice is None:
            try:
                sel = int(input("Select token index: "))
                if 0 <= sel < len(self._tokens):
                    choice = sel
                else:
                    print("[GHSearchConfig] Invalid choice, try again.")
            except (ValueError, KeyboardInterrupt):
                print("[GHSearchConfig] Invalid input, please enter a number.")

        self._token_choice = choice
        self._token = self._tokens[choice]
        self._build_headers()

    def switch_token(self, token_choice):
        if not self._tokens:
            self.init_github_access()

        if 0 <= token_choice < len(self._tokens):
            self._token_choice = token_choice
            self._token = self._tokens[token_choice]
            self._build_headers()
            time.sleep(1)  # small delay to avoid hitting rate limits immediately
            print(f"[GHSearchConfig] Switched to token {self._token_choice + 1}")
        else:
            raise IndexError("Token choice out of range")

    def _build_headers(self):
        self.headers["Authorization"] = f"token {self._token}"

    def send_code_search_request(self, query, page):
        """
        https://docs.github.com/en/rest/search/search?apiVersion=2022-11-28#search-code
        https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting
        """

        initial_choice = self._token_choice
        params = {
            "q": query,
            "sort": "indexed",
            "order": "desc",
            "per_page": RESULTS_PER_PAGE,
            "page": page,
        }

        for attempt in range(1, MAX_RETRIES + 1):
            response = requests.get(
                url = CODE_SEARCH_URL,
                headers = self.headers,
                params = params
            )

            # normal case
            if response.status_code not in (403, 429):
                response.raise_for_status()
                return response.json()

            # ─── swap within the adjacent pair ───────────────────────────────
            if len(self._tokens) > 1:
                idx = self._token_choice
                # compute partner: even→odd, odd→even (0→1, 1→0)
                pair_idx = idx ^ 1
                # only swap if partner exists
                if pair_idx < len(self._tokens):
                    self.switch_token(pair_idx)
                    # if we haven’t yet returned to the original token, retry immediately
                    if self._token_choice != initial_choice:
                        continue
            # ─────────────────────────────────────────────────────────────────
            
            # if we are here, we have exhausted all tokens
            print(f"[GHSearchConfig] ❗️ Both tokens exhausted, waiting for rate limit reset.")
            retry_after = response.headers.get("Retry-After")
            remaining = response.headers.get("X-RateLimit-Remaining")
            reset_ts = response.headers.get("X-RateLimit-Reset")

            if retry_after:
                wait = int(retry_after)
                print(f"[GHSearchConfig] ⏳ Secondary rate-limit, sleeping {wait}s.")
            elif remaining == "0" and reset_ts:
                reset_time = int(reset_ts)
                now = int(time.time())
                wait = max(reset_time - now, 0) + 5
                print(f"[GHSearchConfig] ⏳ Primary rate-limit exhausted, sleeping {wait}s until reset.")
            else:
                backoff = BACKOFF_BASE * 2 ** (attempt - 1)
                wait = min(backoff, BACKOFF_MAX)
                print(f"[GHSearchConfig] ⏳ Secondary rate-limit backoff {wait}s (attempt {attempt}/{MAX_RETRIES}).")

            # if both tokens are exhausted, time to wait
            time.sleep(wait)

        raise RuntimeError(f"Exceeded {MAX_RETRIES} retries for query={query!r} page={page}")


    def get_total_count(self, query):
        data = self.send_code_search_request(query, page=1)
        count = data.get("total_count", 0)
        print(f"[GHSearchConfig] 🔎 Estimated total count: {count}")
        return count

    def fetch_all_pages(self, query):
        items = []
        for page in range(1, MAX_PAGES + 1):
            print(f"[GHSearchConfig] ➜ Fetching page {page}")
            data = self.send_code_search_request(query, page)
            batch = data.get("items", [])
            items.extend(batch)
            if len(batch) < RESULTS_PER_PAGE:
                break
            time.sleep(BACKOFF_BASE)  # small pause between pages
        return items

    def code_search_items(self, import_sig, use_sig,
                          size_min=0, size_max=384_000):
        # max file size is 384 KB
        def _slice_and_yield(low, high):
            # base case for recursion
            if low > high:
                return
            q = f'"{import_sig}" "{use_sig}" in:file language:Python size:{low}..{high} NOT is:fork'
            print(f"\n[GHSearchConfig] Query: size:{low}..{high}")
            count = self.get_total_count(q)

            if count == 0:
                print("[GHSearchConfig] ❌ No results in this range.")
                return
            # if results are less than 1000, fetch all results
            if count <= 1000:
                print(f"[GHSearchConfig] ✅ Fetching {count} results.")
                for item in self.fetch_all_pages(q):
                    yield item
            else: # recursive calls
                print(f"[GHSearchConfig] ⚠️ Too many results ({count}), splitting…")
                mid = (low + high) // 2
                yield from _slice_and_yield(low, mid)
                yield from _slice_and_yield(mid + 1, high)

        yield from _slice_and_yield(size_min, size_max)


        