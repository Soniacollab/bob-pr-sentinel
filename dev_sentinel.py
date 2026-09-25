import subprocess


def run_git_command(command: list[str]) -> str:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=True,
    )

    return result.stdout.strip()


def get_changed_files() -> list[str]:
    output = run_git_command(
        ["git", "diff", "--name-only"]
    )

    if not output:
        return []

    return output.splitlines()


def get_git_diff() -> str:
    return run_git_command(
        ["git", "diff"]
    )


def main():
    print("🛡️ Dev Sentinel")
    print("━━━━━━━━━━━━━━━━━━")

    changed_files = get_changed_files()

    if not changed_files:
        print("No local changes detected.")
        return

    print("Changes detected:")
    for file in changed_files:
        print(f"  • {file}")

    print("\nDiff:")
    print("━━━━━━━━━━━━━━━━━━")
    print(get_git_diff())


if __name__ == "__main__":
    main()