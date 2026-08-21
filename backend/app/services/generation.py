from __future__ import annotations

from typing import Any

from app.services.clarification import apply_working_title_to_outline, merge_clarification_questions, placeholder_project_title
from app.services.page_images import assert_svg_uses_images, prompt_catalog
from app.services.content_plan import assert_svg_matches_plan, normalize_content_plan
from app.services.model_gateway import ModelGateway
from app.services.prompt_contracts import get_prompt_text, render_prompt
from app.services.search_plan import normalize_query_plan
from app.services.style_cards import normalize_style_cards, pack_from_card
from app.services.style_tokens import style_pack_for_prompt
from app.services.svg import prepare_page_svg

STYLE_PACKS: dict[str, dict[str, Any]] = {
    "minimalism": {
        "style_id": "minimalism",
        "style_name": "极简主义",
        "description": "留白优先，信息秩序清晰，适合正式汇报。",
        "palette": {
            "background": "#FFFFFF",
            "surface": "#F5F5F5",
            "surface_alt": "#EBEEF2",
            "text_primary": "#1F2937",
            "text_secondary": "#6B7280",
            "accent_primary": "#002FA7",
        },
    },
    "consulting": {
        "style_id": "consulting",
        "style_name": "咨询风",
        "description": "结构克制，结论优先，强调信息分层。",
        "palette": {
            "background": "#FFFFFF",
            "surface": "#EEF4F8",
            "surface_alt": "#DCE9F2",
            "text_primary": "#0F172A",
            "text_secondary": "#475569",
            "accent_primary": "#003366",
        },
    },
    "tech-dark": {
        "style_id": "tech-dark",
        "style_name": "科技暗色",
        "description": "偏科技感和对比度，适合技术演示。",
        "palette": {
            "background": "#0B1120",
            "surface": "#111827",
            "surface_alt": "#172033",
            "text_primary": "#F8FAFC",
            "text_secondary": "#94A3B8",
            "accent_primary": "#00E5FF",
        },
    },
    "swiss-style": {
        "style_id": "swiss-style",
        "style_name": "瑞士风格",
        "description": "强网格、少装饰、版式先行。",
        "palette": {
            "background": "#FFFFFF",
            "surface": "#F5F5F5",
            "surface_alt": "#EFE7E7",
            "text_primary": "#111111",
            "text_secondary": "#5E5E5E",
            "accent_primary": "#D90429",
        },
    },
    "brand-blue": {
        "style_id": "brand-blue",
        "style_name": "品牌蓝",
        "description": "明亮专业，适合标准商务汇报。",
        "palette": {
            "background": "#F8FBFF",
            "surface": "#EEF5FF",
            "surface_alt": "#D8E6FF",
            "text_primary": "#14213D",
            "text_secondary": "#486581",
            "accent_primary": "#016BFF",
        },
    },
}

_EMPTY_DATA_UPDATES = {
    "question_patch": None,
    "answer_patch": None,
    "outline_patch": None,
    "page_patch": None,
    "summary_patch": None,
}
_OUTLINE_BODY_KEYS = ("cover", "table_of_contents", "parts", "end_page")


def _looks_like_outline_body(payload: dict[str, Any]) -> bool:
    return any(key in payload for key in _OUTLINE_BODY_KEYS)


