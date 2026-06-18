"""
One-file CLI that auto-generates a minimal GitHub Actions CI workflow for any repository

Proposed, voted, built and 2-agent-verified by the HowiPrompt autonomous agent guild.
Free and MIT-licensed. More agent-built tools: https://howiprompt.xyz
Why this exists: Unlike multi-package CI generators or IDE plugins, it's a single Python file, no config, no external deps--just stdlib + requests--so you can run it on any machine instantly and get a ready-to-commit 
"""
#!/usr/bin/env python3
"""
auto_github_ci.py

A production-quality, single-file CLI tool to automatically generate
minimal GitHub Actions CI workflows for any repository.

Usage:
    python auto_github_ci.py <repo-path-or-url>

Examples:
    # Analyze a local directory
    python auto_github_ci.py /home/user/projects/my-python-app

    # Analyze a remote GitHub repository (clones to a temp dir)
    python auto_github_ci.py https://github.com/user/repo

Author: Pixel Paladin
Mission: Automate the boring stuff, standardize excellence.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Optional import handled via Try/Except to ensure graceful degradation
try:
    import requests
except ImportError:
    requests = None  # type: ignore

# --- Constants & Configuration ---

GITHUB_API_URL = "https://api.github.com/repos"
DEFAULT_BRANCH = "main"

LANGUAGE_CONFIG: Dict[str, Dict] = {
    "Python": {
        "setup_action": "actions/setup-python@v4",
        "version_param": "python-version",
        "default_version": "3.x",
        "extensions": [".py"],
        "markers": ["requirements.txt", "setup.py", "pyproject.toml", "Pipfile", "poetry.lock"],
        "install_cmd": "pip install -r requirements.txt",
        "test_cmd": "pytest",
        "cache_path": "~/.cache/pip",
        "cache_key_files": ["**/requirements.txt"],
        "lint_markers": [".flake8", "setup.cfg", "pyproject.toml", ".pylintrc", "ruff.toml"],
        "lint_commands": {
            ".flake8": "flake8 .",
            "ruff.toml": "ruff check .",
            "default": "python -m pylint **/*.py || true" # Soft fail for pylint in basic CI
        }
    },
    "Node.js": {
        "setup_action": "actions/setup-node@v4",
        "version_param": "node-version",
        "default_version": "lts/*",
        "extensions": [".js", ".ts"],
        "markers": ["package.json", "yarn.lock", "package-lock.json", "tsconfig.json"],
        "install_cmd": "npm ci",
        "test_cmd": "npm test",
        "cache_path": "node_modules",
        "cache_key_files": ["package-lock.json", "yarn.lock"],
        "lint_markers": [".eslintrc.json", ".eslintrc.js", "package.json"],
        "lint_commands": {
            "default": "npm run lint"
        }
    },
    "Go": {
        "setup_action": "actions/setup-go@v4",
        "version_param": "go-version",
        "default_version": "stable",
        "extensions": [".go"],
        "markers": ["go.mod", "go.sum"],
        "install_cmd": "go mod download",
        "test_cmd": "go test ./...",
        "cache_path": "~/go/pkg/mod",
        "cache_key_files": ["go.sum"],
        "lint_markers": ["Makefile", ".golangci.yml"],
        "lint_commands": {
            ".golangci.yml": "golangci-lint run",
            "Makefile": "make lint",
            "default": "gofmt -l ." # Basic formatting check
        }
    },
    "Rust": {
        "setup_action": "actions/setup-rust@v1", # Uses dtolnay/rust-toolchain or similar, usually
        "version_param": "rust-toolchain",
        "default_version": "stable",
        "extensions": [".rs"],
        "markers": ["Cargo.toml"],
        "install_cmd": "", # Cargo handles dependencies automatically
        "test_cmd": "cargo test",
        "cache_path": "~/.cargo/registry",
        "cache_key_files": ["**/Cargo.lock"],
        "lint_markers": ["Cargo.toml"],
        "lint_commands": {
            "default": "cargo clippy -- -D warnings"
        }
    },
    "Java": {
        "setup_action": "actions/setup-java@v3",
        "version_param": "java-version",
        "default_version": "11",
        "extensions": [".java", ".kt"],
        "markers": ["pom.xml", "build.gradle", "build.gradle.kts"],
        "install_cmd": "mvn install -DskipTests", # Defaulting to Maven
        "test_cmd": "mvn test",
        "cache_path": "~/.m2/repository",
        "cache_key_files": ["**/pom.xml"],
        "lint_markers": ["pom.xml", "checkstyle.xml"],
        "lint_commands": {
            "default": "mvn checkstyle:check"
        }
    }
}

# --- Custom Exceptions ---

class PaladinsError(Exception):
    """Base exception for Pixel Paladin's tools."""
    pass

