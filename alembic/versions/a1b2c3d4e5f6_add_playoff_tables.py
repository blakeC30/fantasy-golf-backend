"""add playoff tables

Revision ID: a1b2c3d4e5f6
Revises: c9d3f2a8e5b1
Create Date: 2026-03-10
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "c9d3f2a8e5b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE playoff_configs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            league_id UUID NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
            season_id INTEGER NOT NULL REFERENCES seasons(id) ON DELETE CASCADE,
            is_enabled BOOLEAN NOT NULL DEFAULT false,
            playoff_size INTEGER NOT NULL DEFAULT 16,
            draft_style VARCHAR(30) NOT NULL DEFAULT 'snake',
            round1_picks_per_player INTEGER NOT NULL DEFAULT 2,
            subsequent_picks_per_player INTEGER NOT NULL DEFAULT 4,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            seeded_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_playoff_config_league_season UNIQUE (league_id, season_id)
        );

        CREATE TABLE playoff_rounds (
            id SERIAL PRIMARY KEY,
            playoff_config_id UUID NOT NULL REFERENCES playoff_configs(id) ON DELETE CASCADE,
            round_number INTEGER NOT NULL,
            tournament_id UUID REFERENCES tournaments(id),
            draft_opens_at TIMESTAMPTZ,
            draft_resolved_at TIMESTAMPTZ,
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_playoff_round_config_number UNIQUE (playoff_config_id, round_number)
        );

        CREATE TABLE playoff_pods (
            id SERIAL PRIMARY KEY,
            playoff_round_id INTEGER NOT NULL REFERENCES playoff_rounds(id) ON DELETE CASCADE,
            bracket_position INTEGER NOT NULL,
            winner_user_id UUID REFERENCES users(id),
            status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_playoff_pod_round_position UNIQUE (playoff_round_id, bracket_position)
        );

        CREATE TABLE playoff_pod_members (
            id SERIAL PRIMARY KEY,
            pod_id INTEGER NOT NULL REFERENCES playoff_pods(id) ON DELETE CASCADE,
            user_id UUID NOT NULL REFERENCES users(id),
            seed INTEGER NOT NULL,
            draft_position INTEGER NOT NULL,
            total_points DOUBLE PRECISION,
            is_eliminated BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_playoff_pod_member UNIQUE (pod_id, user_id),
            CONSTRAINT uq_playoff_pod_draft_position UNIQUE (pod_id, draft_position),
            CONSTRAINT uq_playoff_pod_seed UNIQUE (pod_id, seed)
        );

        CREATE TABLE playoff_picks (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            pod_id INTEGER NOT NULL REFERENCES playoff_pods(id) ON DELETE CASCADE,
            pod_member_id INTEGER NOT NULL REFERENCES playoff_pod_members(id) ON DELETE CASCADE,
            golfer_id UUID NOT NULL REFERENCES golfers(id),
            tournament_id UUID NOT NULL REFERENCES tournaments(id),
            draft_slot INTEGER NOT NULL,
            points_earned DOUBLE PRECISION,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_playoff_pick_pod_golfer UNIQUE (pod_id, golfer_id),
            CONSTRAINT uq_playoff_pick_pod_member_slot UNIQUE (pod_id, pod_member_id, draft_slot)
        );

        CREATE TABLE playoff_draft_preferences (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            pod_id INTEGER NOT NULL REFERENCES playoff_pods(id) ON DELETE CASCADE,
            pod_member_id INTEGER NOT NULL REFERENCES playoff_pod_members(id) ON DELETE CASCADE,
            golfer_id UUID NOT NULL REFERENCES golfers(id) ON DELETE CASCADE,
            rank INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_pref_member_golfer UNIQUE (pod_member_id, golfer_id),
            CONSTRAINT uq_pref_member_rank UNIQUE (pod_member_id, rank)
        );

        -- Indexes for common query patterns
        CREATE INDEX ix_playoff_rounds_config ON playoff_rounds(playoff_config_id);
        CREATE INDEX ix_playoff_pods_round ON playoff_pods(playoff_round_id);
        CREATE INDEX ix_playoff_pod_members_pod ON playoff_pod_members(pod_id);
        CREATE INDEX ix_playoff_pod_members_user ON playoff_pod_members(user_id);
        CREATE INDEX ix_playoff_picks_pod ON playoff_picks(pod_id);
        CREATE INDEX ix_playoff_picks_pod_member ON playoff_picks(pod_member_id);
        CREATE INDEX ix_playoff_draft_prefs_pod_member ON playoff_draft_preferences(pod_member_id);
        CREATE INDEX ix_playoff_draft_prefs_pod ON playoff_draft_preferences(pod_id);

        UPDATE alembic_version SET version_num = 'a1b2c3d4e5f6';
    """)


def downgrade() -> None:
    op.execute("""
        DROP TABLE IF EXISTS playoff_draft_preferences;
        DROP TABLE IF EXISTS playoff_picks;
        DROP TABLE IF EXISTS playoff_pod_members;
        DROP TABLE IF EXISTS playoff_pods;
        DROP TABLE IF EXISTS playoff_rounds;
        DROP TABLE IF EXISTS playoff_configs;

        UPDATE alembic_version SET version_num = 'c9d3f2a8e5b1';
    """)
