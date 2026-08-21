from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.services.svg import _ensure_svg_xmlns, _local_tag, _parse_svg

LicenseId = Literal["system", "OFL-1.1", "HarmonyOS-Sans-EULA", "unknown"]

HARMONYOS_NOTICE = "本产品使用 HarmonyOS Sans 字体。导出前请先安装该字体，且不得子集化嵌入。"
_FONT_DIR = Path(__file__).resolve().parents[3] / "fonts"


@dataclass(frozen=True)
class FontPolicy:
    name: str
    license_id: LicenseId
    aliases: tuple[str, ...]
    embed_if_file: bool = False
    notice: str | None = None


FONT_REGISTRY: tuple[FontPolicy, ...] = (
    FontPolicy("Microsoft YaHei", "system", ("microsoft yahei", "微软雅黑")),
    FontPolicy("PingFang SC", "system", ("pingfang sc", "苹方")),
    FontPolicy("Noto Sans SC", "OFL-1.1", ("noto sans sc",), embed_if_file=True),
    FontPolicy(
        "Source Han Sans SC",
        "OFL-1.1",
        ("source han sans sc", "source han sans cn", "source han sans", "思源黑体"),
        embed_if_file=True,
    ),
    FontPolicy(
        "Source Han Serif CN",
        "OFL-1.1",
        ("source han serif cn", "source han serif sc", "source han serif", "思源宋体"),
        embed_if_file=True,
    ),
    FontPolicy(
        "HarmonyOS Sans SC",
        "HarmonyOS-Sans-EULA",
        ("harmonyos sans sc", "harmonyos sans cn", "harmonyos sans"),
        notice=HARMONYOS_NOTICE,
    ),
)


def normalize_family(family: str | None) -> str:
    first = str(family or "").split(",")[0].strip().strip("\"'")
    return first


def lookup_font(family: str | None) -> FontPolicy | None:
    name = normalize_family(family)
    if not name:
        return None
    key = name.lower()
    if key in {"sans-serif", "serif", "monospace"}:
        return None
    for policy in FONT_REGISTRY:
        if key == policy.name.lower() or key in policy.aliases:
            return policy
    return None


def font_file_for(policy: FontPolicy, *, font_dir: Path | None = None) -> Path | None:
    directory = font_dir or _FONT_DIR
    slug = policy.name.lower().replace(" ", "-")
    for suffix in (".otf", ".ttf"):
        path = directory / f"{slug}{suffix}"
        if path.is_file() and path.stat().st_size > 0:
            return path
    return None


def decide_font_action(family: str | None, *, font_dir: Path | None = None) -> dict[str, Any]:
    name = normalize_family(family) or "Microsoft YaHei"
    policy = lookup_font(name)
    if policy is None:
        return {
            "name": name,
            "license_id": "unknown",
            "action": "declare",
            "embed": False,
            "notice": None,
            "note": "未知字体，只声明 typeface，不嵌入",
        }
    if policy.license_id == "HarmonyOS-Sans-EULA":
        return {
            "name": policy.name,
            "license_id": policy.license_id,
            "action": "prompt_install",
            "embed": False,
            "notice": policy.notice,
            "note": "HarmonyOS Sans 不得子集化嵌入",
        }
    if policy.embed_if_file:
        path = font_file_for(policy, font_dir=font_dir)
        if path is not None:
            return {
                "name": policy.name,
                "license_id": policy.license_id,
                "action": "embed",
                "embed": True,
                "notice": None,
                "note": f"OFL 允许嵌入：{path.name}",
                "file_path": str(path),
            }
        return {
            "name": policy.name,
            "license_id": policy.license_id,
            "action": "declare",
            "embed": False,
            "notice": None,
            "note": "OFL 允许嵌入，但仓库未提供字体文件，本次只声明 typeface",
        }
    return {
        "name": policy.name,
        "license_id": policy.license_id,
        "action": "system",
        "embed": False,
        "notice": None,
        "note": "系统安全字体，不嵌入",
    }


def collect_typefaces(svg_markup: str) -> list[str]:
    root = _parse_svg(_ensure_svg_xmlns(svg_markup))
    names: list[str] = []
    seen: set[str] = set()
    for elem in root.iter():
        if _local_tag(elem) not in {"text", "tspan"}:
            continue
        family = normalize_family(elem.get("font-family"))
        key = family.lower()
        if not family or key in seen or key in {"sans-serif", "serif", "monospace"}:
            continue
        seen.add(key)
        names.append(family)
    return names


def build_font_report(svgs: list[str], *, font_dir: Path | None = None) -> dict[str, Any]:
    names: list[str] = []
    seen: set[str] = set()
    for markup in svgs:
        for family in collect_typefaces(markup):
            key = family.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(family)
    fonts = [decide_font_action(name, font_dir=font_dir) for name in names]
    for item in fonts:
        if item["embed"] and item["license_id"] == "HarmonyOS-Sans-EULA":
            raise RuntimeError("HarmonyOS Sans 不得嵌入 PPTX")
    notices = [str(item["notice"]) for item in fonts if item.get("notice")]
    return {
        "fonts": fonts,
        "notices": notices,
        "install_required": any(item["action"] == "prompt_install" for item in fonts),
    }
