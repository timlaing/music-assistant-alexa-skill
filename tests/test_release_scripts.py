"""Tests for the release/build helper scripts in scripts/.

These scripts were previously omitted from coverage (see .coveragerc);
they now carry tests so their lines count toward the SonarQube gate.
The top-level scripts are exercised in-process (runpy) so their lines
are measured by coverage at all.
"""

import importlib.util
import json
import runpy
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _load(script_name):
    spec = importlib.util.spec_from_file_location(
        script_name, SCRIPTS / f"{script_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_top_level(script_name, argv, monkeypatch, capsys):
    """Run a top-level script in-process; returns captured output."""
    monkeypatch.setattr(sys, "argv", [script_name, *argv])
    runpy.run_path(str(SCRIPTS / script_name), run_name="__main__")
    return capsys.readouterr()


def _run_top_level_exiting(script_name, argv, monkeypatch, capsys):
    """Run a top-level script expected to exit; returns (output, exit_code)."""
    monkeypatch.setattr(sys, "argv", [script_name, *argv])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_path(str(SCRIPTS / script_name), run_name="__main__")
    return capsys.readouterr(), excinfo.value.code


# --- bump_version ---------------------------------------------------------


@pytest.fixture
def bump(monkeypatch, tmp_path):
    module = _load("bump_version")
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.2.3\n", encoding="utf-8")
    monkeypatch.setattr(module, "VERSION_FILE", version_file)
    monkeypatch.setattr(module, "SYNC_SCRIPT", tmp_path / "missing_sync.py")
    monkeypatch.setattr(module, "ROOT", tmp_path)
    state = {"syncs": 0, "staged": []}
    monkeypatch.setattr(
        module, "run_sync", lambda: state.__setitem__("syncs", state["syncs"] + 1)
    )
    monkeypatch.setattr(
        module,
        "git_add",
        lambda paths: state["staged"].append([str(p) for p in paths]),
    )
    module._state = state
    return module


def test_read_version_plain_and_code_fenced(bump):
    assert bump.read_version() == "1.2.3"
    bump.VERSION_FILE.write_text("```\n9.9.9\n```\n", encoding="utf-8")
    assert bump.read_version() == "9.9.9"


def test_bump_version_string_increments_patch():
    assert _load("bump_version").bump_version_string("1.2.3") == "1.2.4"


def test_bump_version_string_preserves_suffix():
    module = _load("bump_version")
    assert module.bump_version_string("0.0.2-beta") == "0.0.3-beta"
    assert module.bump_version_string("1.0.0+build.7") == "1.0.1+build.7"


@pytest.mark.parametrize("bad", ["garbage", "1.2", "v1.2.3", ""])
def test_bump_version_string_rejects_bad_input(bad):
    with pytest.raises(SystemExit):
        _load("bump_version").bump_version_string(bad)


def test_main_bumps_and_stages_for_app_change(bump, monkeypatch):
    monkeypatch.setattr(bump, "get_staged_files", lambda: ["app/skill/x.py"])
    assert bump.main() == 0
    assert bump.VERSION_FILE.read_text(encoding="utf-8") == "1.2.4\n"
    assert bump._state["syncs"] == 1
    assert bump._state["staged"] == [[str(bump.VERSION_FILE)]]


def test_main_stages_addon_config_when_present(bump, monkeypatch):
    cfg = bump.ROOT / "addons" / "music-assistant-skill" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text('{"version": "1.2.3"}\n', encoding="utf-8")
    monkeypatch.setattr(bump, "get_staged_files", lambda: ["Dockerfile"])
    assert bump.main() == 0
    (staged,) = bump._state["staged"]
    assert str(cfg) in staged


def test_main_skips_when_version_already_staged(bump, monkeypatch):
    monkeypatch.setattr(bump, "get_staged_files", lambda: ["VERSION", "app/app.py"])
    assert bump.main() == 0
    assert bump.VERSION_FILE.read_text(encoding="utf-8") == "1.2.3\n"
    assert bump._state["syncs"] == 0
    assert bump._state["staged"] == []


@pytest.mark.parametrize("staged", [["README.md"], [".gitignore"], ["docs/x.md"], []])
def test_main_skips_without_image_related_changes(bump, monkeypatch, staged):
    monkeypatch.setattr(bump, "get_staged_files", lambda: list(staged))
    assert bump.main() == 0
    assert bump.VERSION_FILE.read_text(encoding="utf-8") == "1.2.3\n"
    assert bump._state["syncs"] == 0


def test_main_errors_when_version_file_missing(bump, monkeypatch):
    bump.VERSION_FILE.unlink()
    monkeypatch.setattr(bump, "get_staged_files", lambda: ["app/app.py"])
    with pytest.raises(SystemExit):
        bump.main()


def test_run_sync_noop_when_script_missing(bump, capsys):
    module = _load("bump_version")
    # SYNC_SCRIPT points at a missing file: no subprocess, no warning.
    module.run_sync()
    assert "failed to run" not in capsys.readouterr().err


def test_run_sync_executes_script_with_python3(bump):
    module = _load("bump_version")
    with mock.patch.object(module.subprocess, "run") as run:
        module.run_sync()
    run.assert_called_once()
    argv = run.call_args[0][0]
    assert argv[0] == "python3"
    assert argv[1] == str(module.SYNC_SCRIPT)


def test_run_sync_warns_when_both_invocations_fail(bump, capsys):
    module = _load("bump_version")
    with mock.patch.object(module.subprocess, "run", side_effect=OSError):
        module.run_sync()
    assert "failed to run sync_version.py" in capsys.readouterr().err


def test_get_staged_files_returns_empty_when_git_fails(bump):
    module = _load("bump_version")
    with mock.patch.object(module.subprocess, "check_output", side_effect=OSError):
        assert module.get_staged_files() == []


def test_git_add_warns_when_git_fails(bump, capsys):
    module = _load("bump_version")
    with mock.patch.object(module.subprocess, "run", side_effect=OSError):
        module.git_add(["VERSION"])
    assert "git add failed" in capsys.readouterr().err


# --- sync_version ---------------------------------------------------------


@pytest.fixture
def sync_mod(monkeypatch, tmp_path):
    module = _load("sync_version")
    version_file = tmp_path / "VERSION"
    version_file.write_text("2.0.0\n", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"version": "1.0.0", "other": True}) + "\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "VERSION_FILE", version_file)
    monkeypatch.setattr(module, "CONFIG", config)
    return module


