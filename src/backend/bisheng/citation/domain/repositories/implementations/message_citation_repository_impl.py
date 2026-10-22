from collections import defaultdict
from typing import DefaultDict, Dict, List, Optional

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from bisheng.citation.domain.models.message_citation import MessageCitation
from bisheng.citation.domain.repositories.interfaces.message_citation_repository import MessageCitationRepository
from bisheng.common.repositories.implementations.base_repository_impl import BaseRepositoryImpl


class MessageCitationRepositoryImpl(BaseRepositoryImpl[MessageCitation, int], MessageCitationRepository):
    """Repository implementation for message citation records."""

    def __init__(self, session: AsyncSession | Session):
        super().__init__(session, MessageCitation)

    async def find_by_message_id(self, message_id: int) -> List[MessageCitation]:
        """Find all citations for a single message."""
        query = (
            select(MessageCitation)
            .where(MessageCitation.message_id == message_id)
            .order_by(MessageCitation.id.asc())
        )
        result = await self.session.exec(query)
        return list(result.all())

    def find_by_message_id_sync(self, message_id: int) -> List[MessageCitation]:
        """Find all citations for a single message synchronously."""
        query = (
            select(MessageCitation)
            .where(MessageCitation.message_id == message_id)
            .order_by(MessageCitation.id.asc())
        )
        result = self.session.exec(query)
        return list(result.all())

    async def find_by_citation_id(self, citation_id: str) -> Optional[MessageCitation]:
        """Find one citation by its business citation ID."""
        query = select(MessageCitation).where(MessageCitation.citation_id == citation_id)
        result = await self.session.exec(query)
        return result.first()

    async def bulk_create(self, citations: List[MessageCitation]) -> List[MessageCitation]:
        """Create citations in batch."""
        if not citations:
            return []
        try:
            return await self.bulk_save(citations)
        except IntegrityError:
            # citation_id is globally unique; concurrent check-then-insert can still
            # hit the unique index. Roll back, retry one-by-one and skip duplicates so
            # the workflow does not fail on a repeated citation.
            await self.session.rollback()
            created: List[MessageCitation] = []
            for citation in citations:
                try:
                    created.append(await self.save(citation))
                except IntegrityError:
                    await self.session.rollback()
                    logger.warning(
                        "skip duplicate citation_id={} for message_id={}",
                        citation.citation_id,
                        citation.message_id,
                    )
            return created

    def bulk_create_sync(self, citations: List[MessageCitation]) -> List[MessageCitation]:
        """Create citations in batch synchronously."""
        if not citations:
            return []
        try:
            return self.bulk_save_sync(citations)
        except IntegrityError:
            # citation_id is globally unique; concurrent check-then-insert can still
            # hit the unique index. Roll back, retry one-by-one and skip duplicates so
            # the workflow does not fail on a repeated citation.
            self.session.rollback()
            created: List[MessageCitation] = []
            for citation in citations:
                try:
                    created.append(self.save_sync(citation))
                except IntegrityError:
                    self.session.rollback()
                    logger.warning(
                        "skip duplicate citation_id={} for message_id={}",
                        citation.citation_id,
                        citation.message_id,
                    )
            return created

    async def find_by_citation_ids(self, citation_ids: List[str]) -> List[MessageCitation]:
        """Find citations by multiple business citation IDs."""
        if not citation_ids:
            return []

        query = (
            select(MessageCitation)
            .where(col(MessageCitation.citation_id).in_(citation_ids))
            .order_by(MessageCitation.id.asc())
        )
        result = await self.session.exec(query)
        return list(result.all())

    def find_by_citation_ids_sync(self, citation_ids: List[str]) -> List[MessageCitation]:
        """Find citations by multiple business citation IDs synchronously."""
        if not citation_ids:
            return []

        query = (
            select(MessageCitation)
            .where(col(MessageCitation.citation_id).in_(citation_ids))
            .order_by(MessageCitation.id.asc())
        )
        result = self.session.exec(query)
        return list(result.all())

    async def find_by_message_ids_grouped(self, message_ids: List[int]) -> Dict[int, List[MessageCitation]]:
        """Find citations for multiple messages and group them by message ID."""
        if not message_ids:
            return {}

        query = (
            select(MessageCitation)
            .where(col(MessageCitation.message_id).in_(message_ids))
            .order_by(MessageCitation.message_id.asc(), MessageCitation.id.asc())
        )
        result = await self.session.exec(query)

        grouped_citations: DefaultDict[int, List[MessageCitation]] = defaultdict(list)
        for citation in result.all():
            grouped_citations[citation.message_id].append(citation)

        return dict(grouped_citations)
