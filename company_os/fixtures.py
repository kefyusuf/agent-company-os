"""Explicit test-only seed. It creates no runtime and cannot activate a company."""
from .store import Store, Actor


def seed_demo(store: Store, company: str = 'COMPANY-DEMO-001') -> Actor:
    return store.bootstrap(company,[
        ('POS-CEO','core.ceo@1',None,'AGT-CEO','APPOINT-CEO'),
        ('POS-CTO','software.cto@1','POS-CEO','AGT-CTO','APPOINT-CTO'),
        ('POS-MANAGER','software.engineering-manager@1','POS-CTO','AGT-MANAGER','APPOINT-MANAGER'),
        ('POS-BACKEND','software.backend-engineer@1','POS-MANAGER','AGT-BACKEND','APPOINT-BACKEND'),
    ])