def test_sync_read_version_plain_and_code_fenced(sync_mod):
    assert sync_mod.read_version() == "2.0.0"
    sync_mod.VERSION_FILE.write_text("```\n3.1.4\n```\n", encoding="utf-8")
    assert sync_mod.read_version() == "3.1.4"


def test_sync_read_version_missing_file_raises(sync_mod):
    sync_mod.VERSION_FILE.unlink()
    with pytest.raises(SystemExit):
        sync_mod.read_version()


def test_sync_updates_outdated_config(sync_mod):
    assert sync_mod.sync() == 0
    raw = sync_mod.CONFIG.read_text(encoding="utf-8")
    assert json.loads(raw) == {"version": "2.0.0", "other": True}
    assert raw.endswith("\n")
    assert '"version": "2.0.0"' in raw


def test_sync_noop_when_config_current(sync_mod):
    sync_mod.CONFIG.write_text(
        json.dumps({"version": "2.0.0", "other": True}) + "\n", encoding="utf-8"
    )
    before = sync_mod.CONFIG.read_text(encoding="utf-8")
    assert sync_mod.sync() == 0
    assert sync_mod.CONFIG.read_text(encoding="utf-8") == before


def test_sync_errors_when_config_missing(sync_mod):
    sync_mod.CONFIG.unlink()
    with pytest.raises(SystemExit):
        sync_mod.sync()


def test_sync_errors_when_version_missing(sync_mod):
    sync_mod.VERSION_FILE.unlink()
    with pytest.raises(SystemExit):
        sync_mod.sync()


# --- build_skill_manifest (top-level script) ------------------------------


def _manifest_template():
    return {
        "manifest": {
            "publishingInformation": {
                "summary": "summary",
                "locales": {
                    "en-US": {
                        "smallIconUri": "https://icons.example.com/s.png",
                        "largeIconUri": "https://icons.example.com/l.png",
                    }
                },
            },
            "apis": {"custom": {"interfaces": [{"type": "AUDIO_PLAYER"}]}},
        }
    }


