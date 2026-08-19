from __future__ import annotations

from app.services.tasks import _batch_page_eligible, enqueue_batch_action
from tests.helpers import make_content_page, make_project


def test_batch_search_skips_ready_pages(db_session):
    project = make_project(db_session, stage="search")
    ready = make_content_page(db_session, project, page_code="page-03", title="已有资料", sort_order=1)
    ready.search_status = "ready"
    ready.page_corpus_digest_json = {"document_count": 2}
    empty = make_content_page(db_session, project, page_code="page-04", title="空页", sort_order=2)
    empty.search_status = "empty"
    empty.page_corpus_digest_json = {"document_count": 0}
    db_session.commit()

    assert _batch_page_eligible(ready, "project_batch_search") is False
    assert _batch_page_eligible(empty, "project_batch_search") is True

    tasks = enqueue_batch_action(
        db_session,
        project_id=project.id,
        action_type="project_batch_search",
        agent_run_id="run-batch",
    )
    db_session.commit()
    assert [task.page_id for task in tasks] == [empty.id]


def test_batch_summary_skips_ready_but_reruns_stale(db_session):
    project = make_project(db_session, stage="search")
    ready = make_content_page(db_session, project, page_code="page-03", title="ready", sort_order=1)
    ready.summary_status = "ready"
    stale = make_content_page(db_session, project, page_code="page-04", title="stale", sort_order=2)
    stale.summary_status = "stale"
    db_session.commit()

    assert _batch_page_eligible(ready, "project_batch_summary") is False
    assert _batch_page_eligible(stale, "project_batch_summary") is True
