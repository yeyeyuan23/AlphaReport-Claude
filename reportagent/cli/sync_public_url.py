"""Synchronize the URL used in report links from the local tunnel log."""
import argparse


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    from reportagent.services.public_url import sync_public_url
    if sync_public_url():
        print("[sync_url] Public URL synchronized")
    else:
        print("[sync_url] No configured tunnel URL or .env")


if __name__ == "__main__":
    main()
