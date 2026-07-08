import onboarding

from .app import SpaceTradersApp


def main() -> None:
    # First-run onboarding runs before the Textual app starts, so a plain
    # terminal wizard can prompt for credentials and write .env.
    onboarding.ensure_onboarded()
    SpaceTradersApp().run()


if __name__ == "__main__":
    main()