@pytest.fixture
def manifest_run(tmp_path, monkeypatch, capsys):
    def run(data=None, args=None, endpoint="https://example.com"):
        infile = tmp_path / "in.json"
        infile.write_text(json.dumps(data or _manifest_template()), encoding="utf-8")
        outfile = tmp_path / "out.json"
        argv = [str(infile), str(outfile)]
        if args:
            argv.extend(args)
        elif endpoint is not None:
            argv.append(endpoint)
        out = _run_top_level("build_skill_manifest.py", argv, monkeypatch, capsys)
        return json.loads(outfile.read_text(encoding="utf-8")), out

    return run


def test_manifest_sets_endpoint_and_apl_interface(manifest_run):
    result, out = manifest_run()
    custom = result["manifest"]["apis"]["custom"]
    assert custom["endpoint"] == {
        "uri": "https://example.com",
        "sslCertificateType": "Wildcard",
    }
    assert {"type": "ALEXA_PRESENTATION_APL"} in custom["interfaces"]
    assert {"type": "AUDIO_PLAYER"} in custom["interfaces"]
    locale = result["manifest"]["publishingInformation"]["locales"]["en-US"]
    assert locale["name"] == "Music Assistant"
    assert len(locale["examplePhrases"]) == 3
    assert "WROTE" in out.out


def test_manifest_keeps_existing_apl_interface(manifest_run):
    data = _manifest_template()
    data["manifest"]["apis"]["custom"]["interfaces"] = [
        {"type": "ALEXA_PRESENTATION_APL"}
    ]
    result, _ = manifest_run(data=data)
    interfaces = result["manifest"]["apis"]["custom"]["interfaces"]
    assert interfaces.count({"type": "ALEXA_PRESENTATION_APL"}) == 1


def test_manifest_reuses_endpoint_from_template(manifest_run):
    data = _manifest_template()
    data["manifest"]["apis"]["custom"]["endpoint"] = {
        "uri": "https://from-template.example.com",
        "sourceDir": "some/dir",
    }
    result, _ = manifest_run(data=data, endpoint=None)
    custom = result["manifest"]["apis"]["custom"]
    assert custom["endpoint"] == {"uri": "https://from-template.example.com"}


def test_manifest_errors_without_any_endpoint(tmp_path, monkeypatch, capsys):
    infile = tmp_path / "in.json"
    infile.write_text(json.dumps(_manifest_template()), encoding="utf-8")
    outfile = tmp_path / "out.json"
    with pytest.raises(SystemExit) as excinfo:
        _run_top_level(
            "build_skill_manifest.py",
            [str(infile), str(outfile)],
            monkeypatch,
            capsys,
        )
    assert excinfo.value.code == 4
    assert "No endpoint URI found" in capsys.readouterr().err


def test_manifest_target_locale_replaces_locales(manifest_run):
    result, _ = manifest_run(args=["https://example.com", "de-DE"])
    locales = result["manifest"]["publishingInformation"]["locales"]
    assert list(locales) == ["de-DE"]
    assert locales["de-DE"]["name"] == "Music Assistant"
    assert "smallIconUri" not in locales["de-DE"]


def test_manifest_normalizes_icons_across_locales(manifest_run):
    data = _manifest_template()
    data["manifest"]["publishingInformation"]["locales"]["fr-FR"] = {}
    result, _ = manifest_run(data=data)
    locales = result["manifest"]["publishingInformation"]["locales"]
    assert locales["fr-FR"]["smallIconUri"] == "https://icons.example.com/s.png"
    assert locales["fr-FR"]["largeIconUri"] == "https://icons.example.com/l.png"


def test_manifest_skips_non_dict_locale_values(manifest_run):
    data = _manifest_template()
    data["manifest"]["publishingInformation"]["locales"]["broken"] = "oops"
    result, _ = manifest_run(data=data)
    locales = result["manifest"]["publishingInformation"]["locales"]
    assert locales["broken"] == "oops"


@pytest.mark.parametrize("argv", [[], ["only-one-arg"]])
def test_manifest_requires_two_args(tmp_path, monkeypatch, capsys, argv):
    with pytest.raises(SystemExit) as excinfo:
        _run_top_level("build_skill_manifest.py", argv, monkeypatch, capsys)
    assert excinfo.value.code == 2
    assert "usage:" in capsys.readouterr().out


def test_manifest_rejects_missing_input(tmp_path, monkeypatch, capsys):
    outfile = tmp_path / "out.json"
    with pytest.raises(SystemExit) as excinfo:
        _run_top_level(
            "build_skill_manifest.py",
            [str(tmp_path / "missing.json"), str(outfile)],
            monkeypatch,
            capsys,
        )
    assert excinfo.value.code == 2
    assert "input manifest not found" in capsys.readouterr().err


