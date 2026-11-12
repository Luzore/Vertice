from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import IntegrityError, StatementError

from db import migrate
from db import session as db_session
from db.models import (
    Alert,
    Base,
    Candidate,
    ControlResult,
    Idea,
    Lesson,
    Position,
    Run,
)
from db.session import session_scope

NOW = datetime(2026, 11, 10, 15, 0, tzinfo=UTC)

# ROADMAP section 15.
EXPECTED_TABLES = {
    "universe",
    "raw_snapshots",
    "bars_daily",
    "bars_intraday",
    "news",
    "macro_points",
    "econ_events",
    "corporate_events",
    "indicator_values",
    "runs",
    "candidates",
    "control_results",
    "ai_calls",
    "ideas",
    "positions",
    "fills",
    "confirmations",
    "alerts",
    "state_transitions",
    "lessons",
    "lesson_cases",
    "metrics_daily",
    "system_status",
}


# --- schema and connection settings -----------------------------------------------


def test_migrations_create_every_table_in_the_roadmap(engine):
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}

    assert tables == EXPECTED_TABLES


def test_models_and_migrations_describe_the_same_schema(engine):
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_database_is_at_the_newest_revision(engine):
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()

    assert current == migrate.head_revision() == "0003"


def test_migrations_can_be_undone(engine, db_url):
    engine.dispose()

    migrate.downgrade(db_url, "base")

    remaining = set(inspect(db_session.make_engine(db_url)).get_table_names())
    assert remaining == {"alembic_version"}


def test_sqlite_runs_in_wal_mode_with_foreign_keys(engine):
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_foreign_keys_are_enforced(engine):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(ControlResult(candidate_id=999, control="C03", result="pass"))


# --- principle 9: a lesson needs evidence before it can be active ----------------

INSERT_LESSON = text(
    "INSERT INTO lessons "
    "(id, kind, statement, scope, test, effect, status, "
    " cases_total, cases_forward, confidence, proposed_at) "
    "VALUES (:id, 'pattern', 'statement', '{}', '{}', '{}', :status, "
    " :cases_total, :cases_forward, :confidence, '2026-11-10T15:00:00.000000Z')"
)


def _insert_lesson_with_sql(engine, **values) -> None:
    """Raw SQL on purpose: the rule must hold without any application code."""
    params = {"id": "L-001", "status": "active", **values}
    with engine.begin() as connection:
        connection.execute(INSERT_LESSON, params)


def test_active_lesson_with_19_cases_is_rejected_by_the_database(engine):
    with pytest.raises(IntegrityError, match="ck_lessons_active_requires_evidence"):
        _insert_lesson_with_sql(engine, cases_total=19, cases_forward=19, confidence=0.95)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT COUNT(*) FROM lessons")).scalar() == 0


def test_active_lesson_with_20_cases_is_accepted(engine):
    _insert_lesson_with_sql(engine, cases_total=20, cases_forward=10, confidence=0.60)

    with engine.connect() as connection:
        assert connection.execute(text("SELECT status FROM lessons")).scalar() == "active"


@pytest.mark.parametrize(
    ("cases_total", "cases_forward", "confidence", "missing"),
    [
        (19, 10, 0.90, "one case short"),
        (20, 9, 0.90, "one forward case short"),
        (20, 10, 0.59, "confidence just below 0.60"),
        (500, 0, 1.00, "many cases but none after the hypothesis was written"),
        (0, 0, 0.0, "no evidence at all"),
    ],
)
def test_active_lesson_without_enough_evidence_is_rejected(
    engine, cases_total, cases_forward, confidence, missing
):
    with pytest.raises(IntegrityError, match="ck_lessons_active_requires_evidence"):
        _insert_lesson_with_sql(
            engine, cases_total=cases_total, cases_forward=cases_forward, confidence=confidence
        )


@pytest.mark.parametrize("status", ["hypothesis", "refuted", "retired"])
def test_other_statuses_need_no_evidence(engine, status):
    _insert_lesson_with_sql(engine, status=status, cases_total=0, cases_forward=0, confidence=0)


def test_hypothesis_cannot_be_promoted_without_evidence(engine):
    _insert_lesson_with_sql(
        engine, status="hypothesis", cases_total=19, cases_forward=12, confidence=0.80
    )

    with (
        pytest.raises(IntegrityError, match="ck_lessons_active_requires_evidence"),
        engine.begin() as connection,
    ):
        connection.execute(text("UPDATE lessons SET status = 'active' WHERE id = 'L-001'"))

    with engine.begin() as connection:
        connection.execute(text("UPDATE lessons SET cases_total = 20 WHERE id = 'L-001'"))
        connection.execute(text("UPDATE lessons SET status = 'active' WHERE id = 'L-001'"))


