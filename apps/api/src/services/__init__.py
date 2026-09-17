"""服务模块"""
from src.services.shacl import (
    validate,
    validate_ontology_data,
    build_shapes_from_ontology,
    ValidationResult,
    Violation,
)

__all__ = [
    "validate",
    "validate_ontology_data",
    "build_shapes_from_ontology",
    "ValidationResult",
    "Violation",
]
