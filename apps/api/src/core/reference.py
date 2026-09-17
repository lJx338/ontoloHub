"""参考本体管理"""
import uuid
from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, Field


class ReferenceType(str, Enum):
    """参考类型"""
    DICTIONARY = "dictionary"  # 字典参考
    ARCHITECTURE = "architecture"  # 架构参考
    MAPPING_TARGET = "mapping_target"  # 映射目标


class ReferenceOntology(BaseModel):
    """参考本体"""
    id: uuid.UUID
    name: str  # IOF Core, IOF Process...
    namespace: str  # https://www.industrialontology.org/ontologies/...
    version: str
    reference_type: ReferenceType
    
    # 来源
    source_url: Optional[str] = None
    source_format: Literal["owl", "ttl", "jsonld"] = "owl"
    
    # 导入状态
    is_imported: bool = False
    imported_at: Optional[str] = None
    
    # 使用范围
    used_namespaces: list[str] = Field(default_factory=list)  # 只引用这些命名空间


class LocalClass(BaseModel):
    """本地类定义"""
    id: uuid.UUID
    name: str
    iri: str
    
    # 参考对齐
    references: list[ReferenceMapping] = Field(default_factory=list)


class ReferenceMapping(BaseModel):
    """参考映射"""
    reference_ontology_id: uuid.UUID
    reference_iri: str
    mapping_type: Literal["exact", "similar", "broader", "narrower"] = "similar"
    confidence: float = Field(ge=0, le=1)
    notes: Optional[str] = None
