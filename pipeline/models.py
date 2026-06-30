"""
ORM 엔티티 정의 (SQLAlchemy 2.0 declarative).

테이블 관계 (모든 키는 law_id 기준)
  LawCatalog   (law_id PK)   전체 현행법령 목록. "무엇이 존재하나" — discovery 가 채움
    └ 1:1 CollectState       처리 상태/재처리 (status·attempts·error·지문·내용해시)
    └ 1:1 Law                수집 결과. payload(JSONB) 통째 + 조회용 컬럼
            └ 1:N LawRelation   relations 정규화 (AI팀 쿼리용)
  SyncHistory                지문 변경 이력 (감사용)
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import (
    DeclarativeBase, Mapped, mapped_column, relationship,
)


class Base(DeclarativeBase):
    pass


class LawCatalog(Base):
    """전체 현행법령 목록(현행). discovery 가 페이지네이션으로 채운다."""
    __tablename__ = "law_catalog"

    law_id: Mapped[str] = mapped_column(Text, primary_key=True)        # 안정적 법령 식별자
    mst: Mapped[str | None] = mapped_column(Text)                      # 현행 버전 일련번호
    law_name: Mapped[str | None] = mapped_column(Text)
    law_type: Mapped[str | None] = mapped_column(Text)                 # 법령구분명(법률/대통령령/…)
    ministry: Mapped[str | None] = mapped_column(Text)                 # 소관부처명
    enforcement_date: Mapped[str | None] = mapped_column(Text)
    promulgation_date: Mapped[str | None] = mapped_column(Text)
    detail_link: Mapped[str | None] = mapped_column(Text)
    version_signature: Mapped[str | None] = mapped_column(Text)        # 법률+시행령+시행규칙 MST 지문(최신)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)     # 폐지되면 false(soft delete)
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # 최초 발견
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))   # 마지막 목록조회에서 본 시각


class CollectState(Base):
    """법령별 처리 상태 + 변경감지 값(재처리의 핵심)."""
    __tablename__ = "collect_state"
    __table_args__ = (Index("idx_state_status", "status"),)

    law_id: Mapped[str] = mapped_column(Text, primary_key=True)
    law_name: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str | None] = mapped_column(Text)                   # pending | done | failed
    attempts: Mapped[int] = mapped_column(Integer, default=0)          # 수집 시도 횟수
    last_error: Mapped[str | None] = mapped_column(Text)              # 마지막 실패 메시지
    version_signature: Mapped[str | None] = mapped_column(Text)        # 마지막 수집 시점 MST 지문(변경감지)
    content_hash: Mapped[str | None] = mapped_column(Text)            # 마지막 내용 해시(저장 스킵)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Law(Base):
    """수집 결과 본체. payload(JSONB) 통째 + 자주 쓰는 조회용 컬럼."""
    __tablename__ = "law"

    law_id: Mapped[str] = mapped_column(Text, primary_key=True)
    law_name: Mapped[str | None] = mapped_column(Text)
    mst: Mapped[str | None] = mapped_column(Text)
    law_type: Mapped[str | None] = mapped_column(Text)
    enforcement_date: Mapped[str | None] = mapped_column(Text)
    promulgation_date: Mapped[str | None] = mapped_column(Text)
    is_current: Mapped[bool | None] = mapped_column(Boolean)
    article_count: Mapped[int | None] = mapped_column(Integer)
    body_text: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)       # 전체 payload(문서 그대로)
    content_hash: Mapped[str | None] = mapped_column(Text)
    version_signature: Mapped[str | None] = mapped_column(Text)
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    relations: Mapped[list["LawRelation"]] = relationship(
        back_populates="law", cascade="all, delete-orphan", passive_deletes=True,
    )


class LawRelation(Base):
    """relations 정규화 행 (law.payload 의 relations 를 풀어 담은 쿼리용 사본)."""
    __tablename__ = "law_relation"
    __table_args__ = (
        Index("idx_relation_law", "law_id"),
        Index("idx_relation_target", "target_law_name", "target_article_no"),
        Index("idx_relation_type", "relation_type"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    law_id: Mapped[str] = mapped_column(
        Text, ForeignKey("law.law_id", ondelete="CASCADE"), nullable=False)
    law_name: Mapped[str | None] = mapped_column(Text)
    relation_type: Mapped[str | None] = mapped_column(Text)            # delegation | citation | internal_ref
    delegation_type: Mapped[str | None] = mapped_column(Text)
    target_category: Mapped[str | None] = mapped_column(Text)          # 법령 | 행정규칙 | 자치법규 | 학칙공단
    source_article_no: Mapped[str | None] = mapped_column(Text)
    source_clause: Mapped[str | None] = mapped_column(Text)
    link_text: Mapped[str | None] = mapped_column(Text)
    target_law_name: Mapped[str | None] = mapped_column(Text)
    target_article_no: Mapped[str | None] = mapped_column(Text)
    target_mst: Mapped[str | None] = mapped_column(Text)
    target_url: Mapped[str | None] = mapped_column(Text)
    resolve_method: Mapped[str | None] = mapped_column(Text)

    law: Mapped["Law"] = relationship(back_populates="relations")


class SyncHistory(Base):
    """지문 변경 이력(감사용)."""
    __tablename__ = "sync_history"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    law_id: Mapped[str | None] = mapped_column(Text)
    law_name: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    old_signature: Mapped[str | None] = mapped_column(Text)
    new_signature: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