def normalize_outline_payload(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise RuntimeError("outline.generate 缺少 ppt_outline")
    nested = result.get("ppt_outline")
    if isinstance(nested, dict):
        return result
    for key in ("PPT_OUTLINE", "outline"):
        alt = result.get(key)
        if not isinstance(alt, dict):
            continue
        if isinstance(alt.get("ppt_outline"), dict):
            return {"ppt_outline": alt["ppt_outline"]}
        if _looks_like_outline_body(alt):
            return {"ppt_outline": alt}
    if _looks_like_outline_body(result):
        return {"ppt_outline": result}
    raise RuntimeError("outline.generate 缺少 ppt_outline")


class GenerationService:
    def __init__(self) -> None:
        self.models = ModelGateway()

    def route_workspace_intent(
        self,
        *,
        router_payload: dict[str, Any],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("workspace.intent_router.system"),
            render_prompt(
                "workspace.intent_router.user",
                {
                    "project_id": router_payload["project_id"],
                    "project_stage": router_payload["project_stage"],
                    "ui_surface": router_payload["ui_surface"],
                    "latest_user_message": router_payload["latest_user_message"],
                    "recent_messages_json": router_payload["recent_messages"],
                    "project_request": router_payload["project_request"],
                    "workflow_constraints_json": router_payload["workflow_constraints"],
                    "fixed_fields_json": router_payload["fixed_fields"],
                    "project_level_status_summary_json": router_payload["project_level_status_summary"],
                    "outline_state_snapshot_json": router_payload["outline_state_snapshot"],
                    "page_context_json": router_payload["page_context"],
                },
            ),
        )
        action_type = str(result.get("action_type") or "reject")
        decision = {
            "scope_type": str(result.get("scope_type") or router_payload.get("default_scope_type") or "project"),
            "target_stage": str(result.get("target_stage") or router_payload["project_stage"]),
            "target_page_id": result.get("target_page_id"),
            "intent_type": str(result.get("intent_type") or action_type),
            "action_type": action_type,
            "should_execute": bool(result.get("should_execute", False)),
            "needs_clarification": bool(result.get("needs_clarification", False)),
            "requires_confirmation": bool(result.get("requires_confirmation", False)),
            "missing_data": self._coerce_string_list(result.get("missing_data")),
            "data_updates": self._normalize_data_updates(result.get("data_updates")),
            "execution_plan": self._normalize_execution_plan(result.get("execution_plan")),
            "next_recommendations": self._normalize_recommendations(result.get("next_recommendations")),
            "reason": str(result.get("reason") or ""),
        }
        if not decision["execution_plan"] and action_type != "reject":
            decision["execution_plan"] = [
                {
                    "step_code": action_type,
                    "step_name": action_type,
                    "reason": decision["reason"] or "按当前动作执行。",
                }
            ]
        return decision

    def generate_project_title(self, request_text: str) -> str:
        return placeholder_project_title(request_text)

    def generate_init_fast_questions(
        self,
        *,
        project_title: str,
        request_text: str,
        init_search_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("init.fast_question_generate.system"),
            render_prompt(
                "init.fast_question_generate.user",
                {
                    "project_title": project_title,
                    "request_text": request_text,
                    "init_search_results_json": init_search_results,
                },
            ),
        )
        page_count_options = self._normalize_page_count_options(result.get("page_count_options"))
        if not page_count_options:
            raise RuntimeError("init.fast_question_generate 返回内容不完整")
        return {
            "page_count_options": page_count_options,
            "ai_questions": merge_clarification_questions(
                self._normalize_questions(result.get("ai_questions")),
                working_title_options=self._coerce_string_list(result.get("working_title_options")),
                request_text=request_text,
            ),
        }

    def refine_init_questions_with_retrieval(
        self,
        *,
        project_title: str,
        request_text: str,
        latest_instruction: str,
        current_questions: list[dict[str, Any]],
        current_page_count_options: list[dict[str, Any]],
        context_digest: list[dict[str, Any]],
        question_patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not context_digest:
            raise RuntimeError("首轮搜索结果为空，不能修订问题")
        result = self.models.context_json(
            get_prompt_text("init.question_refine_with_retrieval.system"),
            render_prompt(
                "init.question_refine_with_retrieval.user",
                {
                    "project_title": project_title,
                    "request_text": request_text,
                    "latest_instruction": latest_instruction,
                    "current_questions_json": current_questions,
                    "current_page_count_options_json": current_page_count_options,
                    "question_patch_json": question_patch or {},
                    "context_digest_json": context_digest,
                },
            ),
        )
        page_count_options = self._normalize_page_count_options(result.get("page_count_options")) or current_page_count_options
        if not page_count_options:
            raise RuntimeError("init.question_refine_with_retrieval 返回内容不完整")
        return {
            "page_count_options": page_count_options,
            "ai_questions": merge_clarification_questions(
                self._normalize_questions(result.get("ai_questions")),
                working_title_options=self._coerce_string_list(result.get("working_title_options")),
                request_text=request_text,
                existing=current_questions,
            ),
        }

    def generate_outline(
        self,
        *,
        project_title: str,
        request_text: str,
        page_count_target: int,
        style_preset: str,
        background_asset_path: str | None,
        answers: dict[str, Any],
        context_digest: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("outline.generate.system"),
            render_prompt(
                "outline.generate.user",
                {
                    "project_title": project_title,
                    "request_text": request_text,
                    "page_count_target_json": page_count_target,
                    "style_preset": style_preset,
                    "background_asset_path": background_asset_path or "",
                    "answers_json": answers,
                    "context_digest_json": context_digest,
                },
            ),
        )
        return apply_working_title_to_outline(normalize_outline_payload(result), answers)

    def generate_page_search_queries(
        self,
        *,
        project_title: str,
        project_request: str,
        page_id: str,
        page_title: str,
        page_bullets: list[str],
        page_section_title: str | None,
        outline_full_snapshot: list[dict[str, Any]],
        latest_instruction: str,
    ) -> list[dict[str, str]]:
        result = self.models.context_json(
            get_prompt_text("page.search_query_expand.system"),
            render_prompt(
                "page.search_query_expand.user",
                {
                    "project_title": project_title,
                    "project_request": project_request,
                    "page_id": page_id,
                    "page_title": page_title,
                    "page_bullets_json": page_bullets,
                    "page_section_title": page_section_title or "",
                    "outline_full_snapshot_json": outline_full_snapshot,
                    "latest_instruction": latest_instruction,
                },
            ),
        )
        queries = self._normalize_search_queries(result)
        if not queries:
            raise RuntimeError("page.search_query_expand 未返回有效搜索词")
        return queries

    def generate_page_outline_patch(
        self,
        *,
        latest_user_message: str,
        page_id: str,
        page_title: str,
        page_bullets: list[str],
        page_section_title: str | None,
        outline_full_snapshot: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("page.outline_patch.system"),
            render_prompt(
                "page.outline_patch.user",
                {
                    "latest_user_message": latest_user_message,
                    "page_id": page_id,
                    "page_title": page_title,
                    "page_bullets_json": page_bullets,
                    "page_section_title": page_section_title or "",
                    "outline_full_snapshot_json": outline_full_snapshot,
                },
            ),
        )
        page_patch = result.get("page_patch")
        if not isinstance(page_patch, dict):
            raise RuntimeError("page.outline_patch 缺少 page_patch")
        title = str(page_patch.get("title") or "").strip()
        bullets = [str(item).strip() for item in page_patch.get("content_outline", []) if str(item).strip()]
        if not title or not bullets:
            raise RuntimeError("page.outline_patch 返回的标题或要点为空")
        return {
            "title": title,
            "content_outline": bullets,
            "section_title": (str(page_patch.get("section_title")).strip() or None)
            if page_patch.get("section_title") is not None
            else None,
            "change_summary": str(page_patch.get("change_summary") or ""),
        }

    def generate_summary_patch(
        self,
        *,
        latest_user_message: str,
        page_title: str,
        page_bullets: list[str],
        current_summary_md: str,
    ) -> dict[str, str]:
        result = self.models.context_json(
            get_prompt_text("page.summary_patch.system"),
            render_prompt(
                "page.summary_patch.user",
                {
                    "latest_user_message": latest_user_message,
                    "page_title": page_title,
                    "page_bullets_json": page_bullets,
                    "current_summary_md": current_summary_md,
                },
            ),
        )
        patch = result.get("summary_patch")
        if not isinstance(patch, dict):
            raise RuntimeError("page.summary_patch 缺少 summary_patch")
        summary_md = str(patch.get("summary_md") or "").strip()
        if not summary_md:
            raise RuntimeError("page.summary_patch 返回空 summary")
        return {"summary_md": summary_md}

    def summarize_selected_sources(
        self,
        *,
        scope_type: str,
        research_goal: str,
        selected_sources: list[dict[str, Any]],
    ) -> dict[str, Any]:
        result = self.models.context_json(
            get_prompt_text("research.summary.system"),
            render_prompt(
                "research.summary.user",
                {
                    "scope_type": scope_type,
                    "research_goal": research_goal,
                    "selected_sources_json": selected_sources,
                },
            ),
        )
        summary_md = str(result.get("summary_md") or "").strip()
        if not summary_md:
            raise RuntimeError("research.summary 缺少 summary_md")
        return {
            "summary_md": summary_md,
            "key_findings": self._coerce_string_list(result.get("key_findings")),
            "open_questions": self._coerce_string_list(result.get("open_questions")),
        }

    def generate_content_plan(self, *, page_context: dict[str, Any]) -> dict[str, Any]:
        page = page_context["page"]
        summary = page_context["summary"]
        page_images = prompt_catalog(page_context.get("page_images"))
        result = self.models.context_json(
            get_prompt_text("plan.page_generate.system"),
            render_prompt(
                "plan.page_generate.user",
                {
                    "page_id": page["page_id"],
                    "page_code": page["page_code"],
                    "page_role": page.get("page_role") or "content",
                    "title": page["title"],
                    "content_outline_json": page["content_outline"],
                    "content_summary": page["content_summary"],
                    "summary_md": summary["summary_md"],
                    "selected_sources_json": summary["selected_sources"],
                    "page_images_json": page_images,
                    "latest_instruction": page_context.get("latest_instruction") or "",
                },
            ),
        )
        return normalize_content_plan(
            result,
            page_code=str(page["page_code"]),
            title=str(page["title"]),
            page_role=str(page.get("page_role") or "content"),
            page_images=page_images,
        )

    def generate_style_cards(
        self,
        *,
        title: str,
        request_text: str,
        style_hint: str,
        outline_cover_title: str,
        page_titles: list[str],
    ) -> list[dict[str, Any]]:
        result = self.models.context_json(
            get_prompt_text("style.card_generate.system"),
            render_prompt(
                "style.card_generate.user",
                {
                    "title": title,
                    "request_text": request_text,
                    "style_hint": style_hint,
                    "outline_cover_title": outline_cover_title,
                    "page_titles_json": page_titles,
                },
            ),
        )
        return normalize_style_cards(result, source="generated")

    def generate_draft_svg(self, *, page_context: dict[str, Any], content_plan: dict[str, Any] | None = None) -> str:
        summary = page_context["summary"]
        page_images = page_context.get("page_images") or []
        svg = self.models.svg_text(
            get_prompt_text("draft.page_generate.system"),
            render_prompt(
                "draft.page_generate.user",
                {
                    "page_id": page_context["page"]["page_id"],
                    "page_code": page_context["page"]["page_code"],
                    "page_brief_version_id": page_context["page"]["page_brief_version_id"],
                    "title": page_context["page"]["title"],
                    "content_outline_json": page_context["page"]["content_outline"],
                    "content_summary": page_context["page"]["content_summary"],
                    "content_plan_json": content_plan or {},
                    "summary_md": summary["summary_md"],
                    "selected_sources_json": summary["selected_sources"],
                    "page_images_json": prompt_catalog(page_images),
                    "latest_instruction": page_context.get("latest_instruction") or "",
                },
            ),
            role="draft",
        )
        prepared = prepare_page_svg(svg, stage="draft", page_images=page_images)
        assert_svg_matches_plan(prepared, content_plan)
        assert_svg_uses_images(prepared, page_images, (content_plan or {}).get("image_slots"))
        return prepared

    def generate_design_svg(
        self,
        *,
        draft_svg: str,
        style_pack_id: str,
        background_asset_path: str | None,
        chrome: dict[str, Any] | None = None,
        content_plan: dict[str, Any] | None = None,
        frozen_card: dict[str, Any] | None = None,
        page_images: list[dict[str, Any]] | None = None,
    ) -> str:
        style_pack = self.get_style_pack(style_pack_id, frozen_card=frozen_card)
        svg = self.models.svg_text(
            get_prompt_text("design.svg_generate.system"),
            render_prompt(
                "design.svg_generate.user",
                {
                    "draft_svg_markup": draft_svg,
                    "content_plan_json": content_plan or {},
                    "page_images_json": prompt_catalog(page_images),
                    "style_pack_json": style_pack_for_prompt(style_pack),
                    "background_asset_json": {
                        "composited_by_system": True,
                        "note": "系统会在 SVG 底层嵌入底色、纹理、标题栏和页码。不要引用本地文件路径，不要再画一层全幅背景，并保证正文不透明、可读。",
                    }
                    if background_asset_path
                    else {
                        "composited_by_system": True,
                        "note": "系统会在 SVG 底层嵌入底色、标题栏和页码。不要再画一层全幅背景。",
                    },
                },
            ),
            role="design",
        )
        prepared = prepare_page_svg(
            svg,
            background_path=background_asset_path,
            stage="design",
            style_pack=style_pack,
            chrome=chrome,
            page_images=page_images,
        )
        assert_svg_matches_plan(prepared, content_plan)
        assert_svg_uses_images(prepared, page_images, (content_plan or {}).get("image_slots"))
        return prepared

    def get_style_pack(self, style_id: str | None, *, frozen_card: dict[str, Any] | None = None) -> dict[str, Any]:
        if frozen_card:
            return pack_from_card(frozen_card)
        if not style_id:
            raise RuntimeError("style_pack_id 不能为空")
        style_pack = STYLE_PACKS.get(style_id)
        if style_pack is None:
            style_hint = str(style_id).strip()
            style_pack = {
                "style_id": "custom",
                "style_name": "自定义风格",
                "description": f"用户自定义风格要求：{style_hint}",
                "palette": {
                    "background": "#F8FAFC",
                    "surface": "#FFFFFF",
                    "surface_alt": "#E2E8F0",
                    "text_primary": "#0F172A",
                    "text_secondary": "#475569",
                    "accent_primary": "#2563EB",
                },
            }
        return style_pack

    def list_style_options(self) -> list[dict[str, Any]]:
        return [STYLE_PACKS[key] for key in STYLE_PACKS]

    def _normalize_search_queries(self, payload: Any) -> list[dict[str, str]]:
        return normalize_query_plan(payload)

    def _normalize_page_count_options(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        options: list[dict[str, Any]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            label = str(raw_item.get("label") or "").strip()
            reason = str(raw_item.get("reason") or "").strip()
            page_count = raw_item.get("page_count")
            if not label or not isinstance(page_count, int):
                continue
            options.append(
                {
                    "option_code": str(raw_item.get("option_code") or f"OPT-{len(options) + 1}"),
                    "label": label,
                    "page_count": page_count,
                    "reason": reason,
                }
            )
        return options

    def _normalize_questions(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        questions: list[dict[str, Any]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            question_code = str(raw_item.get("question_code") or "").strip()
            label = str(raw_item.get("label") or "").strip()
            if not question_code or not label:
                continue
            options = []
            for option in raw_item.get("options", []):
                if not isinstance(option, dict):
                    continue
                option_label = str(option.get("label") or "").strip()
                if not option_label:
                    continue
                options.append(
                    {
                        "option_code": str(option.get("option_code") or f"OPT-{len(options) + 1}"),
                        "label": option_label,
                    }
                )
            if len(options) != 3:
                continue
            questions.append(
                {
                    "question_code": question_code,
                    "label": label,
                    "description": str(raw_item.get("description") or "").strip(),
                    "options": options,
                    "allow_custom": bool(raw_item.get("allow_custom", True)),
                }
            )
        return questions

    def _normalize_execution_plan(self, payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        plan: list[dict[str, str]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            step_code = str(raw_item.get("step_code") or "").strip()
            step_name = str(raw_item.get("step_name") or step_code).strip()
            reason = str(raw_item.get("reason") or "").strip()
            if not step_code:
                continue
            plan.append(
                {
                    "step_code": step_code,
                    "step_name": step_name,
                    "reason": reason,
                }
            )
        return plan

    def _normalize_recommendations(self, payload: Any) -> list[dict[str, str]]:
        if not isinstance(payload, list):
            return []
        recommendations: list[dict[str, str]] = []
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                continue
            code = str(raw_item.get("code") or "").strip()
            label = str(raw_item.get("label") or "").strip()
            reason = str(raw_item.get("reason") or "").strip()
            if code and label:
                recommendations.append({"code": code, "label": label, "reason": reason})
        return recommendations

    def _coerce_string_list(self, payload: Any) -> list[str]:
        if not isinstance(payload, list):
            return []
        return [str(item).strip() for item in payload if str(item).strip()]

    def _normalize_data_updates(self, payload: Any) -> dict[str, Any]:
        normalized = dict(_EMPTY_DATA_UPDATES)
        if not isinstance(payload, dict):
            return normalized
        for key in normalized:
            if key in payload:
                normalized[key] = payload[key]
        return normalized
