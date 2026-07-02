"""U26 — packaging/licensing/secrets checks for publishing as a Reachy Mini app on HF.

These are lightweight static checks (no network, no HF calls) that the app's packaging
metadata is in good shape ahead of an eventual (human-approved) publish.
"""

from pathlib import Path

import tomllib


ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict:
    with (ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)


def test_pyproject_declares_license():
    data = _load_pyproject()
    project = data["project"]
    assert project.get("license", {}).get("text") == "Apache-2.0"


def test_pyproject_keeps_reachy_mini_app_entry_point():
    data = _load_pyproject()
    entry_points = data["project"]["entry-points"]["reachy_mini_apps"]
    assert entry_points["reachy_fleet_supervisor"] == (
        "reachy_fleet_supervisor.supervisor_app:ReachyFleetSupervisorApp"
    )


def test_license_file_present_and_apache2():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "Apache License" in license_text
    assert "Version 2.0" in license_text


def test_notice_file_attributes_pollen_base():
    notice_text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Pollen Robotics" in notice_text
    assert "Apache License" in notice_text


def test_readme_frontmatter_has_hf_spaces_metadata():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert readme.startswith("---\n")
    frontmatter, _, _ = readme[4:].partition("\n---\n")
    assert "title:" in frontmatter
    assert "sdk:" in frontmatter
    assert "license: apache-2.0" in frontmatter
    assert "reachy_mini" in frontmatter


def test_env_example_has_no_populated_secrets():
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for line in env_example.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() in {"OPENAI_API_KEY", "HF_TOKEN"}:
            assert value.strip() in ("", '""'), f"{key} must stay blank in .env.example"


def test_gitignore_excludes_dotenv():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in gitignore.split()

