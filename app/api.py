from __future__ import annotations

from aiohttp import web

from app.db.repository import Repository
from app.services.analysis_engine import AnalysisEngine
from app.services.word_analysis import task_type_to_short


def create_api_app(repo: Repository) -> web.Application:
    app = web.Application()
    analysis = AnalysisEngine(repo)

    async def post_word(request: web.Request) -> web.Response:
        payload = await request.json()
        user_id = int(payload["user_id"])
        word = str(payload["word"]).strip()
        task_type = str(payload.get("task_type", ""))
        task_id = str(payload.get("task_id", ""))
        age_group = payload.get("age_group")
        if not word or len(word) > 50:
            return web.json_response({"error": "word must be 1..50 chars"}, status=400)
        await repo.save_user_word(
            user_id=user_id,
            word=word,
            task_type=task_type_to_short(task_type),
            task_id=task_id,
            age_group=age_group,
        )
        return web.json_response({"ok": True})

    async def get_report(request: web.Request) -> web.Response:
        user_id = int(request.query["user_id"])
        result = await analysis.analyze_user(user_id)
        return web.json_response(
            {
                "freq": result["freq"],
                "dominant_cluster": result["dominant_cluster"],
                "task_correlation": result["task_correlation"],
                "trend": result["trend"],
            }
        )

    app.router.add_post("/api/word", post_word)
    app.router.add_get("/api/report", get_report)
    return app