def test_active_lesson_cannot_lose_its_evidence(engine):
    _insert_lesson_with_sql(engine, cases_total=25, cases_forward=12, confidence=0.70)

    with (
        pytest.raises(IntegrityError, match="ck_lessons_active_requires_evidence"),
        engine.begin() as connection,
    ):
        connection.execute(text("UPDATE lessons SET confidence = 0.40 WHERE id = 'L-001'"))


def test_application_code_cannot_bypass_the_rule_either(engine):
    lesson = Lesson(
        id="L-002",
        kind="pattern",
        statement="Breakouts in high-risk regimes hit the stop more often than the target.",
        scope={"setup": "RUPTURA", "regime": "RIESGO_ALTO"},
        test={"metric": "hit_stop_before_target", "holds_if": "== true"},
        effect={"type": "screener_weight", "component": "setup_quality", "delta": -5},
        status="active",
        cases_total=19,
        cases_forward=19,
        confidence=0.99,
        proposed_at=NOW,
    )

    with (
        pytest.raises(IntegrityError, match="ck_lessons_active_requires_evidence"),
        session_scope(engine) as session,
    ):
        session.add(lesson)


def test_lesson_round_trips_with_its_json_fields(engine):
    with session_scope(engine) as session:
        session.add(
            Lesson(
                id="L-003",
                kind="own_error",
                statement="Entradas con gap pierden más.",
                scope={"setup": "CATALIZADOR"},
                test={"metric": "hit_stop_before_target", "holds_if": "== true"},
                effect={"type": "dossier_text"},
                status="hypothesis",
                proposed_at=NOW,
            )
        )

    with session_scope(engine) as session:
        lesson = session.get(Lesson, "L-003")
        assert lesson.scope == {"setup": "CATALIZADOR"}
        assert lesson.statement == "Entradas con gap pierden más."
        assert (lesson.cases_total, lesson.cases_forward, lesson.confidence) == (0, 0, 0.0)
        assert lesson.proposed_at == NOW
        assert lesson.activated_at is None


@pytest.mark.parametrize(
    ("column", "value", "constraint"),
    [
        ("kind", "'hunch'", "ck_lessons_kind_valid"),
        ("status", "'confirmed'", "ck_lessons_status_valid"),
        ("confidence", "1.2", "ck_lessons_confidence_range"),
        ("cases_forward", "30", "ck_lessons_case_counts_consistent"),
    ],
)
def test_lesson_rejects_invalid_values(engine, column, value, constraint):
    _insert_lesson_with_sql(
        engine, status="hypothesis", cases_total=20, cases_forward=10, confidence=0.7
    )

    with pytest.raises(IntegrityError, match=constraint), engine.begin() as connection:
        connection.execute(text(f"UPDATE lessons SET {column} = {value} WHERE id = 'L-001'"))


# --- time is stored in UTC (principle 11) ------------------------------------------


def test_instants_are_stored_in_utc_and_come_back_aware(engine):
    monterrey_morning = datetime(2026, 11, 10, 8, 0, tzinfo=timezone(timedelta(hours=-6)))

    with session_scope(engine) as session:
        session.add(Run(kind="premarket", as_of=monterrey_morning, started_at=monterrey_morning))

    with engine.connect() as connection:
        stored = connection.execute(text("SELECT as_of FROM runs")).scalar()
    assert stored == "2026-11-10T14:00:00.000000Z"

    with session_scope(engine) as session:
        run = session.scalars(select(Run)).one()
        assert run.as_of == datetime(2026, 11, 10, 14, 0, tzinfo=UTC)
        assert run.as_of.tzinfo is UTC


def test_naive_datetimes_are_refused(engine):
    naive = datetime(2026, 11, 10, 8, 0)  # noqa: DTZ001 - naive on purpose

    with pytest.raises(StatementError, match="naive datetime"), session_scope(engine) as session:
        session.add(Run(kind="premarket", as_of=naive, started_at=naive))


def test_stored_instants_sort_in_time_order(engine):
    moments = [NOW + timedelta(hours=offset) for offset in (30, -2, 0, 9)]

    with session_scope(engine) as session:
        session.add_all(Run(kind="intraday", as_of=m, started_at=m) for m in moments)

    with session_scope(engine) as session:
        ordered = session.scalars(select(Run.as_of).order_by(Run.as_of)).all()
        recent = session.scalars(select(Run.as_of).where(Run.as_of > NOW)).all()

    assert ordered == sorted(moments)
    assert sorted(recent) == [NOW + timedelta(hours=9), NOW + timedelta(hours=30)]


