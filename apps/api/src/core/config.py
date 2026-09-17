"""配置管理模块"""
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 配置模块位于 apps/api/src/core/config.py；monorepo 根为 parents[4]。
_REPO_ROOT = Path(__file__).resolve().parents[4]
_ENV_FILE_CANDIDATES = (
    _REPO_ROOT / ".env",       # 仓库根目录（alembic 在 apps/api/ 子目录跑也能读到）
    Path.cwd() / ".env",       # 当前工作目录（开发服务器）
)
_DEFAULT_ENV_FILE = str(
    next((p for p in _ENV_FILE_CANDIDATES if p.is_file()), _ENV_FILE_CANDIDATES[0])
)


def _anchor_sqlite_url(url: str) -> str:
    """把 sqlite:///./relative 之类的相对路径钉到仓库根，避免 cwd 漂移。

    这样 alembic 在 apps/api/ 子目录跑、uvicorn 在仓库根跑、CLI 在任意目录跑，
    DB 文件都落在 <repo>/data/ontolohub.db。
    """
    if not url.startswith("sqlite"):
        return url
    # 形如 sqlite:///./data/x.db / sqlite:////abs/path/db / sqlite:///./db
    if "//./" in url:
        return url.replace("sqlite:///", f"sqlite:///{_REPO_ROOT.as_posix()}/", 1)
    return url


class DatabaseSettings(BaseSettings):
    """数据库配置"""
    url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/ontolohub",
        description="数据库连接 URL",
    )
    pool_size: int = Field(default=10, ge=1, le=100)
    max_overflow: int = Field(default=20, ge=0, le=50)
    echo: bool = Field(default=False, description="是否打印 SQL 日志")

    model_config = SettingsConfigDict(
        env_prefix="DATABASE_",
        env_file=_DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )


class APISettings(BaseSettings):
    """API 配置"""
    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1, le=65535)
    debug: bool = Field(default=False)
    title: str = Field(default="OntoloHub API")
    version: str = Field(default="0.1.0")
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8080"]
    )

    model_config = SettingsConfigDict(env_prefix="API_")


class PluginSettings(BaseSettings):
    """插件配置"""
    path: Path = Field(default=Path("./plugins"))
    enabled: bool = Field(default=True)

    model_config = SettingsConfigDict(env_prefix="PLUGINS_")


class SemanticaSettings(BaseSettings):
    """语义服务配置 (Semantica)"""
    url: str = Field(default="http://localhost:8080")
    enabled: bool = Field(default=False)
    timeout: int = Field(default=60, ge=1)

    model_config = SettingsConfigDict(env_prefix="SEMANTICA_")


class OpenMetadataSettings(BaseSettings):
    """OpenMetadata 配置"""
    url: str = Field(default="http://localhost:8585")
    enabled: bool = Field(default=False)
    token: Optional[str] = Field(default=None)

    model_config = SettingsConfigDict(env_prefix="OPENMETADATA_")


class AISettings(BaseSettings):
    """AI 模型配置"""
    provider: str = Field(default="openai")
    model_name: str = Field(default="gpt-4o-mini")
    api_key: Optional[str] = Field(default=None)
    base_url: str = Field(default="https://api.openai.com/v1")
    max_tokens: int = Field(default=4000, ge=100)
    temperature: float = Field(default=0.7, ge=0, le=2)

    model_config = SettingsConfigDict(env_prefix="AI_")

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v.strip() == "":
            return None
        return v


class SecuritySettings(BaseSettings):
    """安全配置"""
    secret_key: str = Field(default="change-me-in-production")
    allowed_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8080"]
    )

    model_config = SettingsConfigDict(env_prefix="")


class UploadSettings(BaseSettings):
    """文件上传配置"""
    max_size_mb: int = Field(default=50, ge=1, le=500)
    allowed_types: list[str] = Field(
        default=[".csv", ".xlsx", ".json", ".ttl", ".owl", ".nt"]
    )

    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024

    model_config = SettingsConfigDict(env_prefix="UPLOAD_")


class LogSettings(BaseSettings):
    """日志配置"""
    level: str = Field(default="INFO")
    format: str = Field(default="text")  # text | json

    model_config = SettingsConfigDict(env_prefix="LOG_")


class Settings(BaseSettings):
    """全局配置"""
    # 优先从仓库根目录的 .env 读取，这样 alembic 在 apps/api/ 子目录跑也能加载。
    # 也兼容「当前工作目录下 .env」以及「环境变量」。
    _ENV_FILE_CANDIDATES = (
        Path(__file__).resolve().parents[3] / ".env",  # 仓库根目录
        Path.cwd() / ".env",                           # 当前工作目录（开发服务器）
    )

    model_config = SettingsConfigDict(
        env_file=_DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 子配置
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    api: APISettings = Field(default_factory=APISettings)
    plugins: PluginSettings = Field(default_factory=PluginSettings)
    semantica: SemanticaSettings = Field(default_factory=SemanticaSettings)
    openmetadata: OpenMetadataSettings = Field(default_factory=OpenMetadataSettings)
    ai: AISettings = Field(default_factory=AISettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    log: LogSettings = Field(default_factory=LogSettings)

    def model_post_init(self, __context: object) -> None:
        # 把 SQLite 相对路径钉到仓库根
        self.database.url = _anchor_sqlite_url(self.database.url)


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()


# 快捷访问
settings = get_settings()
