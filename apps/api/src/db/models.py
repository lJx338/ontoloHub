"""?????"""
from .base import Base
from .project import Project, UseCase, Requirement, Decision
from .ontology import (
    Ontology,
    OntologyVersion,
    OntologyClass,
    Property,
    Relation,
    Constraint,
    OntologyKind,
    OntologyStatus,
    OntologyVersionStatus,
    ClassType,
    PropertyType,
    RelationType,
    ConstraintType,
    SeverityLevel,
)
from .candidate import Proposal, ModelRun, ReviewDecision
from .evidence import Source, SourceSnapshot, Evidence, ProfilingRun
from .mapping import MappingVersion, IdentityMapping, DatasetSnapshot
from .object_ import Object, Link
from .validation import MappingFixture, ValidationRun, SavedQuery, ExpectedResult
from .runtime import ObjectView, ActionType, ActionRun, AutomationRule, Task
from .release import (
    ChangeRequest,
    ChangeRequestReviewer,
    ChangeRequestComment,
    ReviewerStatus,
    Release,
    UseCaseBundle,
    Deployment,
    PreflightReport,
)
from .governance import PluginInstallation, AuditEvent, DriftProposal, HealthSnapshot
from .identity import User, Membership, GlobalRole, Role, has_role, ApiKey
from .connector import Connector, ConnectorType, ConnectorStatus

__all__ = [
    "Base",
    # ??????
    "OntologyKind",
    "OntologyStatus",
    "OntologyVersionStatus",
    "ClassType",
    "PropertyType",
    "RelationType",
    "ConstraintType",
    "SeverityLevel",
    # ??
    "Project",
    "UseCase",
    "Requirement",
    "Decision",
    # ??
    "Ontology",
    "OntologyVersion",
    "OntologyClass",
    "Property",
    "Relation",
    "Constraint",
    # ??
    "Proposal",
    "ModelRun",
    "ReviewDecision",
    # ??
    "Source",
    "SourceSnapshot",
    "Evidence",
    "ProfilingRun",
    # ??
    "MappingVersion",
    "IdentityMapping",
    "DatasetSnapshot",
    # ??
    "Object",
    "Link",
    # ??
    "MappingFixture",
    "ValidationRun",
    "SavedQuery",
    "ExpectedResult",
    # ??
    "ObjectView",
    "ActionType",
    "ActionRun",
    "AutomationRule",
    "Task",
    # CR
    "ChangeRequest",
    "ChangeRequestReviewer",
    "ChangeRequestComment",
    "ReviewerStatus",
    "Release",
    "UseCaseBundle",
    "Deployment",
    "PreflightReport",
    # ??
    "PluginInstallation",
    "AuditEvent",
    "DriftProposal",
    "HealthSnapshot",
    # 身份与权限（HIA-51 M1-01）
    "User",
    "Membership",
    "GlobalRole",
    "Role",
    "has_role",
    "ApiKey",  # HIA-64 B1
    # Connector 框架（HIA-71）
    "Connector",
    "ConnectorType",
    "ConnectorStatus",
]