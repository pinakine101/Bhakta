import pytest

from app.db.database import init_db
from app.db.repository import Repository
from app.services.analysis_engine import AnalysisEngine
from app.services.word_analysis import build_default_word_dict_rows


@pytest.mark.asyncio
async def test_save_word_and_report_shape(tmp_path) -> None:
    db_path = str(tmp_path / "words.sqlite3")
    await init_db(db_path)
    repo = Repository(db_path)
    await repo.ensure_user(500, "word_user")
    await repo.ensure_analysis_profile(500)
    await repo.seed_word_dictionary(build_default_word_dict_rows())

    await repo.save_user_word(500, "тишина", "t1", "d_breath_5")
    await repo.save_user_word(500, "тревога", "t2", "f_no_sugar_48")
    await repo.save_user_word(500, "неизвестноеслово", "t3", "r_focus_sprint")

    analysis = AnalysisEngine(repo)
    result = await analysis.analyze_user(500)

    assert "freq" in result
    assert "dominant_cluster" in result
    assert "task_correlation" in result
    assert "trend" in result
