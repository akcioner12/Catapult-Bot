"""
Разовый локальный скрипт: получает YOUTUBE_REFRESH_TOKEN через OAuth-флоу в браузере.
Запускать один раз локально (НЕ в Railway).
Перед запуском: pip install google-auth-oauthlib google-api-python-client
"""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]

CLIENT_ID = input("YOUTUBE_CLIENT_ID: ").strip()
CLIENT_SECRET = input("YOUTUBE_CLIENT_SECRET: ").strip()

flow = InstalledAppFlow.from_client_config(
    {
        "installed": {
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    },
    scopes=SCOPES,
)
creds = flow.run_local_server(port=0)
print("\nYOUTUBE_REFRESH_TOKEN=" + creds.refresh_token)
