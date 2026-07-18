"""ai_browser - Automated web browsing for bug bounty reconnaissance.

This module automates web browsing against an authorized target hostname,
proxying all traffic through Burp Suite so that the aiScraper service can
poll and normalize captured traffic from Burp's proxy history.
"""

__version__ = "0.1.0"