def test_manifest_rejects_non_json_input(tmp_path, monkeypatch, capsys):
    infile = tmp_path / "in.txt"
    infile.write_text("{}", encoding="utf-8")
    outfile = tmp_path / "out.json"
    with pytest.raises(SystemExit) as excinfo:
        _run_top_level(
            "build_skill_manifest.py",
            [str(infile), str(outfile)],
            monkeypatch,
            capsys,
        )
    assert excinfo.value.code == 2


def test_manifest_rejects_non_json_output(tmp_path, monkeypatch, capsys):
    infile = tmp_path / "in.json"
    infile.write_text(json.dumps(_manifest_template()), encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        _run_top_level(
            "build_skill_manifest.py",
            [str(infile), str(tmp_path / "out.txt")],
            monkeypatch,
            capsys,
        )
    assert excinfo.value.code == 2


# --- find_skills_to_delete (top-level script) -----------------------------


def _write_skills(tmp_path, payload):
    path = tmp_path / "skills.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_find_skills_no_args_prints_empty(tmp_path, monkeypatch, capsys):
    out, code = _run_top_level_exiting(
        "find_skills_to_delete.py", [], monkeypatch, capsys
    )
    assert code == 0
    assert out.out.strip() == ""


def test_find_skills_non_json_arg_prints_empty(tmp_path, monkeypatch, capsys):
    path = tmp_path / "skills.txt"
    path.write_text("{}", encoding="utf-8")
    out, code = _run_top_level_exiting(
        "find_skills_to_delete.py", [str(path)], monkeypatch, capsys
    )
    assert code == 0
    assert out.out.strip() == ""


def test_find_skills_invalid_json_prints_empty(tmp_path, monkeypatch, capsys):
    path = tmp_path / "skills.json"
    path.write_text("{not json", encoding="utf-8")
    out, code = _run_top_level_exiting(
        "find_skills_to_delete.py", [str(path)], monkeypatch, capsys
    )
    assert code == 0
    assert out.out.strip() == ""


@pytest.mark.parametrize(
    "entry",
    [
        {"skillName": "music assistant", "skillId": "amzn1.ask.skill.1"},
        {"name": "music assistant", "id": "amzn1.ask.skill.2"},
        {"skillName": " Music Assistant ", "skillId": "amzn1.ask.skill.3"},
        {"nameByLocale": {"en-US": "music assistant"}, "skillId": "amzn1.ask.skill.4"},
        {"nameByLocale": {"fr-FR": "music assistant"}, "id": "amzn1.ask.skill.5"},
    ],
)
def test_find_skills_matches_by_name_fields(tmp_path, monkeypatch, capsys, entry):
    path = _write_skills(tmp_path, {"skills": [entry]})
    out = _run_top_level("find_skills_to_delete.py", [path], monkeypatch, capsys)
    assert entry.get("skillId") or entry.get("id") in out.out.split()


def test_find_skills_ignores_other_names(tmp_path, monkeypatch, capsys):
    path = _write_skills(
        tmp_path, {"skills": [{"skillName": "other skill", "skillId": "X"}]}
    )
    out = _run_top_level("find_skills_to_delete.py", [path], monkeypatch, capsys)
    assert out.out.strip() == ""


def test_find_skills_ignores_non_dict_entries(tmp_path, monkeypatch, capsys):
    path = _write_skills(tmp_path, {"skills": ["junk", 42, None]})
    out = _run_top_level("find_skills_to_delete.py", [path], monkeypatch, capsys)
    assert out.out.strip() == ""


def test_find_skills_walk_finds_nested_matches(tmp_path, monkeypatch, capsys):
    path = _write_skills(
        tmp_path,
        {"children": [{"skillId": "nested-1", "name": "Music Assistant"}]},
    )
    out = _run_top_level("find_skills_to_delete.py", [path], monkeypatch, capsys)
    assert "nested-1" in out.out.split()


def test_find_skills_handles_top_level_list_payload(tmp_path, monkeypatch, capsys):
    path = _write_skills(tmp_path, [{"skillId": "listed-1", "name": "music assistant"}])
    out = _run_top_level("find_skills_to_delete.py", [path], monkeypatch, capsys)
    assert "listed-1" in out.out.split()