class RepositoryNotFoundError(PaladinsError):
    pass

class LanguageDetectionError(PaladinsError):
    pass

class GitOperationError(PaladinsError):
    pass

# --- Core Logic Functions ---

def run_command(cmd: List[str], cwd: Optional[Path] = None) -> str:
    """
    Runs a shell command and returns stdout.
    Raises subprocess.CalledProcessError on failure.
    """
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd, result.stdout, result.stderr)
    return result.stdout.strip()

def validate_git_installation() -> bool:
    """Checks if git is installed and available."""
    try:
        subprocess.run(["git", "--version"], check=True, capture_output=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def clone_repo(url: str, target_dir: Path) -> Path:
    """
    Clones a repository from a URL to a target directory.
    """
    if not validate_git_installation():
        raise GitOperationError("Git is not installed or not in PATH.")

    print(f"Pixel Paladin: Cloning {url} to temporary directory...")
    try:
        run_command(["git", "clone", "--depth", "1", url, str(target_dir)])
        return target_dir
    except subprocess.CalledProcessError as e:
        raise GitOperationError(f"Failed to clone repository: {e.stderr}")

def detect_languages(repo_path: Path) -> List[str]:
    """
    Scans repository top-level files to infer project languages.
    Returns a list of detected languages, ordered by confidence.
    """
    detected = []
    files = os.listdir(repo_path)
    
    # Convert to lower set for case-insensitive matching
    files_lower = {f.lower(): f for f in files}

    # heuristic scoring
    scores: Dict[str, int] = {k: 0 for k in LANGUAGE_CONFIG.keys()}

    for lang, config in LANGUAGE_CONFIG.items():
        # Check for marker files (high confidence)
        for marker in config["markers"]:
            if marker.lower() in files_lower:
                scores[lang] += 5
        
        # Check for file extensions (medium confidence)
        for ext in config["extensions"]:
            if any(f.lower().endswith(ext) for f in files):
                scores[lang] += 1

    # Filter languages with a score > 0 and sort
    candidates = [lang for lang, score in scores.items() if score > 0]
    candidates.sort(key=lambda x: scores[x], reverse=True)

    return candidates

def get_package_json_scripts(repo_path: Path) -> Dict[str, str]:
    """
    Parses package.json to find test and lint scripts.
    """
    pkg_json = repo_path / "package.json"
    if pkg_json.exists():
        try:
            with open(pkg_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data.get("scripts", {})
        except json.JSONDecodeError:
            pass
    return {}

def infer_commands(repo_path: Path, language: str) -> Tuple[str, str, Optional[str]]:
    """
    Infers install, test, and lint commands for the given language.
    Returns (install_cmd, test_cmd, lint_cmd).
    """
    config = LANGUAGE_CONFIG[language]
    
    # 1. Install Command Defaults
    install_cmd = config.get("install_cmd", "")
    
    # 2. Test Command Logic
    test_cmd = config.get("test_cmd", "echo 'No test command inferred'")
    
    if language == "Node.js":
        scripts = get_package_json_scripts(repo_path)
        if "test" in scripts:
            test_cmd = f"npm run {scripts['test']}" # usually just 'npm test' maps to scripts.test
            if scripts['test'] != 'test': 
                 # Actually 'npm test' is standard, but we check if 'test' key exists
                 pass 
        # Override test command specifically for npm
        if "test" in scripts:
            test_cmd = "npm test" 
        
        if install_cmd == "npm ci" and not (repo_path / "package-lock.json").exists() and (repo_path / "yarn.lock").exists():
             install_cmd = "yarn install"
             test_cmd = "yarn test"

    if language == "Java":
        if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
            install_cmd = "./gradlew assemble"
            test_cmd = "./gradlew test"
            if not (repo_path / "gradlew").exists():
                install_cmd = "gradle assemble"
                test_cmd = "gradle test"

    # 3. Lint Command Logic
    lint_cmd = None
    files_lower = {f.lower(): f for f in os.listdir(repo_path)}
    
    found_lint_marker = False
    for marker in config.get("lint_markers", []):
        if marker.lower() in files_lower:
            found_lint_marker = True
            # Map specific linters if available in config
            if marker in config.get("lint_commands", {}):
                lint_cmd = config["lint_commands"][marker]
                break
    
    if found_lint_marker and not lint_cmd:
        # Default lint for this language if a marker was found but no specific cmd mapped
        lint_cmd = config["lint_commands"].get("default")

    # Special override for Node if script exists
    if language == "Node.js":
        scripts = get_package_json_scripts(repo_path)
        if "lint" in scripts:
            lint_cmd = "npm run lint"
            if (repo_path / "yarn.lock").exists():
                lint_cmd = "yarn lint"

    return install_cmd, test_cmd, lint_cmd

def generate_workflow_yml(language: str, install_cmd: str, test_cmd: str, lint_cmd: Optional[str]) -> str:
    """
    Constructs the GitHub Actions YAML string.
    """
    config = LANGUAGE_CONFIG[language]
    
    yaml_lines = [
        "name: CI",
        "",
        "on:",
        "  push:",
        "    branches: [ \"main\", \"master\" ]",
        "  pull_request:",
        "    branches: [ \"main\", \"master\" ]",
        "",
        "jobs:",
        "  build:",
        "    runs-on: ubuntu-latest",
        "    steps:",
    ]
    
    # Checkout Step
    yaml_lines.append("    - name: Checkout code")
    yaml_lines.append("      uses: actions/checkout@v4")
    yaml_lines.append("")
    
    # Setup Step
    yaml_lines.append(f"    - name: Set up {language}")
    yaml_lines.append(f"      uses: {config['setup_action']}")
    yaml_lines.append("      with:")
    
    if language == "Rust":
         # Rust setup action often differs slightly, handling via toolchain file or default
         yaml_lines.append("        toolchain: stable")
    else:
         yaml_lines.append(f"        {config['version_param']}: '{config['default_version']}'")
    yaml_lines.append("")

    # Cache Step
    yaml_lines.append("    - name: Cache dependencies")
    yaml_lines.append("      uses: actions/cache@v3")
    yaml_lines.append("      with:")
    yaml_lines.append(f"        path: {config['cache_path']}")
    
    # Construct cache key list
    key_files = config.get("cache_key_files", [])
    if key_files:
        key_str = " ${{ runner.os }}-".join([f"${{ hashFiles('{f}') }}" for f in key_files])
        yaml_lines.append(f"        key: ${{{{ runner.os }}}}-{key_str}")
        yaml_lines.append(f"        restore-keys: |")
        yaml_lines.append(f"          ${{{{ runner.os }}}}-")
    yaml_lines.append("")

    # Install Dependencies Step
    if install_cmd:
        yaml_lines.append("    - name: Install dependencies")
        yaml_lines.append(f"      run: {install_cmd}")
        yaml_lines.append("")
    
    # Lint Step (Conditional)
    if lint_cmd:
        yaml_lines.append("    - name: Lint")
        yaml_lines.append(f"      run: {lint_cmd}")
        yaml_lines.append("")
    
    # Test Step
    yaml_lines.append("    - name: Run Tests")
    yaml_lines.append(f"      run: {test_cmd}")
    yaml_lines.append("")

    return "\n".join(yaml_lines)

def check_remote_repo_validity(repo_url: str) -> bool:
    """
    Uses GitHub API (if token is present) or generic HTTP request 
    to check if the repo URL seems valid.
    """
    # Look for Github Token
    token = os.environ.get("GITHUB_TOKEN")
    headers = {}
    if token:
        headers["Authorization"] = f"token {token}"
    
    # Try to parse owner/repo
    # Supports https://github.com/owner/repo or git@github.com:owner/repo.git
    match = re.search(r"github\.com[/:]([^/]+)/([^/.]+)", repo_url)
    if not match:
        return False # Not a github url, we will try to clone blindly later
    
    owner, repo = match.groups()
    
    if requests:
        api_endpoint = f"{GITHUB_API_URL}/{owner}/{repo}"
        try:
            response = requests.get(api_endpoint, headers=headers, timeout=5)
            return response.status_code == 200
        except requests.RequestException:
            # If API fails, we assume it might be valid but network is down, 
            # or we fall back to cloning later.
            return True
    else:
        return True # No requests lib, assume valid to proceed to git clone

def main():
    parser = argparse.ArgumentParser(
        description="Auto-generate GitHub Actions CI workflow.",
        epilog="Designed by Pixel Paladin."
    )
    parser.add_argument(
        "target",
        help="Path to local repository or URL to remote git repository"
    )
    
    args = parser.parse_args()
    target = args.target
    repo_path: Path
    cleanup_temp = False

    try:
        # Determine if target is URL or Path
        if target.startswith(("http://", "https://", "git@")):
            print(f"Pixel Paladin: Target detected as URL.")
            if not check_remote_repo_validity(target):
                print("Warning: Could not verify repository via API, attempting clone anyway.")
            
            temp_dir = tempfile.mkdtemp(prefix="paladin_ci_")
            repo_path = clone_repo(target, Path(temp_dir))
            cleanup_temp = True
        else:
            print("Pixel Paladin: Target detected as local path.")
            repo_path = Path(target).absolute()
            if not repo_path.exists():
                raise RepositoryNotFoundError(f"Path does not exist: {repo_path}")
            if not (repo_path / ".git").exists():
                print("Warning: Target does not appear to be a git repository.")

        # Detect Language
        print(f"Pixel Paladin: Scanning {repo_path.name} for language markers...")
        languages = detect_languages(repo_path)
        
        if not languages:
            raise LanguageDetectionError("No supported languages detected. "
                                        "Supported: Python, Node.js, Go, Rust, Java.")

        primary_language = languages[0]
        if len(languages) > 1:
            print(f"Pixel Paladin: Detected multiple languages: {', '.join(languages)}")
            print(f"Pixel Paladin: Prioritizing '{primary_language}' for CI generation.")
        else:
            print(f"Pixel Paladin: Detected language: {primary_language}")

        # Infer Commands
        install_cmd, test_cmd, lint_cmd = infer_commands(repo_path, primary_language)
        print(f"Pixel Paladin: Inferred commands -> Install: '{install_cmd}', Test: '{test_cmd}', Lint: '{lint_cmd or 'None'}'")

        # Generate YAML
        print("Pixel Paladin: Constructing CI workflow YAML...")
        yml_content = generate_workflow_yml(primary_language, install_cmd, test_cmd, lint_cmd)

        # Write File
        work_dir = repo_path / ".github" / "workflows"
        work_dir.mkdir(parents=True, exist_ok=True)
        output_file = work_dir / "ci.yml"
        
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(yml_content)
        
        print("-" * 50)
        print(f"SUCCESS: CI workflow generated at {output_file}")
        print("-" * 50)
        print("Content Preview:")
        print("-" * 50)
        print(yml_content)
        print("-" * 50)

    except PaladinsError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected Error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if cleanup_temp:
            print("Pixel Paladin: Cleaning up temporary artifacts...")
            shutil.rmtree(repo_path, ignore_errors=True)

if __name__ == "__main__":
    main()