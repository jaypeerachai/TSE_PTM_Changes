"""
General Utility functions
"""

import urllib.request
from datetime import datetime
from dateutil import parser

def convert_datetime(iso_datetime):
    """
    Convert ISO 8601 datetime string to "YYYY-MM-DD HH:MM:SS" format.
    """
    if not iso_datetime:
        return None
    try:
        dt = parser.isoparse(iso_datetime)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None

def is_connected_to_internet():
    """
    Check if the machine is connected to the internet by trying to access a known URL.
    """
    try:
        urllib.request.urlopen('http://google.com')
        print("\nconnected to internet")
        return True
    except:
        print("\nnot connected to internet")
        return False

def serialize(obj):
    """Recursively convert object into JSON-serializable format."""
    if isinstance(obj, dict):
        return {k: serialize(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [serialize(i) for i in obj]
    elif isinstance(obj, tuple):
        return tuple(serialize(i) for i in obj)
    elif isinstance(obj, datetime):
        return obj.isoformat()
    elif hasattr(obj, "__dict__"):
        return serialize(vars(obj))
    else:
        return obj