# --- other rules the database enforces ----------------------------------------------


def _make_position(session, *, state: str = "OPEN") -> Position:
    run = Run(kind="premarket", as_of=NOW, started_at=NOW)
    session.add(run)
    session.flush()
    idea = Idea(
        run_id=run.id, plan_date=date(2026, 11, 10), ticker="XYZ", verdict="COMPRAR", created_at=NOW
    )
    session.add(idea)
    session.flush()
    position = Position(
        idea_id=idea.id,
        is_shadow=False,
        ticker="XYZ",
        qty_open=24,
        avg_price=48.25,
        stop=46.70,
        targets=[51.05, 52.90],
        initial_r=1.55,
        state=state,
        opened_at=NOW,
    )
    session.add(position)
    session.flush()
    return position


def test_new_idea_defaults(engine):
    with session_scope(engine) as session:
        idea_id = _make_position(session).idea_id

    with session_scope(engine) as session:
        idea = session.get(Idea, idea_id)
        assert idea.state == "PROPOSED"
        assert idea.lessons_applied == []
        assert idea.no_ai is False


@pytest.mark.parametrize(
    ("model", "values"),
    [
        (Run, {"kind": "overnight", "as_of": NOW, "started_at": NOW}),
        (Run, {"kind": "premarket", "as_of": NOW, "started_at": NOW, "regime": "BAJISTA"}),
    ],
)
def test_unknown_vocabulary_is_rejected(engine, model, values):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(model(**values))


@pytest.mark.parametrize(
    ("field", "value"),
    [("verdict", "VENDER"), ("state", "FILLED"), ("setup", "CORTO"), ("prob_raw", 1.4)],
)
def test_idea_rejects_invalid_values(engine, field, value):
    with pytest.raises(IntegrityError), session_scope(engine) as session:
        run = Run(kind="premarket", as_of=NOW, started_at=NOW)
        session.add(run)
        session.flush()
        values = {
            "run_id": run.id,
            "plan_date": date(2026, 11, 10),
            "ticker": "XYZ",
            "verdict": "COMPRAR",
            "created_at": NOW,
            field: value,
        }
        session.add(Idea(**values))


def test_candidate_score_must_be_between_0_and_100(engine):
    with (
        pytest.raises(IntegrityError, match="ck_candidates_score_range"),
        session_scope(engine) as session,
    ):
        run = Run(kind="premarket", as_of=NOW, started_at=NOW)
        session.add(run)
        session.flush()
        session.add(Candidate(run_id=run.id, ticker="XYZ", score=120, breakdown={}))


def test_only_one_open_alert_per_position_and_rule(engine):
    with session_scope(engine) as session:
        position = _make_position(session)
        session.add(
            Alert(position_id=position.id, rule="stop", urgency="ALTA", message="m", created_at=NOW)
        )
        position_id = position.id

    with pytest.raises(IntegrityError), session_scope(engine) as session:
        session.add(
            Alert(position_id=position_id, rule="stop", urgency="ALTA", message="m", created_at=NOW)
        )

    # A different rule, or the same rule once the first alert is resolved, is fine.
    with session_scope(engine) as session:
        session.add(
            Alert(
                position_id=position_id,
                rule="target_1",
                urgency="MEDIA",
                message="m",
                created_at=NOW,
            )
        )
        first = session.scalars(select(Alert).where(Alert.rule == "stop")).one()
        first.resolved_at = NOW + timedelta(minutes=5)
        first.resolution = "sold"

    with session_scope(engine) as session:
        session.add(
            Alert(position_id=position_id, rule="stop", urgency="ALTA", message="m", created_at=NOW)
        )


def test_closed_position_needs_a_close_time_and_no_quantity(engine):
    with (
        pytest.raises(IntegrityError, match="ck_positions_closed_has_close_time"),
        session_scope(engine) as session,
    ):
        _make_position(session, state="CLOSED")

    with (
        pytest.raises(IntegrityError, match="ck_positions_closed_has_no_quantity"),
        session_scope(engine) as session,
    ):
        position = _make_position(session)
        position.state = "CLOSED"
        position.closed_at = NOW + timedelta(days=2)

    with session_scope(engine) as session:
        position = _make_position(session)
        position.state = "CLOSED"
        position.closed_at = NOW + timedelta(days=2)
        position.qty_open = 0
