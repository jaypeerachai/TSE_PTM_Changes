"""
HuggingFaceConfig class for managing Hugging Face API access and requests.
"""
import json
import requests
from requests.structures import CaseInsensitiveDict
from utilities import Utils

CONFIG_PATH = "config.json"

class HuggingFaceConfig():

    def __init__(self, token=None):
        self._token = token
    
    @property
    def token(self):
        return self._token
    
    def init_huggingface_access(self):
        with open(CONFIG_PATH) as json_file:
            config = json.load(json_file)
        github_config = config["hf"]
        github_token = github_config["api"]
        token_choice = input("\nPlease select a token to use for HF: ")
        self._token = github_token[int(token_choice)]
        
    def prepare_info_request(self):
        headers = CaseInsensitiveDict()
        headers["Authorization"] = "Bearer {}".format(self._token)
        return headers
    
    def send_request(self, url, headers=None):
        while True:
            print("\nsend request to {} ...".format(url))
            try:
                resp = requests.get(url, headers=headers, timeout=30)
                break
            except:
                print("\nrequest failed, retrying ...")
        return resp
        