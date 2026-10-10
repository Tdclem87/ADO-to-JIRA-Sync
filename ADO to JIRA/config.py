import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from models import AppConfig, RUNTIME


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
LOG_DIR = BASE_DIR / "logs"
STATE_PATH = BASE_DIR / "run_state.json"
VERSION_PATHS = (BASE_DIR / "VERSION", BASE_DIR.parent / "VERSION")
APP_VERSION = next(
    (path.read_text(encoding="utf-8").strip() for path in VERSION_PATHS if path.is_file()),
    "0+unknown",
)
DEFAULT_MAX_ATTACHMENT_SIZE_MB = 100


class SessionLogFilter(logging.Filter):
    def filter(self, record):
        record.session_id = RUNTIME.session_id
        return True


class ConsoleVisibilityFilter(logging.Filter):
    def filter(self, record):
        return getattr(record, "show_console", True)


def update_run_state(status, **updates):
    RUNTIME.run_state.update(
        {
            "status": status,
            "session_id": RUNTIME.session_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **updates,
        }
    )
    temporary_path = STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(RUNTIME.run_state, indent=2), encoding="utf-8"
    )
    temporary_path.replace(STATE_PATH)


def configure_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logger = RUNTIME.logger
    if logger.handlers:
        return

    handler = TimedRotatingFileHandler(
        LOG_DIR / "activity.log",
        when="midnight",
        interval=1,
        backupCount=89,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)s] [session=%(session_id)s] %(message)s"
        )
    )
    handler.addFilter(SessionLogFilter())
    logger.addHandler(handler)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console_handler.addFilter(ConsoleVisibilityFilter())
    logger.addHandler(console_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.info("Logging started.")


def get_required_input(prompt):
    value = input(prompt).strip()
    if not value:
        raise ValueError(f"{prompt.rstrip(': ')} is required.")
    return value


def get_required_environment_variable(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable '{name}' is not set.")
    return value


def load_configuration():
    if not CONFIG_PATH.exists():
        raise RuntimeError(
            f"Configuration file not found: {CONFIG_PATH}. "
            "Copy config.example.json to config.json and update it."
        )

    try:
        configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"Configuration file is not valid JSON: {CONFIG_PATH}"
        ) from error

    try:
        ado_configs = configuration["ado_configs"]
        jira_configuration = configuration["jira"]
        jira_base_url = jira_configuration["base_url"].rstrip("/")
        jira_email = jira_configuration["email"]
        max_attachment_size_mb = configuration.get(
            "max_attachment_size_mb", DEFAULT_MAX_ATTACHMENT_SIZE_MB
        )
        if (
            not isinstance(max_attachment_size_mb, (int, float))
            or max_attachment_size_mb <= 0
        ):
            raise ValueError("max_attachment_size_mb must be greater than 0.")
        max_attachment_bytes = int(max_attachment_size_mb * 1024 * 1024)
    except (KeyError, TypeError, AttributeError) as error:
        raise RuntimeError(
            "Configuration must include ado_configs and jira settings."
        ) from error
    except ValueError as error:
        raise RuntimeError(str(error)) from error

    RUNTIME.config = AppConfig(
        ado_configs=ado_configs,
        jira_base_url=jira_base_url,
        jira_email=jira_email,
        max_attachment_bytes=max_attachment_bytes,
        config_path=CONFIG_PATH,
        log_dir=LOG_DIR,
        state_path=STATE_PATH,
        app_version=APP_VERSION,
    )
    RUNTIME.logger.info(
        "Loaded configuration for %d Azure DevOps organization(s).",
        len(ado_configs),
    )


def view_readme():
    readme_path = BASE_DIR.parent / "README.md"
    if not readme_path.exists():
        print(f"README not found: {readme_path}")
        return

    print("\n" + readme_path.read_text(encoding="utf-8"))
    input("Press Enter to continue...")


def read_jira_issue_key_or_function_key(prompt):
    if os.name != "nt":
        return input(prompt).strip()

    import msvcrt

    print(prompt, end="", flush=True)
    characters = []
    while True:
        character = msvcrt.getwch()
        if character in {"\x00", "\xe0"}:
            scan_code = ord(msvcrt.getwch())
            if scan_code == 59:
                print()
                return "F1"
            if scan_code == 60:
                print()
                return "F2"
            continue
        if character in {"\r", "\n"}:
            print()
            return "".join(characters).strip()
        if character == "\x03":
            raise KeyboardInterrupt
        if character == "\b":
            if characters:
                characters.pop()
                print("\b \b", end="", flush=True)
            continue
        if character.isprintable():
            characters.append(character)
            print(character, end="", flush=True)


def add_organization_configuration():
    print("\nAdd an Azure DevOps organization for Jira issue matching.")
    print("The Jira project-key prefix maps Jira issues to this Azure DevOps organization.")
    while True:
        prefix = get_required_input(
            "Jira project-key prefix (for example NEWORG): "
        ).upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]*", prefix):
            print("Use letters and numbers, starting with a letter.")
            continue
        if any(existing.upper() == prefix for existing in RUNTIME.config.ado_configs):
            print(f"The prefix {prefix} is already configured.")
            continue
        break

    while True:
        organization_url = get_required_input(
            "Azure DevOps organization URL (for example https://dev.azure.com/your-org): "
        ).rstrip("/")
        parsed_url = urlparse(organization_url)
        if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
            break
        print("Enter a valid URL beginning with http:// or https://.")

    configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ado_configs = configuration.setdefault("ado_configs", {})
    if any(existing.upper() == prefix for existing in ado_configs):
        print(f"The prefix {prefix} is already configured.")
        load_configuration()
        return

    ado_configs[prefix] = {"organization_url": organization_url}
    temporary_path = CONFIG_PATH.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps(configuration, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(CONFIG_PATH)
    load_configuration()

    pat_variable = f"AZDO_{prefix}_PAT"
    print(f"Organization {prefix} was added to {CONFIG_PATH}.")
    print(f"Set its Azure DevOps PAT as the Windows environment variable {pat_variable}.")
    if not os.environ.get(pat_variable):
        print("Restart the launcher after setting the PAT so it can read the new variable.")


def get_jira_issue_key():
    while True:
        valid_prefixes = " / ".join(
            sorted(prefix.upper() for prefix in RUNTIME.config.ado_configs.keys())
        )
        value = read_jira_issue_key_or_function_key(
            f"Jira issue key ({valid_prefixes}; F1 README; F2 add organization): "
        )
        if value.upper() == "F1":
            view_readme()
            continue
        if value.upper() == "F2":
            add_organization_configuration()
            continue
        if not value:
            print("A Jira issue key is required.")
            continue
        return value


def launch_windows_environment_variables():
    if os.name != "nt":
        print("Windows environment variables can only be opened on Windows.")
        return

    try:
        subprocess.run(
            ["rundll32.exe", "sysdm.cpl,EditEnvironmentVariables"],
            check=False,
        )
        print("Opened the Windows Environment Variables window.")
    except OSError as error:
        print(f"Could not open the environment variables editor: {error}")


def prompt_for_configuration():
    RUNTIME.logger.info("Initial configuration setup started.")
    print("\nInitial setup is required.")
    print("This will create a local config file and save it next to this script.")
    print("Your PATs and Jira token will stay in Windows environment variables.")

    while True:
        try:
            org_count = int(
                get_required_input(
                    "How many Azure DevOps organizations do you want to configure? "
                )
            )
            if org_count <= 0:
                raise ValueError("Organization count must be at least 1.")
            break
        except ValueError:
            print("Please enter a whole number greater than 0.")

    ado_configs = {}
    for index in range(1, org_count + 1):
        while True:
            prefix = get_required_input(
                f"Organization {index} Jira prefix (for example MBSD): "
            ).strip().upper()
            if prefix:
                break
        while True:
            organization_url = get_required_input(
                f"Organization {index} Azure DevOps URL (for example https://dev.azure.com/your-org): "
            )
            if organization_url:
                break
        ado_configs[prefix] = {"organization_url": organization_url}

    jira_base_url = get_required_input(
        "Jira base URL (for example https://your-company.atlassian.net): "
    )
    jira_email = get_required_input("Jira email address: ")

    configuration = {
        "ado_configs": ado_configs,
        "jira": {"base_url": jira_base_url, "email": jira_email},
    }
    CONFIG_PATH.write_text(json.dumps(configuration, indent=2), encoding="utf-8")
    RUNTIME.logger.info("Initial configuration saved.")
    print(f"Configuration saved to: {CONFIG_PATH}")


def ensure_environment_variables():
    required_vars = {"JIRA_API_TOKEN": "Jira API token"}
    for prefix in RUNTIME.config.ado_configs:
        required_vars[f"AZDO_{prefix.upper()}_PAT"] = f"{prefix} Azure DevOps PAT"

    missing = [name for name in required_vars if not os.environ.get(name)]
    if not missing:
        RUNTIME.logger.info("All required environment variables are available.")
        return

    RUNTIME.logger.warning("Missing %d required environment variable(s).", len(missing))
    print("\nMissing environment variables:")
    for name in missing:
        print(f"- {name} ({required_vars[name]})")

    print("\nCreate the required tokens if you do not already have them:")
    print("Jira API token:")
    print("1. Open https://id.atlassian.com/manage-profile/security/api-tokens")
    print("2. Select Create API token, enter a label, and create the token.")
    print("3. Copy it immediately; Atlassian will not show it again.")
    print("4. Store it as JIRA_API_TOKEN in Windows environment variables.")
    print("Azure DevOps PAT, once for each organization:")
    print("1. Sign in to the Azure DevOps organization.")
    print("2. Select your profile icon, then User settings > Personal access tokens.")
    print("3. Select New Token, enter a name and expiration date.")
    print("4. Choose the organization and use the minimum Work Items read access needed by this tool.")
    print("5. Create the PAT and copy it immediately; Azure DevOps will not show it again.")
    print("6. Store it as AZDO_<PREFIX>_PAT, using each configured prefix.")

    print("\nAdd the missing values in Windows:")
    print("1. Press the Windows key and search for 'environment variables'.")
    print("2. Open 'Edit the system environment variables'.")
    print("3. Select 'Environment Variables'.")
    print("4. Under User variables, select 'New'.")
    print("5. Enter each missing variable name exactly as listed above.")
    print("6. Enter the matching Jira API token or Azure DevOps PAT as its value.")
    print("7. Select OK on each window to save the changes.")
    print("8. Close and reopen PowerShell, Command Prompt, or VS Code.")

    response = input(
        "Open Windows Environment Variables now to add them? (y/n): "
    ).strip().lower()
    if response in {"y", "yes"}:
        launch_windows_environment_variables()

    input("After you add the missing values, press Enter to continue...")


def ensure_setup():
    if not CONFIG_PATH.exists():
        RUNTIME.logger.info("Configuration file is missing; starting guided setup.")
        prompt_for_configuration()
        load_configuration()
        return

    try:
        load_configuration()
    except RuntimeError as error:
        RUNTIME.logger.error("Configuration check failed: %s", error)
        print(f"Configuration check failed: {error}")
        prompt_for_configuration()
        load_configuration()