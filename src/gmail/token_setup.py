import argparse
import json
from pathlib import Path

from src.gmail.client import GmailClient


def load_client_config(path: Path) -> dict:
    config = json.loads(path.read_text())
    if "installed" not in config:
        raise ValueError("Expected a 'Desktop app' OAuth client file (top-level key 'installed')")
    return config


def format_env_lines(client_config: dict, refresh_token: str) -> str:
    installed = client_config["installed"]
    return "\n".join(
        [
            f"GOOGLE_CLIENT_ID={installed['client_id']}",
            f"GOOGLE_CLIENT_SECRET={installed['client_secret']}",
            f"GMAIL_REFRESH_TOKEN={refresh_token}",
        ]
    )


def obtain_refresh_token(client_config: dict, port: int = 0) -> str:
    # Imported lazily: only needed on the workstation that has a browser, not in the runtime image.
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(client_config, scopes=GmailClient.SCOPES)
    # prompt=consent forces Google to return a refresh token even if the app was already authorized.
    creds = flow.run_local_server(port=port, access_type="offline", prompt="consent")
    if not creds.refresh_token:
        raise RuntimeError("Google did not return a refresh token; revoke the app access and retry")
    return creds.refresh_token


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Generate the Gmail refresh token for the .env file")
    parser.add_argument("client_secret_file", type=Path, help="OAuth client JSON downloaded from Google Cloud")
    parser.add_argument("--port", type=int, default=0, help="Local callback port (0 = random free port)")
    args = parser.parse_args(argv)

    client_config = load_client_config(args.client_secret_file)
    refresh_token = obtain_refresh_token(client_config, args.port)
    print("\nAdd these lines to the .env of the server:\n")
    print(format_env_lines(client_config, refresh_token))


if __name__ == "__main__":
    main()
