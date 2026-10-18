"""Security and behaviour tests for scripts/check_sensitive.py.

Locks in the S8705/S8707 hardening: argv-derived paths must never reach the
git or filesystem sinks unless they resolve inside the repository.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_sensitive.py"

_spec = importlib.util.spec_from_file_location("check_sensitive", SCRIPT)
check_sensitive = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_sensitive)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".git").mkdir(parents=True)
    return root


# --- _resolve_in_repo -----------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "-rf", "--output=/tmp/x", "/etc/passwd", "../outside.txt", "a/../../b"],
)
def test_resolve_in_repo_rejects_unsafe_args(repo, bad):
    assert check_sensitive._resolve_in_repo(bad, str(repo)) is None


def test_resolve_in_repo_accepts_plain_relative_path(repo):
    target = repo / "docs" / "note.txt"
    target.parent.mkdir()
    target.write_text("x")
    assert check_sensitive._resolve_in_repo("docs/note.txt", str(repo)) == str(
        target.resolve()
    )


def test_resolve_in_repo_rejects_symlink_escape(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    assert check_sensitive._resolve_in_repo("escape/secret.txt", str(repo)) is None


# --- get_staged_content ---------------------------------------------------


def test_get_staged_content_rejects_traversal(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    assert check_sensitive.get_staged_content("/etc/passwd", str(repo)) == ""
    assert check_sensitive.get_staged_content("../x.txt", str(repo)) == ""
    assert check_sensitive.get_staged_content("-p", str(repo)) == ""


def test_get_staged_content_git_uses_revspec_and_repo_cwd(repo, monkeypatch):
    captured = {}

    def fake_check_output(cmd, cwd=None, stderr=None, text=False):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return b"hello from index"

    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(check_sensitive.subprocess, "check_output", fake_check_output)
    out = check_sensitive.get_staged_content("app/demo.py", str(repo))
    assert out == "hello from index"
    assert captured["cmd"] == ["/usr/bin/git", "show", ":app/demo.py"]
    assert captured["cwd"] == str(repo.resolve())


def test_get_staged_content_fallback_reads_in_repo(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    (repo / "docs").mkdir()
    (repo / "docs" / "a.txt").write_text("staged text", encoding="utf-8")
    assert check_sensitive.get_staged_content("docs/a.txt", str(repo)) == "staged text"
    assert check_sensitive.get_staged_content("docs/missing.txt", str(repo)) == ""


# --- extractors -----------------------------------------------------------


def test_python_strings_only_scans_literals():
    src = "import os\nvalue = os.path.join('a', 'b')\nURL = 'https://real.example.com/x'\n"
    text = check_sensitive._python_strings(src)
    assert "real.example.com" in text
    assert "os.path.join" not in text


def test_python_strings_unparsable_falls_back_to_raw():
    src = "def broken(:\n"
    assert check_sensitive._python_strings(src) == src


def test_shell_strings_captures_heredoc_python_and_quoted():
    sh = (
        "#!/bin/sh\n"
        "run <<'PYEOF'\n"
        "endpoint = 'https://heredoc.example.io/v1'\n"
        "PYEOF\n"
        "echo 'https://quoted.example.net/x'\n"
    )
    text = check_sensitive._shell_strings(sh)
    assert "heredoc.example.io" in text
    assert "quoted.example.net" in text


def test_shell_strings_heredoc_fallback_to_quotes():
    sh = "run <<'EOF'\nnot python at all 'https://fallback.example.org'\nEOF\n"
    assert "fallback.example.org" in check_sensitive._shell_strings(sh)


# --- scan_files -----------------------------------------------------------


def _write(root, rel, content):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return rel


def test_scan_files_flags_domain_in_python_string(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    rel = _write(repo, "demo.py", "URL = 'https://leak.example.com/api'\n")
    assert check_sensitive.scan_files([rel], set()) == [(rel, "leak.example.com")]


def test_scan_files_ignores_identifiers_and_allowlist(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    rel = _write(
        repo,
        "demo.py",
        "import sys\nsys.exit(1)\nURL = 'https://ghcr.io/img'\n",
    )
    # code identifiers like sys.exit never reach the scanner (AST extraction);
    # the registry hostname is only suppressed via the allowlist argument
    assert check_sensitive.scan_files([rel], set()) == [(rel, "ghcr.io")]
    assert check_sensitive.scan_files([rel], {"ghcr.io"}) == []


def test_scan_files_workflow_context(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    # GitHub Actions tokens and non-URL lines are skipped
    rel_expr = _write(
        repo,
        ".github/workflows/ci.yml",
        "run: echo ${{ github.actor }} to api.partner.example.com\n",
    )
    # A real URL on a workflow line is flagged
    rel_url = _write(
        repo,
        ".github/workflows/deploy.yml",
        "run: curl https://exfil.example.com/upload\n",
    )
    assert check_sensitive.scan_files([rel_expr], set()) == []
    assert check_sensitive.scan_files([rel_url], set()) == [
        (rel_url, "exfil.example.com")
    ]


def test_scan_files_skips_images_and_ignored_dirs(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    img = _write(repo, "assets/pic.png", "https://inside.example.com/x")
    nested = _write(repo, "node_modules/pkg/index.js", "url = 'https://cdn.example.io'")
    assert check_sensitive.scan_files([img, nested], set()) == []


# --- main -----------------------------------------------------------------


def test_main_reports_findings(repo, monkeypatch, capsys):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    rel = _write(repo, "leak.py", "URL = 'https://leak.example.com/api'\n")
    assert check_sensitive.main([rel]) == 1
    out = capsys.readouterr().out
    assert "leak.example.com" in out
    assert "--no-verify" in out


def test_main_deduplicates_findings(repo, monkeypatch, capsys):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    rel = _write(
        repo,
        "dupe.py",
        "A = 'https://dupe.example.com/1'\nB = 'https://dupe.example.com/2'\n",
    )
    assert check_sensitive.main([rel]) == 1
    out = capsys.readouterr().out
    assert out.count("dupe.example.com") == 1


def test_main_clean_and_empty(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    rel = _write(repo, "clean.py", "URL = 'https://localhost/api'\n")
    assert check_sensitive.main([rel]) == 0
    assert check_sensitive.main([]) == 0


# --- repo_root / get_staged_files / build_allowlist -----------------------


def test_repo_root_walks_up_and_falls_back(repo, monkeypatch):
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert check_sensitive.repo_root() == str(repo)
    plain = repo.parent / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert check_sensitive.repo_root() == str(plain)


def test_get_staged_files_from_git(monkeypatch):
    def fake_check_output(cmd, text=False, cwd=None, stderr=None):
        assert "--cached" in cmd
        return "app/app.py\n\nscripts/demo.py\n"

    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(check_sensitive.subprocess, "check_output", fake_check_output)
    assert check_sensitive.get_staged_files() == ["app/app.py", "scripts/demo.py"]


def test_get_staged_files_fallback_walk(repo, monkeypatch):
    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: None)
    monkeypatch.chdir(repo)
    (repo / "src").mkdir()
    (repo / "src" / "keep.py").write_text("x")
    (repo / "skip.png").write_text("x")
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "junk.py").write_text("x")
    files = check_sensitive.get_staged_files()
    assert "src/keep.py" in files
    assert not any(f.endswith("skip.png") for f in files)
    assert not any(f.startswith("node_modules") for f in files)


def test_build_allowlist_file_and_env(repo, monkeypatch):
    monkeypatch.chdir(repo)
    (repo / ".sensitive_allowlist").write_text(
        "# comment\npartner.example.net\n\nUPPER.example.org\n"
    )
    monkeypatch.setenv("SENSITIVE_ALLOWLIST", "extra.example.com, ,also.example.io")
    allow = check_sensitive.build_allowlist()
    assert {"partner.example.net", "upper.example.org"} <= allow
    assert {"extra.example.com", "also.example.io"} <= allow
    assert check_sensitive.DEFAULT_ALLOWLIST <= allow


def test_get_staged_content_git_error_falls_back(repo, monkeypatch):
    def fake_check_output(cmd, cwd=None, stderr=None, text=False):
        raise check_sensitive.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(check_sensitive, "get_git_executable", lambda: "/usr/bin/git")
    monkeypatch.setattr(check_sensitive.subprocess, "check_output", fake_check_output)
    (repo / "a.txt").write_text("fallback works", encoding="utf-8")
    assert check_sensitive.get_staged_content("a.txt", str(repo)) == "fallback works"
