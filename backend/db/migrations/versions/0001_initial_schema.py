"""initial schema

Revision ID: 0001
Revises: -
Create Date: 2026-11-10 19:53:29.380328
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('bars_daily',
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('session_date', sa.Date(), nullable=False),
    sa.Column('open', sa.Float(), nullable=False),
    sa.Column('high', sa.Float(), nullable=False),
    sa.Column('low', sa.Float(), nullable=False),
    sa.Column('close', sa.Float(), nullable=False),
    sa.Column('volume', sa.Float(), nullable=False),
    sa.Column('vwap', sa.Float(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('ticker', 'session_date', name=op.f('pk_bars_daily'))
    )
    op.create_table('bars_intraday',
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('timeframe', sa.Text(), nullable=False),
    sa.Column('ts', sa.Text(), nullable=False),
    sa.Column('open', sa.Float(), nullable=False),
    sa.Column('high', sa.Float(), nullable=False),
    sa.Column('low', sa.Float(), nullable=False),
    sa.Column('close', sa.Float(), nullable=False),
    sa.Column('volume', sa.Float(), nullable=False),
    sa.Column('vwap', sa.Float(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('ticker', 'timeframe', 'ts', name=op.f('pk_bars_intraday'))
    )
    op.create_table('corporate_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('event_date', sa.Date(), nullable=False),
    sa.Column('event_at', sa.Text(), nullable=True),
    sa.Column('payload', sa.Text(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('fetched_at', sa.Text(), nullable=False),
    sa.CheckConstraint("event_type IN ('earnings','filing_8k','form_4','offering','split')", name=op.f('ck_corporate_events_event_type_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_corporate_events'))
    )
    with op.batch_alter_table('corporate_events', schema=None) as batch_op:
        batch_op.create_index('ix_corporate_events_ticker_event_date', ['ticker', 'event_date'], unique=False)

    op.create_table('econ_events',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('country', sa.Text(), nullable=False),
    sa.Column('scheduled_at', sa.Text(), nullable=False),
    sa.Column('impact', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('fetched_at', sa.Text(), nullable=False),
    sa.CheckConstraint("impact IN ('high','medium','low')", name=op.f('ck_econ_events_impact_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_econ_events')),
    sa.UniqueConstraint('name', 'country', 'scheduled_at', name=op.f('uq_econ_events_name_country_scheduled_at'))
    )
    with op.batch_alter_table('econ_events', schema=None) as batch_op:
        batch_op.create_index('ix_econ_events_scheduled_at', ['scheduled_at'], unique=False)

    op.create_table('indicator_values',
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('as_of', sa.Text(), nullable=False),
    sa.Column('metric', sa.Text(), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('ticker', 'as_of', 'metric', name=op.f('pk_indicator_values'))
    )
    op.create_table('lessons',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('statement', sa.Text(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('test', sa.Text(), nullable=False),
    sa.Column('effect', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('cases_total', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('cases_forward', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('confidence', sa.Float(), server_default=sa.text('0'), nullable=False),
    sa.Column('proposed_at', sa.Text(), nullable=False),
    sa.Column('activated_at', sa.Text(), nullable=True),
    sa.CheckConstraint("kind IN ('pattern','own_error','reasoning')", name=op.f('ck_lessons_kind_valid')),
    sa.CheckConstraint("status <> 'active' OR (cases_total >= 20 AND cases_forward >= 10 AND confidence >= 0.60)", name=op.f('ck_lessons_active_requires_evidence')),
    sa.CheckConstraint("status IN ('hypothesis','active','refuted','retired')", name=op.f('ck_lessons_status_valid')),
    sa.CheckConstraint('cases_total >= 0 AND cases_forward >= 0 AND cases_forward <= cases_total', name=op.f('ck_lessons_case_counts_consistent')),
    sa.CheckConstraint('confidence BETWEEN 0 AND 1', name=op.f('ck_lessons_confidence_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lessons'))
    )
    op.create_table('macro_points',
    sa.Column('series_id', sa.Text(), nullable=False),
    sa.Column('obs_date', sa.Date(), nullable=False),
    sa.Column('value', sa.Float(), nullable=True),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('fetched_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('series_id', 'obs_date', name=op.f('pk_macro_points'))
    )
    op.create_table('metrics_daily',
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('equity_real', sa.Float(), nullable=True),
    sa.Column('equity_system', sa.Float(), nullable=True),
    sa.Column('spy_close', sa.Float(), nullable=True),
    sa.Column('pnl', sa.Float(), nullable=True),
    sa.Column('drawdown', sa.Float(), nullable=True),
    sa.Column('brier', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('day', name=op.f('pk_metrics_daily'))
    )
    op.create_table('news',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tickers', sa.Text(), nullable=False),
    sa.Column('published_at', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('headline', sa.Text(), nullable=False),
    sa.Column('summary', sa.Text(), nullable=True),
    sa.Column('url', sa.Text(), nullable=True),
    sa.Column('sentiment', sa.Float(), nullable=True),
    sa.Column('category', sa.Text(), nullable=True),
    sa.Column('severe', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('fetched_at', sa.Text(), nullable=False),
    sa.CheckConstraint('sentiment IS NULL OR sentiment BETWEEN -1 AND 1', name=op.f('ck_news_sentiment_range')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_news')),
    sa.UniqueConstraint('url', name=op.f('uq_news_url'))
    )
    with op.batch_alter_table('news', schema=None) as batch_op:
        batch_op.create_index('ix_news_published_at', ['published_at'], unique=False)

    op.create_table('raw_snapshots',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('dataset', sa.Text(), nullable=False),
    sa.Column('as_of', sa.Text(), nullable=False),
    sa.Column('fetched_at', sa.Text(), nullable=False),
    sa.Column('latency_ms', sa.Integer(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('payload', sa.Text(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('ok','slow','error','stale')", name=op.f('ck_raw_snapshots_status_valid')),
    sa.CheckConstraint('latency_ms IS NULL OR latency_ms >= 0', name=op.f('ck_raw_snapshots_latency_non_negative')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_raw_snapshots'))
    )
    with op.batch_alter_table('raw_snapshots', schema=None) as batch_op:
        batch_op.create_index('ix_raw_snapshots_fetched_at', ['fetched_at'], unique=False)
        batch_op.create_index('ix_raw_snapshots_source_dataset_as_of', ['source', 'dataset', 'as_of'], unique=False)

    op.create_table('runs',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('as_of', sa.Text(), nullable=False),
    sa.Column('started_at', sa.Text(), nullable=False),
    sa.Column('finished_at', sa.Text(), nullable=True),
    sa.Column('regime', sa.Text(), nullable=True),
    sa.Column('stats', sa.Text(), nullable=True),
    sa.CheckConstraint("kind IN ('premarket','intraday','close','replay')", name=op.f('ck_runs_kind_valid')),
    sa.CheckConstraint("regime IS NULL OR regime IN ('ALCISTA','NEUTRAL','RIESGO_ALTO')", name=op.f('ck_runs_regime_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_runs'))
    )
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.create_index('ix_runs_as_of', ['as_of'], unique=False)

    op.create_table('state_transitions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('entity', sa.Text(), nullable=False),
    sa.Column('entity_id', sa.Text(), nullable=False),
    sa.Column('from_state', sa.Text(), nullable=True),
    sa.Column('to_state', sa.Text(), nullable=False),
    sa.Column('at', sa.Text(), nullable=False),
    sa.Column('actor', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.CheckConstraint("actor IN ('system','user')", name=op.f('ck_state_transitions_actor_valid')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_state_transitions'))
    )
    with op.batch_alter_table('state_transitions', schema=None) as batch_op:
        batch_op.create_index('ix_state_transitions_entity', ['entity', 'entity_id'], unique=False)

    op.create_table('system_status',
    sa.Column('component', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('component', name=op.f('pk_system_status'))
    )
    op.create_table('universe',
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('name', sa.Text(), nullable=False),
    sa.Column('sector', sa.Text(), nullable=True),
    sa.Column('exchange', sa.Text(), nullable=False),
    sa.Column('sic_listed', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('active', sa.Boolean(), server_default=sa.text('1'), nullable=False),
    sa.Column('updated_at', sa.Text(), nullable=False),
    sa.PrimaryKeyConstraint('ticker', name=op.f('pk_universe'))
    )
    op.create_table('ai_calls',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=True),
    sa.Column('task', sa.Text(), nullable=False),
    sa.Column('model', sa.Text(), nullable=False),
    sa.Column('dossier_hash', sa.Text(), nullable=False),
    sa.Column('dossier_path', sa.Text(), nullable=True),
    sa.Column('raw_response', sa.Text(), nullable=True),
    sa.Column('structured_output', sa.Text(), nullable=True),
    sa.Column('usage', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('session_id', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.CheckConstraint('cost_usd IS NULL OR cost_usd >= 0', name=op.f('ck_ai_calls_cost_non_negative')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_ai_calls_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ai_calls'))
    )
    with op.batch_alter_table('ai_calls', schema=None) as batch_op:
        batch_op.create_index('ix_ai_calls_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_ai_calls_dossier_hash', ['dossier_hash'], unique=False)

    op.create_table('candidates',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('setup', sa.Text(), nullable=True),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('breakdown', sa.Text(), nullable=False),
    sa.CheckConstraint("setup IS NULL OR setup IN ('RUPTURA','PULLBACK','CATALIZADOR','REBOTE')", name=op.f('ck_candidates_setup_valid')),
    sa.CheckConstraint('score BETWEEN 0 AND 100', name=op.f('ck_candidates_score_range')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_candidates_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_candidates')),
    sa.UniqueConstraint('run_id', 'ticker', name=op.f('uq_candidates_run_id_ticker'))
    )
    op.create_table('control_results',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=False),
    sa.Column('control', sa.Text(), nullable=False),
    sa.Column('result', sa.Text(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=True),
    sa.CheckConstraint("result IN ('pass','fail','warn')", name=op.f('ck_control_results_result_valid')),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], name=op.f('fk_control_results_candidate_id_candidates')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_control_results')),
    sa.UniqueConstraint('candidate_id', 'control', name=op.f('uq_control_results_candidate_id_control'))
    )
    op.create_table('ideas',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Integer(), nullable=False),
    sa.Column('candidate_id', sa.Integer(), nullable=True),
    sa.Column('ai_call_id', sa.Integer(), nullable=True),
    sa.Column('plan_date', sa.Date(), nullable=False),
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('setup', sa.Text(), nullable=True),
    sa.Column('score', sa.Float(), nullable=True),
    sa.Column('verdict', sa.Text(), nullable=False),
    sa.Column('no_ai', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    sa.Column('prob_raw', sa.Float(), nullable=True),
    sa.Column('prob_calibrated', sa.Float(), nullable=True),
    sa.Column('entry_type', sa.Text(), nullable=True),
    sa.Column('entry_min', sa.Float(), nullable=True),
    sa.Column('entry_max', sa.Float(), nullable=True),
    sa.Column('no_chase_above', sa.Float(), nullable=True),
    sa.Column('watch_trigger', sa.Text(), nullable=True),
    sa.Column('stop', sa.Float(), nullable=True),
    sa.Column('targets', sa.Text(), nullable=True),
    sa.Column('horizon_days', sa.Integer(), nullable=True),
    sa.Column('shares', sa.Float(), nullable=True),
    sa.Column('amount_usd', sa.Float(), nullable=True),
    sa.Column('risk_usd', sa.Float(), nullable=True),
    sa.Column('ev_r', sa.Float(), nullable=True),
    sa.Column('thesis', sa.Text(), nullable=True),
    sa.Column('reasons', sa.Text(), nullable=True),
    sa.Column('risks', sa.Text(), nullable=True),
    sa.Column('invalidation', sa.Text(), nullable=True),
    sa.Column('lessons_applied', sa.Text(), server_default=sa.text("'[]'"), nullable=False),
    sa.Column('state', sa.Text(), server_default=sa.text("'PROPOSED'"), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.CheckConstraint("setup IS NULL OR setup IN ('RUPTURA','PULLBACK','CATALIZADOR','REBOTE')", name=op.f('ck_ideas_setup_valid')),
    sa.CheckConstraint("state IN ('PROPOSED','BOUGHT','DECLINED','SHADOW','EXPIRED','NOT_FILLABLE')", name=op.f('ck_ideas_state_valid')),
    sa.CheckConstraint("verdict IN ('COMPRAR','VIGILAR','NO_HOY')", name=op.f('ck_ideas_verdict_valid')),
    sa.CheckConstraint('prob_calibrated IS NULL OR prob_calibrated BETWEEN 0 AND 1', name=op.f('ck_ideas_prob_calibrated_range')),
    sa.CheckConstraint('prob_raw IS NULL OR prob_raw BETWEEN 0 AND 1', name=op.f('ck_ideas_prob_raw_range')),
    sa.CheckConstraint('shares IS NULL OR shares >= 0', name=op.f('ck_ideas_shares_non_negative')),
    sa.ForeignKeyConstraint(['ai_call_id'], ['ai_calls.id'], name=op.f('fk_ideas_ai_call_id_ai_calls')),
    sa.ForeignKeyConstraint(['candidate_id'], ['candidates.id'], name=op.f('fk_ideas_candidate_id_candidates')),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('fk_ideas_run_id_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_ideas')),
    sa.UniqueConstraint('run_id', 'ticker', name=op.f('uq_ideas_run_id_ticker'))
    )
    with op.batch_alter_table('ideas', schema=None) as batch_op:
        batch_op.create_index('ix_ideas_plan_date', ['plan_date'], unique=False)
        batch_op.create_index('ix_ideas_ticker', ['ticker'], unique=False)

    op.create_table('lesson_cases',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('lesson_id', sa.Text(), nullable=False),
    sa.Column('idea_id', sa.Integer(), nullable=False),
    sa.Column('holds', sa.Boolean(), nullable=False),
    sa.Column('is_forward', sa.Boolean(), nullable=False),
    sa.Column('evaluated_at', sa.Text(), nullable=False),
    sa.ForeignKeyConstraint(['idea_id'], ['ideas.id'], name=op.f('fk_lesson_cases_idea_id_ideas')),
    sa.ForeignKeyConstraint(['lesson_id'], ['lessons.id'], name=op.f('fk_lesson_cases_lesson_id_lessons')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_lesson_cases')),
    sa.UniqueConstraint('lesson_id', 'idea_id', name=op.f('uq_lesson_cases_lesson_id_idea_id'))
    )
    op.create_table('positions',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('idea_id', sa.Integer(), nullable=False),
    sa.Column('is_shadow', sa.Boolean(), nullable=False),
    sa.Column('ticker', sa.Text(), nullable=False),
    sa.Column('qty_open', sa.Float(), nullable=False),
    sa.Column('avg_price', sa.Float(), nullable=False),
    sa.Column('stop', sa.Float(), nullable=False),
    sa.Column('targets', sa.Text(), nullable=False),
    sa.Column('initial_r', sa.Float(), nullable=False),
    sa.Column('state', sa.Text(), server_default=sa.text("'OPEN'"), nullable=False),
    sa.Column('opened_at', sa.Text(), nullable=False),
    sa.Column('closed_at', sa.Text(), nullable=True),
    sa.CheckConstraint("(state = 'CLOSED') = (closed_at IS NOT NULL)", name=op.f('ck_positions_closed_has_close_time')),
    sa.CheckConstraint("state <> 'CLOSED' OR qty_open = 0", name=op.f('ck_positions_closed_has_no_quantity')),
    sa.CheckConstraint("state IN ('OPEN','SELL_ALERT','PARTIALLY_CLOSED','CLOSED')", name=op.f('ck_positions_state_valid')),
    sa.CheckConstraint('avg_price > 0', name=op.f('ck_positions_avg_price_positive')),
    sa.CheckConstraint('initial_r > 0', name=op.f('ck_positions_initial_r_positive')),
    sa.CheckConstraint('qty_open >= 0', name=op.f('ck_positions_qty_non_negative')),
    sa.ForeignKeyConstraint(['idea_id'], ['ideas.id'], name=op.f('fk_positions_idea_id_ideas')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_positions'))
    )
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.create_index('ix_positions_state', ['state'], unique=False)
        batch_op.create_index('ix_positions_ticker', ['ticker'], unique=False)

    op.create_table('alerts',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('rule', sa.Text(), nullable=False),
    sa.Column('urgency', sa.Text(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.Column('resolved_at', sa.Text(), nullable=True),
    sa.Column('resolution', sa.Text(), nullable=True),
    sa.CheckConstraint("urgency IN ('ALTA','MEDIA','BAJA')", name=op.f('ck_alerts_urgency_valid')),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], name=op.f('fk_alerts_position_id_positions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_alerts'))
    )
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.create_index('uq_alerts_open_position_rule', ['position_id', 'rule'], unique=True, sqlite_where=sa.text('resolved_at IS NULL'), postgresql_where=sa.text('resolved_at IS NULL'))

    op.create_table('fills',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('position_id', sa.Integer(), nullable=False),
    sa.Column('side', sa.Text(), nullable=False),
    sa.Column('price', sa.Float(), nullable=False),
    sa.Column('qty', sa.Float(), nullable=False),
    sa.Column('filled_at', sa.Text(), nullable=False),
    sa.Column('commission', sa.Float(), server_default=sa.text('0'), nullable=False),
    sa.Column('origin', sa.Text(), nullable=False),
    sa.CheckConstraint("origin IN ('user','virtual')", name=op.f('ck_fills_origin_valid')),
    sa.CheckConstraint("side IN ('buy','sell')", name=op.f('ck_fills_side_valid')),
    sa.CheckConstraint('commission >= 0', name=op.f('ck_fills_commission_non_negative')),
    sa.CheckConstraint('price > 0', name=op.f('ck_fills_price_positive')),
    sa.CheckConstraint('qty > 0', name=op.f('ck_fills_qty_positive')),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], name=op.f('fk_fills_position_id_positions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_fills'))
    )
    with op.batch_alter_table('fills', schema=None) as batch_op:
        batch_op.create_index('ix_fills_position_id', ['position_id'], unique=False)

    op.create_table('confirmations',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('kind', sa.Text(), nullable=False),
    sa.Column('idea_id', sa.Integer(), nullable=True),
    sa.Column('position_id', sa.Integer(), nullable=True),
    sa.Column('alert_id', sa.Integer(), nullable=True),
    sa.Column('corrects_id', sa.Integer(), nullable=True),
    sa.Column('data', sa.Text(), nullable=True),
    sa.Column('note', sa.Text(), nullable=True),
    sa.Column('created_at', sa.Text(), nullable=False),
    sa.CheckConstraint("kind IN ('bought','declined','sold','stop_raised')", name=op.f('ck_confirmations_kind_valid')),
    sa.ForeignKeyConstraint(['alert_id'], ['alerts.id'], name=op.f('fk_confirmations_alert_id_alerts')),
    sa.ForeignKeyConstraint(['corrects_id'], ['confirmations.id'], name=op.f('fk_confirmations_corrects_id_confirmations')),
    sa.ForeignKeyConstraint(['idea_id'], ['ideas.id'], name=op.f('fk_confirmations_idea_id_ideas')),
    sa.ForeignKeyConstraint(['position_id'], ['positions.id'], name=op.f('fk_confirmations_position_id_positions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_confirmations'))
    )


def downgrade() -> None:
    op.drop_table('confirmations')
    with op.batch_alter_table('fills', schema=None) as batch_op:
        batch_op.drop_index('ix_fills_position_id')

    op.drop_table('fills')
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index('uq_alerts_open_position_rule', sqlite_where=sa.text('resolved_at IS NULL'), postgresql_where=sa.text('resolved_at IS NULL'))

    op.drop_table('alerts')
    with op.batch_alter_table('positions', schema=None) as batch_op:
        batch_op.drop_index('ix_positions_ticker')
        batch_op.drop_index('ix_positions_state')

    op.drop_table('positions')
    op.drop_table('lesson_cases')
    with op.batch_alter_table('ideas', schema=None) as batch_op:
        batch_op.drop_index('ix_ideas_ticker')
        batch_op.drop_index('ix_ideas_plan_date')

    op.drop_table('ideas')
    op.drop_table('control_results')
    op.drop_table('candidates')
    with op.batch_alter_table('ai_calls', schema=None) as batch_op:
        batch_op.drop_index('ix_ai_calls_dossier_hash')
        batch_op.drop_index('ix_ai_calls_created_at')

    op.drop_table('ai_calls')
    op.drop_table('universe')
    op.drop_table('system_status')
    with op.batch_alter_table('state_transitions', schema=None) as batch_op:
        batch_op.drop_index('ix_state_transitions_entity')

    op.drop_table('state_transitions')
    with op.batch_alter_table('runs', schema=None) as batch_op:
        batch_op.drop_index('ix_runs_as_of')

    op.drop_table('runs')
    with op.batch_alter_table('raw_snapshots', schema=None) as batch_op:
        batch_op.drop_index('ix_raw_snapshots_source_dataset_as_of')
        batch_op.drop_index('ix_raw_snapshots_fetched_at')

    op.drop_table('raw_snapshots')
    with op.batch_alter_table('news', schema=None) as batch_op:
        batch_op.drop_index('ix_news_published_at')

    op.drop_table('news')
    op.drop_table('metrics_daily')
    op.drop_table('macro_points')
    op.drop_table('lessons')
    op.drop_table('indicator_values')
    with op.batch_alter_table('econ_events', schema=None) as batch_op:
        batch_op.drop_index('ix_econ_events_scheduled_at')

    op.drop_table('econ_events')
    with op.batch_alter_table('corporate_events', schema=None) as batch_op:
        batch_op.drop_index('ix_corporate_events_ticker_event_date')

    op.drop_table('corporate_events')
    op.drop_table('bars_intraday')
    op.drop_table('bars_daily')
