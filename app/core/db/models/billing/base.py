from ..base import Base


class BillingBase(Base):
    __abstract__ = True
    __table_args__ = {"schema": "billing"}
