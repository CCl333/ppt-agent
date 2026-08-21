from __future__ import annotations

from pathlib import Path

from app.services.font_policy import build_font_report, decide_font_action


def test_system_font_is_not_embedded():
    decision = decide_font_action("Microsoft YaHei, sans-serif")
    assert decision["license_id"] == "system"
    assert decision["embed"] is False
    assert decision["action"] == "system"


def test_harmonyos_never_embeds_and_requires_notice():
    decision = decide_font_action("HarmonyOS Sans SC")
    assert decision["license_id"] == "HarmonyOS-Sans-EULA"
    assert decision["embed"] is False
    assert decision["action"] == "prompt_install"
    assert "HarmonyOS Sans" in (decision["notice"] or "")


def test_ofl_embeds_only_when_file_exists(tmp_path: Path):
    missing = decide_font_action("Source Han Sans SC", font_dir=tmp_path)
    assert missing["embed"] is False
    assert missing["action"] == "declare"
    font_path = tmp_path / "source-han-sans-sc.otf"
    font_path.write_bytes(b"OTTO")
    present = decide_font_action("Source Han Sans SC", font_dir=tmp_path)
    assert present["embed"] is True
    assert present["action"] == "embed"


def test_font_report_collects_svg_families_and_skips_generic():
    svg = """
    <svg viewBox="0 0 1280 720">
      <text font-family='"Microsoft YaHei", sans-serif'>标题</text>
      <text font-family="HarmonyOS Sans SC">角标</text>
      <text font-family="sans-serif">忽略</text>
    </svg>
    """
    report = build_font_report([svg])
    names = {item["name"] for item in report["fonts"]}
    assert "Microsoft YaHei" in names
    assert "HarmonyOS Sans SC" in names
    assert "sans-serif" not in names
    assert report["install_required"] is True
    assert report["notices"]
    assert all(not item["embed"] for item in report["fonts"] if item["license_id"] == "HarmonyOS-Sans-EULA")
