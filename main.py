"""
main.py
-------
Single entry point for the full Iron2Tech file pipeline. This is what
gets bundled into the .exe.

    1. Scan a repository        -> scanner_v5.py
    2. Export a scan to Excel   -> report_reader.py
    3. Open a file (search)     -> opener.py
    4. Ask the agent            -> agent.py

Each option just hands control to that module's existing interactive
flow (nothing was rewritten), then drops you back to this menu when
it finishes. Run it as a normal script during development:

    python main.py

Later, PyInstaller turns this exact file into IronTechPipeline.exe.
"""

import sys

import scanner_v5
import report_reader
import opener
import agent


def run_scanner():
    folder = input("Enter repository path: ").strip()
    scanner_v5.scan_repository(folder)


def run_reader():
    report = report_reader.load_reports()
    if report is None:
        return
    data = report_reader.load_json(report)
    report_reader.menu(data)


def run_opener():
    opener.main()


def run_agent():
    agent.main()


MENU_ACTIONS = {
    "1": ("Scan a repository", run_scanner),
    "2": ("Export a scan to Excel", run_reader),
    "3": ("Open a file (search)", run_opener),
    "4": ("Ask the agent (natural language)", run_agent),
}


def main():
    while True:
        print("\n=== IRON2TECH File Pipeline ===")
        for key, (label, _) in MENU_ACTIONS.items():
            print(f"{key}. {label}")
        print("0. Exit")

        choice = input("Choose: ").strip()

        if choice == "0":
            print("Goodbye.")
            break

        action = MENU_ACTIONS.get(choice)
        if action is None:
            print("Invalid choice.")
            continue

        _, func = action
        try:
            func()
        except KeyboardInterrupt:
            print("\nCancelled.")
        except Exception as e:
            print(f"Something went wrong: {e}")


if __name__ == "__main__":
    main()
