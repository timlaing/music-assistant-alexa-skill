#!/usr/bin/env python3
import ast
import os
import re
import subprocess
import sys
import shutil

# Simple domain regex (covers typical domains like example.com, sub.example.co.uk)
DOMAIN_RE = re.compile(r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b")

# Default allowlist (lowercase). You may add entries via the SENSITIVE_ALLOWLIST env var
# Common registry hostnames and local hosts are allowlisted by default.
DEFAULT_ALLOWLIST = {
    "localhost",
    "127.0.0.1",
    "ghcr.io",
    "streams.80s80s.de",
    "*.cloudfront.net",
}

IGNORED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".githooks"}
# Files to never scan (relative paths)
IGNORED_FILES = {"scripts/check_sensitive.py"}

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "svg", "webp"}
# Common non-domain file extensions to ignore when matched (e.g., openapi.json)
IGNORE_EXTS = {
    "yml",
    "yaml",
    "json",
    "md",
    "txt",
    "py",
    "sh",
    "cfg",
    "ini",
    "toml",
    "po",
    "mo",
    "ico",
    "css",
    "js",
    "html",
    "svg",
}

# Small whitelist of common TLDs to avoid matching identifiers like `sys.exit`.
TLD_WHITELIST = {
    "com",
    "net",
    "org",
    "io",
    "app",
    "de",
    "uk",
    "co",
    "edu",
    "gov",
    "info",
    "tv",
    "me",
    "xyz",
    "us",
    "ca",
    "biz",
    "online",
    "site",
    "tech",
    "dev",
    "ai",
}


def repo_root():
    # Find repository root by walking up until we find .git or stop at filesystem root
    cur = os.getcwd()
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return os.getcwd()
        cur = parent


def get_git_executable():
    return shutil.which("git")


def get_staged_files():
    git = get_git_executable()
    if git:
        try:
            out = subprocess.check_output(
                [git, "diff", "--cached", "--name-only", "--diff-filter=ACM"], text=True
            )
            return [f for f in out.splitlines() if f]
        except subprocess.CalledProcessError:
            pass

    # Fallback: walk repository and return likely text files
    root = repo_root()
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune ignored directories
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
        for fn in filenames:
            # skip binary-like or large files by extension
            if fn.endswith((".png", ".jpg", ".jpeg", ".gif", ".pb", ".so", ".dll")):
                continue
            path = os.path.relpath(os.path.join(dirpath, fn), start=os.getcwd())
            files.append(path)
    return files


def _resolve_in_repo(path, root=None):
    """Canonical in-repo location for a caller-supplied relative path.

    Rejects empty values, option-style arguments (``-x``), absolute paths and
    ``..`` traversal, and re-checks the resolved location so symlinks cannot
    escape the repository (S8705/S8707: argv-derived values must not reach
    file sinks unvalidated). Returns None for anything unsafe.
    """
    if not path or path.startswith("-") or os.path.isabs(path):
        return None
    if ".." in path.split(os.sep):
        return None
    root_real = os.path.realpath(root or repo_root())
    resolved = os.path.realpath(os.path.join(root_real, path))
    if resolved != root_real and not resolved.startswith(root_real + os.sep):
        return None
    return resolved


