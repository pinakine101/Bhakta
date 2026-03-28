from __future__ import annotations

from collections import Counter
from datetime import datetime

from app.db.repository import Repository
from app.services.word_analysis import calc_trend, correlation_by_task, dominant_cluster, top_words


class AnalysisEngine:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    async def analyze_user(self, user_id: int) -> dict:
        week_rows = await self.repo.get_user_weekly_word_records(user_id, week_shift=0)
        prev_rows = await self.repo.get_user_weekly_word_records(user_id, week_shift=1)

        week_words = [row[0] for row in week_rows]
        prev_words = [row[0] for row in prev_rows]
        week_clusters = [row[4] for row in week_rows if row[3] == 1 and row[4]]
        prev_clusters = [row[4] for row in prev_rows if row[3] == 1 and row[4]]

        top = top_words(week_words, limit=5)
        dom = dominant_cluster(week_clusters)
        trend = calc_trend(len(week_words), len(prev_words))

        task_cluster_pairs = [(row[1], row[4]) for row in week_rows if row[3] == 1 and row[4]]
        task_corr = correlation_by_task(task_cluster_pairs)

        responsive_tasks: list[str] = []
        avoidant_tasks: list[str] = []
        for task_type, clusters in task_corr.items():
            if clusters.get("покой", 0) >= 2:
                responsive_tasks.append(task_type)
            if clusters.get("сопротивление", 0) >= 2:
                avoidant_tasks.append(task_type)

        await self.repo.update_user_profile_analysis(
            user_id=user_id,
            dominant_cluster=dom,
            trend=trend,
            responsive_tasks=sorted(set(responsive_tasks)),
            avoidant_tasks=sorted(set(avoidant_tasks)),
            last_analysis=datetime.utcnow().isoformat(sep=" "),
        )

        return {
            "freq": dict(top),
            "dominant_cluster": dom,
            "task_correlation": task_corr,
            "trend": trend,
            "week_word_count": len(week_words),
            "cluster_counts": dict(Counter(week_clusters)),
        }