def get_staged_content(path, root=None):
    root_real = os.path.realpath(root or repo_root())
    resolved = _resolve_in_repo(path, root_real)
    if resolved is None:
        return ""
    git = get_git_executable()
    if git:
        try:
            # Get raw bytes from git to avoid UnicodeDecodeError when blobs
            # contain binary. The ":<path>" revspec form cannot be mistaken
            # for an option regardless of the path contents.
            out = subprocess.check_output(
                [git, "show", f":{path}"], cwd=root_real, stderr=subprocess.DEVNULL
            )
            return out.decode("utf-8", errors="ignore")
        except (subprocess.CalledProcessError, OSError):
            pass

    # Fallback: read file from working tree (already confined to the repo)
    try:
        if not os.path.isfile(resolved):
            return ""
        with open(resolved, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def _python_strings(content):
    """Extract string literals from Python source; fall back to the raw text."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return content
    strings = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
        elif isinstance(node, ast.JoinedStr):
            for val in getattr(node, "values", []):
                if isinstance(val, ast.Constant) and isinstance(val.value, str):
                    strings.append(val.value)
    return "\n".join(strings)


def _shell_strings(content):
    """Extract heredoc bodies (parsed as Python when possible) and quoted strings."""
    strings = []
    for _, body in re.findall(r"<<['\"]?(\w+)['\"]?\n(.*?)\n\1", content, re.S):
        try:
            tree = ast.parse(body)
        except (SyntaxError, ValueError):
            # fallback: extract quoted strings from the heredoc body
            found = re.findall(r"'([^']*)'|\"([^\"]*)\"", body)
            strings.extend([a or b for a, b in found])
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                strings.append(node.value)
            elif isinstance(node, ast.JoinedStr):
                for val in getattr(node, "values", []):
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        strings.append(val.value)

    # also capture top-level quoted strings in the shell script
    found_top = re.findall(r"'([^']*)'|\"([^\"]*)\"", content)
    strings.extend([a or b for a, b in found_top])
    return "\n".join(strings)


def _scan_text_for(path, content, ext):
    """Choose the string extractor for a file's extension."""
    if ext == ".py":
        return _python_strings(content)
    if ext == ".sh":
        return _shell_strings(content)
    return content


def _line_for(text, start, end):
    line_start = text.rfind("\n", 0, start)
    line_start = 0 if line_start == -1 else line_start + 1
    line_end = text.find("\n", end)
    if line_end == -1:
        return text[line_start:]
    return text[line_start:line_end]


def _ignored_domain(domain):
    """Skip image/file extensions and labels that are not plausible TLDs."""
    parts = domain.rsplit(".", 1)
    if len(parts) != 2:
        return False
    ext = parts[1].lower()
    # ignore image/file extensions and other ignored file-like endings
    if ext in IMAGE_EXTS or ext in IGNORE_EXTS:
        return True
    # require last label look like a real TLD to avoid matching code identifiers
    return ext not in TLD_WHITELIST


def _context_allows(path, line):
    """Skip GitHub Actions expressions and non-URL workflow lines."""
    # skip GitHub Actions expression tokens like ${{ github.actor }}
    if "${{" in line or "}}" in line:
        return True
    # for workflow files, be conservative: only flag if the line contains a URL
    if path.startswith(".github/workflows") and (
        "http" not in line and "://" not in line
    ):
        return True
    return False


def scan_files(paths, allowlist):
    findings = []
    for p in paths:
        # skip repository-local hooks and other ignored paths
        if p.split(os.sep)[0] in IGNORED_DIRS:
            continue
        if os.path.normpath(p) in IGNORED_FILES:
            continue
        _, ext = os.path.splitext(p)
        # skip common image files entirely
        if ext.lower().lstrip(".") in IMAGE_EXTS:
            continue
        content = get_staged_content(p)
        if not content:
            continue
        scan_text = _scan_text_for(p, content, ext)
        for m in DOMAIN_RE.finditer(scan_text):
            domain = m.group(0)
            line = _line_for(scan_text, m.start(), m.end())
            if _ignored_domain(domain) or _context_allows(p, line):
                continue
            if is_allowed(domain, allowlist):
                continue
            findings.append((p, domain))
    return findings


def is_allowed(domain, allowlist):
    d = domain.lower()
    for a in allowlist:
        a = a.lower()
        if a.startswith("*."):
            # wildcard subdomain match
            if d == a[2:] or d.endswith(a[1:]):
                return True
        else:
            if d == a:
                return True
    return False


def build_allowlist():
    allow = set(DEFAULT_ALLOWLIST)
    # Repo-level allowlist file (.sensitive_allowlist)
    try:
        root = repo_root()
        allowfile = os.path.join(root, ".sensitive_allowlist")
        if os.path.exists(allowfile):
            with open(allowfile, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    allow.add(line.lower())
    except Exception:
        pass

    # Environment variable (comma-separated) overrides/additions
    env = os.environ.get("SENSITIVE_ALLOWLIST")
    if env:
        for part in env.split(","):
            p = part.strip().lower()
            if p:
                allow.add(p)
    return allow


def main(args):
    paths = args if args else get_staged_files()
    if not paths:
        return 0
    allowlist = build_allowlist()
    bad = scan_files(paths, allowlist)
    if bad:
        print("Error: sensitive domain names detected:")
        reported = set()
        for path, domain in bad:
            key = (path, domain)
            if key in reported:
                continue
            reported.add(key)
            print(f" - {path}: {domain}")
        print(
            "\nIf these are expected, add them to the allowlist via the SENSITIVE_ALLOWLIST env var or commit with --no-verify."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
