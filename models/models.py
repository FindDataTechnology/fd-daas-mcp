"""Single source of truth for all database schemas across the MCP ecosystem.

Every MCP server and the dashboard share this one Base. Schema changes
MUST be made here first, then reflected in consuming code.

Shared Base for all tables across all MCP domains. Entity +
EntityDatasourceLink were dropped (design D5 / task 3.7) — the entity master
lives in fd-open-data-mcp. Adds +GatewayUpstream for the gateway-mcp data
gateway; +PipelineCollection + PipelineCollectionItem for daas-mcp managed
fetch+cron collections.

Domains:
  cron-mcp:    Schedule, Execution, Task (scheduler data)
  daas-mcp:    DaasSource, DaasFunction, DaasFunctionColumn, Observation (source-based registry)
  daas-mcp mgmt: Category, DatasourceForm, DatasourceSection, DatasourceCollection,
                 DatasourceCollectionItem, PipelineCollection, PipelineCollectionItem
  dashboard:   Datasource, DatasourceColumn (dashboard metadata)
  process:    Rule (unified json/script/position/llm), ProcessResult, IndicatorRule (LLM extraction + indicators; owned by daas-mcp, relocated from process-mcp)
  entity:      EntityCollection, EntityCollectionItem, EntityCollectionChange (natural-key entity groups; master is fd-open-data-mcp)
"""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

# ═══════════════════════════════════════════════════════════════
# gateway-mcp domain — upstream registry (gateway_upstreams)
# ═══════════════════════════════════════════════════════════════
# ponytail: P4 — harness-registry models (Function/FunctionColumn/
# DataSnapshot) + SpecialistAgent (CrewAI) removed when leader-mcp
# dissolved into gateway-mcp + workflow-mcp. Live daas catalog =
# DaasFunction/DaasFunctionColumn; data-fetch routing = fd-open-data-mcp.


class GatewayUpstream(Base):
    """A data-fetch MCP upstream that the gateway routes to via a fastmcp.Client.

    Named `gateway_upstreams` — the gateway is the single entry point for all
    data-fetch calls (routed to fd-open-data-mcp or other upstreams). transport
    is 'stdio' (command + args_json + cwd + env_json) or 'http' (url). The
    fastmcp.Client is built from these fields (see gateway_database.build_client),
    mirroring composite-mcp's Upstream pattern.
    """
    __tablename__ = "gateway_upstreams"
    __table_args__ = (UniqueConstraint("name", name="uq_gateway_upstream_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    transport = Column(String(16), nullable=False, default="http")
    command = Column(String, nullable=True)        # stdio executable
    url = Column(String, nullable=True)             # http transport URL
    args_json = Column(JSON, nullable=True)        # stdio argv list
    env_json = Column(JSON, nullable=True)         # stdio env dict (optional override)
    cwd = Column(String, nullable=True)            # stdio working directory
    enabled = Column(Boolean, default=True, nullable=False)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "url": self.url,
            "args": self.args_json or [],
            "env": self.env_json or {},
            "cwd": self.cwd,
            "enabled": bool(self.enabled),
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# cron-mcp domain — scheduler data
# ═══════════════════════════════════════════════════════════════


class Schedule(Base):
    __tablename__ = "schedules"

    id = Column(String, primary_key=True, default=lambda: _short_uuid())
    name = Column(String, nullable=False)
    cron_expr = Column(String, nullable=False)
    task_name = Column(String, nullable=False)
    agent = Column(String, nullable=True)
    prompt = Column(Text, nullable=True)
    enabled = Column(Integer, default=1)
    timezone = Column(String, default="UTC")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    last_run_at = Column(DateTime, nullable=True)
    next_run_at = Column(DateTime, nullable=True)


class Execution(Base):
    __tablename__ = "executions"

    id = Column(String, primary_key=True, default=lambda: _short_uuid())
    schedule_id = Column(String, nullable=False, index=True)
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="pending")
    output = Column(Text, nullable=True)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, default=lambda: _short_uuid())
    name = Column(String, nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    command = Column(Text, nullable=False)
    timeout = Column(Integer, default=60)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


# ═══════════════════════════════════════════════════════════════
# daas-mcp domain — source-based registry
# ═══════════════════════════════════════════════════════════════


class DaasSource(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    label = Column(String(128), nullable=False)
    description = Column(String, nullable=True)
    url = Column(String(512), nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    config = Column(JSON, nullable=True)
    category_id = Column(
        Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True, index=True
    )
    score = Column(Float, nullable=True, default=None)  # default priority/quality weight
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    functions = relationship(
        "DaasFunction", back_populates="source", cascade="all, delete-orphan", lazy="selectin"
    )
    forms = relationship(
        "DatasourceForm", back_populates="source", cascade="all, delete-orphan", lazy="selectin"
    )
    category = relationship("Category", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "url": self.url,
            "enabled": self.enabled,
            "config": self.config or {},
            "category_id": self.category_id,
            "score": self.score,
            "function_count": len(self.functions) if self.functions else 0,
        }


class DaasFunction(Base):
    __tablename__ = "daas_functions"
    __table_args__ = (UniqueConstraint("source_id", "name", name="uq_source_daas_function"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String(255), nullable=False, index=True)
    label = Column(String(255), nullable=True)
    description = Column(String, nullable=True)
    category = Column(String(255), nullable=False, default="未分类")
    parameters = Column(JSON, nullable=True)
    output_type = Column(String(64), default="DataFrame")
    frequency = Column(String(64), nullable=True)  # data refresh cadence: daily/weekly/monthly/quarterly/yearly/realtime/irregular
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    source = relationship("DaasSource", back_populates="functions")
    columns = relationship(
        "DaasFunctionColumn",
        back_populates="function",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "source": self.source.name if self.source else None,
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "category": self.category,
            "parameters": self.parameters or [],
            "output_type": self.output_type,
            "frequency": self.frequency,
            "columns": [c.to_dict() for c in self.columns] if self.columns else [],
        }


class DaasFunctionColumn(Base):
    __tablename__ = "daas_function_columns"
    __table_args__ = (UniqueConstraint("function_id", "name", name="uq_daas_function_column"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    function_id = Column(
        Integer, ForeignKey("daas_functions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(String(255), nullable=False)
    label = Column(String(255), nullable=True)
    type = Column(String(64), nullable=True)
    description = Column(String, nullable=True)
    nullable = Column(Boolean, default=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    function = relationship("DaasFunction", back_populates="columns")

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "description": self.description,
            "nullable": self.nullable,
        }


class Observation(Base):
    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("source", "function_name", "indicator", "date", name="uq_observation"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(64), nullable=False, index=True)
    function_name = Column(String(255), nullable=False, index=True)
    indicator = Column(String(255), nullable=False, index=True)
    date = Column(String(64), nullable=False, index=True)
    value = Column(String(64), nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "function_name": self.function_name,
            "indicator": self.indicator,
            "date": self.date,
            "value": self.value,
            "metadata": self.metadata_ or {},
        }


# ═══════════════════════════════════════════════════════════════
# daas-mcp management domain — categories, forms, sections, collections
# (additive to the source-based registry above)
# ═══════════════════════════════════════════════════════════════


class Category(Base):
    """Hierarchical category tree for datasources. Self-referencing parent_id."""
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, unique=True, index=True)
    label = Column(String(255), nullable=True)
    parent_id = Column(
        Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sort_order = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    children = relationship(
        "Category",
        back_populates="parent",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    parent = relationship(
        "Category", back_populates="children", remote_side=[id], lazy="selectin"
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label,
            "parent_id": self.parent_id,
            "sort_order": self.sort_order,
        }


class DatasourceForm(Base):
    """A form exposed by a datasource (e.g. EDGAR '10-K', '8-K')."""
    __tablename__ = "datasource_forms"
    __table_args__ = (UniqueConstraint("source_id", "form_type", name="uq_source_form_type"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    form_type = Column(String(64), nullable=False)
    label = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    source = relationship("DaasSource", back_populates="forms")
    sections = relationship(
        "DatasourceSection",
        back_populates="form",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "form_type": self.form_type,
            "label": self.label,
            "sections": [s.to_dict() for s in self.sections] if self.sections else [],
        }


class DatasourceSection(Base):
    """A section of a form, carrying an extraction instruction.

    e.g. form '10-K' → section 'Item 1 Business' → instruction 'Extract the
    company-description paragraph.'
    """
    __tablename__ = "datasource_sections"
    __table_args__ = (UniqueConstraint("form_id", "section_name", name="uq_form_section_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    form_id = Column(
        Integer, ForeignKey("datasource_forms.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section_name = Column(String(255), nullable=False)
    instruction = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    form = relationship("DatasourceForm", back_populates="sections")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "form_id": self.form_id,
            "section_name": self.section_name,
            "instruction": self.instruction,
            "sort_order": self.sort_order,
        }


class DatasourceCollection(Base):
    """A named collection of datasources (or specific datasource-sections)."""
    __tablename__ = "datasource_collections"
    __table_args__ = (UniqueConstraint("name", name="uq_datasource_collection_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = relationship(
        "DatasourceCollectionItem",
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "item_count": len(self.items) if self.items else 0,
        }


class DatasourceCollectionItem(Base):
    """One entry in a collection: a whole datasource (section_id NULL) or a
    specific datasource-section (section_id set)."""
    __tablename__ = "datasource_collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "source_id", "section_id", name="uq_collection_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer, ForeignKey("datasource_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id = Column(
        Integer, ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    section_id = Column(
        Integer, ForeignKey("datasource_sections.id", ondelete="CASCADE"), nullable=True, index=True
    )
    sort_order = Column(Integer, nullable=False, default=0)
    score = Column(Float, nullable=True, default=None)  # per-collection override of source.score
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    collection = relationship("DatasourceCollection", back_populates="items")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "source_id": self.source_id,
            "section_id": self.section_id,
            "sort_order": self.sort_order,
            "score": self.score,
        }


class PipelineCollection(Base):
    """A named collection of fetch *items* — a managed "datasource collection"
    where each item binds a source MCP (`source_mcp` + `tool` + `arguments_json`)
    to a `scraw_<slug>` storage target and a cron cadence. Distinct from the
    curation-only `DatasourceCollection` (which groups datasources for the
    NotebookLM-style workspace and carries no fetch/storage/cron semantics).

    Adding an enabled item triggers an immediate history backfill + a
    `cron-mcp` schedule; removing/disabling an item unwires the schedule.
    """
    __tablename__ = "pipeline_collections"
    __table_args__ = (UniqueConstraint("name", name="uq_pipeline_collection_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = relationship(
        "PipelineCollectionItem",
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "item_count": len(self.items) if self.items else 0,
        }


class PipelineCollectionItem(Base):
    """One fetch unit in a `PipelineCollection`.

    `source_mcp` is a server name in `.mcp.json` (e.g. `akshare-mcp`); `tool`
    is the tool to call on that MCP (e.g. `call_akshare_function`); the tool's
    kwargs live in `arguments_json` (e.g. `{"name":"stock_zh_a_hist",
    "params_json":"{\\"symbol\\":\\"000001\\"}"}`). This is the `data_job`
    shape from `add-cron-mcp-data-fetch`, so items migrate 1:1 to
    `create_data_job` later.

    `task_name` is the `cron-mcp` task name (`pipeline_<collection>_<item>`),
    stored so remove/disable can delete the right rows. `last_run_at` /
    `last_status` / `last_row_count` / `error_message` record the most recent
    backfill or cron tick.
    """
    __tablename__ = "pipeline_collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "name", name="uq_pipeline_collection_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer,
        ForeignKey("pipeline_collections.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = Column(String(128), nullable=False)
    source_mcp = Column(String(128), nullable=False)
    tool = Column(String(255), nullable=False)
    arguments_json = Column(Text, nullable=True)
    storage_table = Column(String(128), nullable=False)
    upsert_keys_json = Column(Text, nullable=True)
    cron_expr = Column(String(64), nullable=False)
    timezone = Column(String(64), nullable=False, default="Asia/Shanghai")
    enabled = Column(Boolean, default=True, nullable=False)
    task_name = Column(String(255), nullable=True)
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(32), nullable=True)  # ok | backfill_failed | cron_failed | backfill_timeout
    last_row_count = Column(Integer, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    collection = relationship("PipelineCollection", back_populates="items")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "name": self.name,
            "source_mcp": self.source_mcp,
            "tool": self.tool,
            "arguments": _json_loads(self.arguments_json),
            "storage_table": self.storage_table,
            "upsert_keys": _json_loads(self.upsert_keys_json),
            "cron_expr": self.cron_expr,
            "timezone": self.timezone,
            "enabled": bool(self.enabled),
            "task_name": self.task_name,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_status": self.last_status,
            "last_row_count": self.last_row_count,
            "error_message": self.error_message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _json_loads(raw):
    import json as _json
    if raw is None:
        return None
    try:
        return _json.loads(raw)
    except (ValueError, TypeError):
        return raw


# ═══════════════════════════════════════════════════════════════
# dashboard domain
# ═══════════════════════════════════════════════════════════════


class Datasource(Base):
    __tablename__ = "datasources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    db_type = Column(String, nullable=False, default="sqlite")
    connection_string = Column(String, nullable=False)
    description = Column(String, default="")
    is_readonly = Column(Integer, default=1)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class DatasourceColumn(Base):
    __tablename__ = "datasource_columns"
    __table_args__ = (UniqueConstraint("datasource_id", "table_name", "column_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    datasource_id = Column(
        Integer, ForeignKey("datasources.id", ondelete="CASCADE"), nullable=False
    )
    table_name = Column(String, nullable=False)
    column_name = Column(String, nullable=False)
    column_type = Column(String, default="")
    is_primary_key = Column(Integer, default=0)
    is_nullable = Column(Integer, default=1)
    description = Column(String, default="")
    source_field = Column(String, default="")
    unit = Column(String, default="")
    semantic_type = Column(String, default="")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Dashboard(Base):
    """Metadata for a standalone HTML dashboard built by the `fd-daas-dashboard-creator`
    skill (one self-contained `mcp/dashboard-mcp/dashboards/<slug>.html` file). This
    table is the single source of truth for the dashboard registry — `dashboard-mcp`
    CRUD tools read/write it, and `index.html` + `daas.md` are regenerated from it.

    `slug` is the kebab-case filename stem (matches `^[A-Za-z0-9_-]+$`); `name` is the
    human-readable title; `intro` is a one-paragraph description; `source_tables` lists
    the `scraw_*` / `observations` tables backing the charts; `entity_coverage` /
    `time_range` describe the data scope; `chart_config` is a structural description
    (chart type + source columns + entity/date binding) the skill expands into ECharts
    options at build time — not a full ECharts option blob.
    """
    __tablename__ = "dashboards"
    __table_args__ = (UniqueConstraint("slug", name="uq_dashboard_slug"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(128), nullable=False, index=True)
    name = Column(String(256), nullable=False)
    intro = Column(Text, nullable=True)
    source_tables = Column(JSON, nullable=True)  # ["scraw_byd_daily", "observations", ...]
    entity_coverage = Column(JSON, nullable=True)  # ["600519", "000858"] or null for unscoped
    time_range = Column(JSON, nullable=True)  # {"start": "2024-01-01", "end": "2024-12-31"} or null
    refresh_cadence = Column(String(128), nullable=True)  # "static snapshot" | "daily 04:30 (Asia/Shanghai)"
    chart_config = Column(JSON, nullable=True)  # [{"type":"line","source_table":...,"x":...,"y":[...]}]
    file_path = Column(String(512), nullable=False)  # "mcp/dashboard-mcp/dashboards/<slug>.html"
    file_url = Column(String(512), nullable=False)  # "file:///abs/path/to/<slug>.html"
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "intro": self.intro,
            "source_tables": self.source_tables or [],
            "entity_coverage": self.entity_coverage,
            "time_range": self.time_range,
            "refresh_cadence": self.refresh_cadence,
            "chart_config": self.chart_config or [],
            "file_path": self.file_path,
            "file_url": self.file_url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# composite-mcp domain — composite MCP curation + orchestration
# ═══════════════════════════════════════════════════════════════


class Composite(Base):
    __tablename__ = "composites"
    __table_args__ = (UniqueConstraint("name", name="uq_composite_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(String, nullable=True)
    # ponytail: manifest-mode fields layered over the relational upstreams/tools.
    # workflows = names of registered workflow manifests (the `workflows` table)
    # to surface inside this composite; prompt = system prompt for the surface.
    workflows = Column(JSON, nullable=True)   # ["data-fetch", "indicators"]
    prompt = Column(Text, nullable=True)      # system prompt for the composite surface
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "workflows": self.workflows or [],
            "prompt": self.prompt,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Upstream(Base):
    """One upstream MCP connection, scoped to a composite.

    transport: 'stdio' (command+args) or 'http' (url).
    Scoped per-composite (denormalized) — two composites wanting the same
    upstream each define their own row.
    """
    __tablename__ = "upstreams"
    __table_args__ = (UniqueConstraint("composite_id", "key", name="uq_composite_upstream_key"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    composite_id = Column(Integer, ForeignKey("composites.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(128), nullable=False)  # short id used as mount namespace
    transport = Column(String(16), nullable=False, default="stdio")  # 'stdio' | 'http'
    command = Column(String, nullable=True)   # stdio: executable
    args = Column(JSON, nullable=True)        # stdio: argv
    env = Column(JSON, nullable=True)         # stdio: env dict
    cwd = Column(String, nullable=True)       # stdio: working directory
    url = Column(String, nullable=True)       # http: upstream URL
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "composite_id": self.composite_id,
            "key": self.key,
            "transport": self.transport,
            "command": self.command,
            "args": self.args or [],
            "env": self.env or {},
            "cwd": self.cwd,
            "url": self.url,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CompositeTool(Base):
    """A selected tool from an upstream, exposed (proxied) by a composite."""
    __tablename__ = "composite_tools"
    __table_args__ = (
        UniqueConstraint("composite_id", "upstream_key", "tool_name", name="uq_composite_tool"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    composite_id = Column(Integer, ForeignKey("composites.id", ondelete="CASCADE"), nullable=False, index=True)
    upstream_key = Column(String(128), nullable=False)
    tool_name = Column(String(255), nullable=False)
    alias = Column(String(255), nullable=True)  # retained for forward compat; unused in v1
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "composite_id": self.composite_id,
            "upstream_key": self.upstream_key,
            "tool_name": self.tool_name,
            "alias": self.alias,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class CompositeChain(Base):
    """A chained tool: a linear pipeline of upstream tool calls."""
    __tablename__ = "composite_chains"
    __table_args__ = (UniqueConstraint("composite_id", "name", name="uq_composite_chain_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    composite_id = Column(Integer, ForeignKey("composites.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(128), nullable=False)
    description = Column(String, nullable=True)
    steps = Column(JSON, nullable=False)  # [{upstream, tool, input: {...}}]
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "composite_id": self.composite_id,
            "name": self.name,
            "description": self.description,
            "steps": self.steps or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# helpers
# ═══════════════════════════════════════════════════════════════


def _short_uuid() -> str:
    import uuid
    return str(uuid.uuid4())[:8]


# ═══════════════════════════════════════════════════════════════
# process domain — LLM extraction rules + results + math indicators
# (owned by daas-mcp; relocated from the former process-mcp)
# ═══════════════════════════════════════════════════════════════


class Rule(Base):
    """A reusable, type-tagged membership/derivation rule - the unified rule
    store backing entity + indicator collection sync.

    `rule_type` selects the evaluator: `json` (declarative entity filter),
    `script` (a Python `members(ctx)` file), `position` (CSS/xpath/regex/
    json-path extraction), `llm` (natural-language extraction). `target` is
    what the rule yields: `entity_ids`, `indicator_names`, or `rows`.
    `config_json` carries the type-specific config (see the daas-rules-engine
    spec). Collections reference a rule via `rule_id` (nullable; NULL = manual).

    The `RuleEngine` (daas-mcp/rule_engine.py) dispatches on `rule_type`.
    Script loading is path-based (importlib) so it never relies on a bare
    import of a group-local module at runtime - the fix for the
    `No module named 'entity_rule_script'` regression.
    """
    __tablename__ = "rules"
    __table_args__ = (UniqueConstraint("name", name="uq_rule_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    rule_type = Column(String(16), nullable=False)  # json | script | position | llm
    target = Column(String(32), nullable=False, default="entity_ids")  # entity_ids | indicator_names | rows
    config_json = Column(JSON, nullable=False)
    description = Column(String, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "rule_type": self.rule_type,
            "target": self.target,
            "config": self.config_json or {},
            "description": self.description,
            "enabled": bool(self.enabled),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class ProcessResult(Base):
    """One extraction result row, idempotent on (rule_id, source_table, source_rowid)."""
    __tablename__ = "process_results"
    __table_args__ = (
        UniqueConstraint("rule_id", "source_table", "source_rowid", name="uq_process_result"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(
        Integer, ForeignKey("rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_table = Column(String(128), nullable=False, index=True)
    source_rowid = Column(Integer, nullable=False, index=True)
    extracted_json = Column(JSON, nullable=True)
    model = Column(String(128), nullable=True)
    run_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "source_table": self.source_table,
            "source_rowid": self.source_rowid,
            "extracted": self.extracted_json,
            "model": self.model,
            "run_at": self.run_at.isoformat() if self.run_at else None,
        }


class IndicatorRule(Base):
    """A persisted indicator rule: bind a source data table + date/value column
    + math op to an output indicator name, replayable via run_indicator.

    `datasource` is a soft reference to daas `sources.name` (no FK, matching
    the unified `rules` table's `llm`-type `config_json.datasource`). `run_indicator` upserts results into the daas
    `observations` table — the project's existing indicator store — keyed on
    (source=datasource, function_name, indicator=indicator_name, date).
    """
    __tablename__ = "indicator_rules"
    __table_args__ = (UniqueConstraint("name", name="uq_indicator_rule_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    datasource = Column(String(128), nullable=False)  # daas sources.name (soft ref)
    function_name = Column(String(255), nullable=False)
    source_table = Column(String(128), nullable=False)
    date_column = Column(String(128), nullable=False)
    value_column = Column(String(128), nullable=False)
    op = Column(String(64), nullable=False)
    params_json = Column(JSON, nullable=True)
    indicator_name = Column(String(255), nullable=False)
    score = Column(Float, nullable=True, default=None)  # default priority/quality weight; NULL = inherit the datasource's sources.score
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "datasource": self.datasource,
            "function_name": self.function_name,
            "source_table": self.source_table,
            "date_column": self.date_column,
            "value_column": self.value_column,
            "op": self.op,
            "params": self.params_json or {},
            "indicator_name": self.indicator_name,
            "score": self.score,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class IndicatorCollection(Base):
    """A named, ordered collection of indicators — a reusable bundle (e.g.
    "momentum", "trend") where each member can carry a per-collection score
    override. Distinct from `DatasourceCollection` (which groups datasources)
    and `EntityCollection` (which groups entities).

    Effective score for a member = `COALESCE(item.score, indicator_rules.score,
    sources.score)` — a 3-level chain (item override → indicator default →
    datasource default). Deleting an indicator rule cascades to its membership
    rows (real FK); the audit log row survives (denormalized indicator_name).
    """
    __tablename__ = "indicator_collections"
    __table_args__ = (UniqueConstraint("name", name="uq_indicator_collection_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(String, nullable=True)
    rule_id = Column(
        Integer, ForeignKey("rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = relationship(
        "IndicatorCollectionItem",
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "rule_id": self.rule_id,
            "item_count": len(self.items) if self.items else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class IndicatorCollectionItem(Base):
    """One entry in an indicator collection: a single indicator rule with a
    per-collection `score` override (NULL = inherit the indicator's default
    `indicator_rules.score`, which itself inherits the datasource default
    when NULL). UNIQUE(collection_id, indicator_id) makes re-adding a no-op."""
    __tablename__ = "indicator_collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "indicator_id", name="uq_indicator_collection_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer, ForeignKey("indicator_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    indicator_id = Column(
        Integer, ForeignKey("indicator_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sort_order = Column(Integer, nullable=False, default=0)
    score = Column(Float, nullable=True, default=None)  # per-collection override of indicator_rules.score
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    collection = relationship("IndicatorCollection", back_populates="items")
    indicator = relationship("IndicatorRule", lazy="selectin")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "indicator_id": self.indicator_id,
            "sort_order": self.sort_order,
            "score": self.score,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class IndicatorCollectionChange(Base):
    """Append-only audit log of every indicator-collection membership
    transition. `action` ∈ {add_in, remove_out}; `source` ∈ {manual, cron}.
    `indicator_name` is denormalized so the row survives indicator-rule
    deletion (the membership row cascades away, but the audit row does not —
    it is FK-linked only to the collection)."""
    __tablename__ = "indicator_collection_changes"
    __table_args__ = (
        UniqueConstraint(
            "collection_id", "indicator_name", "changed_at", name="uq_indicator_collection_change"
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer, ForeignKey("indicator_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    indicator_name = Column(String(128), nullable=False)  # denormalized; survives rule deletion
    action = Column(String(16), nullable=False)  # add_in | remove_out
    source = Column(String(16), nullable=False, default="manual")  # manual | cron
    reason = Column(String, nullable=True)
    changed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "indicator_name": self.indicator_name,
            "action": self.action,
            "source": self.source,
            "reason": self.reason,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# alerts-mcp domain — trigger rules over DB series + dispatch events
# (reads observations / scraw_*; writes only its own tables)
# ═══════════════════════════════════════════════════════════════


class AlertRule(Base):
    """A trigger rule: watch a series in daas.db, evaluate a condition, fire
    notifications when it matches.

    `source_table` (default `observations`) + `series_filter_json` (key→value
    WHERE pairs, e.g. {"source":"akshare","function_name":"stock_zh_a_hist",
    "indicator":"close"}) + `date_column` + `value_column` locate the series.
    Identifiers are validated against `^[A-Za-z_][A-Za-z0-9_]*$` by alerts-mcp
    before interpolation; filter values are bind params.

    `condition` is a safe DSL string (ast-walk, no eval) over `latest`/`prev`
    + whitelisted funcs (crosses_above, pct_change, …). `fire_mode` is
    `every_match` (subject to `cooldown_seconds`) or `on_change` (false→true).
    `channels_json` lists channel names, optionally with per-rule overrides
    (e.g. {"telegram": {"chat_id": "…"}}).

    `last_state` / `last_fired_at` / `last_value` persist between cron ticks
    so `on_change` + cooldown survive restarts.
    """

    __tablename__ = "alert_rules"
    __table_args__ = (UniqueConstraint("name", name="uq_alert_rule_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    source_table = Column(String(128), nullable=False, default="observations")
    series_filter_json = Column(JSON, nullable=True)
    date_column = Column(String(128), nullable=False, default="date")
    value_column = Column(String(128), nullable=False, default="value")
    condition = Column(Text, nullable=False)
    fire_mode = Column(String(16), nullable=False, default="every_match")  # every_match | on_change
    cooldown_seconds = Column(Integer, nullable=False, default=300)
    channels_json = Column(JSON, nullable=False)  # ["telegram","slack"] or {"telegram":{"chat_id":"…"}}
    message_template = Column(Text, nullable=False, default="$rule_name: $indicator = $latest")
    last_state = Column(Boolean, nullable=True)
    last_fired_at = Column(DateTime, nullable=True)
    last_value = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "enabled": bool(self.enabled),
            "source_table": self.source_table,
            "series_filter": self.series_filter_json or {},
            "date_column": self.date_column,
            "value_column": self.value_column,
            "condition": self.condition,
            "fire_mode": self.fire_mode,
            "cooldown_seconds": self.cooldown_seconds,
            "channels": self.channels_json or [],
            "message_template": self.message_template,
            "last_state": self.last_state,
            "last_fired_at": self.last_fired_at.isoformat() if self.last_fired_at else None,
            "last_value": self.last_value,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class AlertEvent(Base):
    """One dispatch of an AlertRule — inserted when a rule fires, never when it
    evaluates false. `channels_results_json` records per-channel
    `{ok, error?}`. Rule state (`last_fired_at`/`last_state`/`last_value`) is
    updated in the same transaction as this insert."""

    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rule_id = Column(
        Integer, ForeignKey("alert_rules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fired_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    value_json = Column(JSON, nullable=True)  # the series values that triggered
    message_rendered = Column(Text, nullable=True)
    channels_results_json = Column(JSON, nullable=True)  # [{channel, ok, error?}]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "fired_at": self.fired_at.isoformat() if self.fired_at else None,
            "value": self.value_json or {},
            "message_rendered": self.message_rendered,
            "channels_results": self.channels_results_json or [],
        }


# ═══════════════════════════════════════════════════════════════
# entity domain — stocks + countries, linked to daas `sources`
# ═══════════════════════════════════════════════════════════════


# Entity + EntityDatasourceLink are dropped (design D5 / task 3.7): the entity
# master lives in fd-open-data-mcp now, resolved via the gateway. Collections
# below reference entities only by natural key (entity_type, code).


class EntityCollection(Base):
    """A named collection of entities (stocks + countries) — a watchlist /
    portfolio. Distinct from `DatasourceCollection` (which groups datasources).

    `rule_json` is an optional membership rule: a JSON object with keys
    `entity_type`, `exchange`, `country_code`, `codes` (list), `name_regex`.
    When set, `sync_entity_collection` re-derives the intended member set by
    applying the rule to `entities` and records add_in / remove_out diffs in
    `entity_collection_changes`. When NULL the collection is manual.

    `rule_script` is the script analogue: a path (repo-root relative) to a
    Python file defining `members(ctx) -> list`. When set, `sync_entity_collection`
    executes the script (which can read any daas.db table via `ctx.query(sql)`)
    and diffs its result against the current members. `rule_json` and
    `rule_script` are mutually exclusive — a collection has at most one rule.
    Storing the path in the DB (rather than the source) lets workflows and
    cron (`--sync-entity-collection <name>`) re-run the rule without re-passing it.
    """
    __tablename__ = "entity_collections"
    __table_args__ = (UniqueConstraint("name", name="uq_entity_collection_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(String, nullable=True)
    rule_json = Column(JSON, nullable=True)
    rule_script = Column(String, nullable=True)
    rule_id = Column(
        Integer, ForeignKey("rules.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    items = relationship(
        "EntityCollectionItem",
        back_populates="collection",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "rule_id": self.rule_id,
            "rule": self.rule_json,
            "rule_script": self.rule_script,
            "item_count": len(self.items) if self.items else 0,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EntityCollectionItem(Base):
    """Current membership: one row per (collection, entity). Removing a member
    deletes this row and appends an `entity_collection_changes` remove_out
    event. Re-keyed to the natural key `(entity_type, code)` (design D5 /
    entity-master migration); UNIQUE(collection_id, entity_type, code) makes
    re-adding a no-op."""
    __tablename__ = "entity_collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "entity_type", "code", name="uq_entity_collection_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer, ForeignKey("entity_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type = Column(String(32), nullable=False)
    code = Column(String(64), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    added_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    added_reason = Column(String, nullable=True)

    collection = relationship("EntityCollection", back_populates="items")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "entity_type": self.entity_type,
            "code": self.code,
            "sort_order": self.sort_order,
            "added_at": self.added_at.isoformat() if self.added_at else None,
            "added_reason": self.added_reason,
        }


class EntityCollectionChange(Base):
    """Append-only audit log of every membership transition.

    `action` ∈ {add_in, remove_out}; `source` ∈ {manual, cron} (manual = a
    single add/remove call, cron = a rule-driven sync tick). Re-adding an
    entity after removal produces add_in → remove_out → add_in — the correct
    audit semantic. Re-keyed to `(entity_type, code)` (design D5 / entity-master
    migration) so the audit history survives the drop of `entities`; the unique
    key is `(collection_id, entity_type, code, changed_at)`. Re-keyed to
    `(entity_type, code)` (design D5 / entity-master migration) so the audit
    history survives the drop of `entities`."""
    __tablename__ = "entity_collection_changes"
    __table_args__ = (
        UniqueConstraint(
            "collection_id", "entity_type", "code", "changed_at",
            name="uq_entity_collection_change",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(
        Integer, ForeignKey("entity_collections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    entity_type = Column(String(32), nullable=False)
    code = Column(String(64), nullable=False)
    action = Column(String(16), nullable=False)  # add_in | remove_out
    source = Column(String(16), nullable=False, default="manual")  # manual | cron
    reason = Column(String, nullable=True)
    changed_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "collection_id": self.collection_id,
            "entity_type": self.entity_type,
            "code": self.code,
            "action": self.action,
            "source": self.source,
            "reason": self.reason,
            "changed_at": self.changed_at.isoformat() if self.changed_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# (specialist-agent + legacy workflow-step models removed in P4 dissolution)
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
# research domain - persisted research bundle linking an entity collection,
# indicator collection, rules, dashboard, and cron pipeline collection under
# one name, plus a generated markdown report. References are by name (soft),
# not FK, so attach/detach is free and cascade is handled in tool logic.
# ═══════════════════════════════════════════════════════════════


class Research(Base):
    """A persisted research bundle - a named study that ties together an entity
    collection, an indicator collection, rules, a dashboard, and a cron pipeline
    collection, and carries a generated markdown report.

    Each `*_name`/`dashboard_slug`/`pipeline_collection_name` is a soft
    by-name reference to the corresponding table (those tables enforce name/slug
    uniqueness). `component_refs` is a JSON object for auxiliary references not
    covered by the dedicated columns: `{"rules": [...], "scraw_tables": [...],
    "indicators": [...]}`. `report_md` holds the assembled markdown body;
    `report_path` is the on-disk file path. `status` is draft/active/archived.
    """

    __tablename__ = "researches"
    __table_args__ = (UniqueConstraint("name", name="uq_research_name"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    description = Column(Text, nullable=True)
    status = Column(String(16), nullable=False, default="draft")  # draft | active | archived
    entity_collection_name = Column(String(128), nullable=True)
    indicator_collection_name = Column(String(128), nullable=True)
    dashboard_slug = Column(String(128), nullable=True)
    pipeline_collection_name = Column(String(128), nullable=True)
    component_refs = Column(JSON, nullable=True)  # {rules, scraw_tables, indicators}
    report_md = Column(Text, nullable=True)
    report_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "entity_collection_name": self.entity_collection_name,
            "indicator_collection_name": self.indicator_collection_name,
            "dashboard_slug": self.dashboard_slug,
            "pipeline_collection_name": self.pipeline_collection_name,
            "component_refs": self.component_refs or {},
            "report_md": self.report_md,
            "report_path": self.report_path,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Workflow(Base):
    """A named, ordered workflow of data-fetch steps over specialist agents.

    Manifest-backed workflows (D4) carry a `version` + `manifest` (JSON string)
    + `enabled`; legacy step-row workflows predate those columns and run with
    ``version=1``/``manifest=NULL``. The UNIQUE constraint is additively widened
    to ``(name, version)`` by a named index at migration time (see
    ``workflow_database._ensure_workflow_columns``) — the legacy name-only
    autoindex is left in place (harmless while version defaults to 1).
    """

    __tablename__ = "workflows"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_workflow_name_version"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    manifest = Column(Text, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True)
    description = Column(String, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    steps = relationship(
        "WorkflowStep",
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="WorkflowStep.sort_order",
        lazy="selectin",
    )
    runs = relationship(
        "WorkflowRun",
        back_populates="workflow",
        cascade="all, delete-orphan",
        lazy="select",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "manifest": _json_loads(self.manifest) if self.manifest else None,
            "enabled": self.enabled,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "step_count": len(self.steps) if self.steps else 0,
        }


class WorkflowStep(Base):
    """One step in a workflow: a `fd-open-data-mcp` gateway call (tool + arguments, + optional deps).

    `depends_on` is a comma-separated list of prior step `sort_order` values
    whose raw output is injected as text context into this step's request.
    `on_fail` is "continue" (default — record error, keep going) or "stop".
    `model` is reserved (unused by the direct-call executor). `agent` is a
    legacy/sentinel column (nullable; new steps store `fd-open-data-mcp`).
    """

    __tablename__ = "workflow_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow_id = Column(
        Integer,
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sort_order = Column(Integer, nullable=False)
    agent = Column(String(128), nullable=True)  # legacy/sentinel (fd-open-data-mcp); unused by executor
    request = Column(Text, nullable=True)  # optional human-readable description
    tool = Column(String(128), nullable=True)  # fd-open-data-mcp tool name (e.g. 'read')
    arguments_json = Column(Text, nullable=True)  # JSON object string of tool arguments
    depends_on = Column(String(255), nullable=True)  # "1,2" → inject prior step outputs
    on_fail = Column(String(16), nullable=False, default="continue")  # continue | stop
    model = Column(String(64), nullable=True)  # optional per-step override
    enabled = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    workflow = relationship("Workflow", back_populates="steps")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "sort_order": self.sort_order,
            "agent": self.agent,
            "request": self.request,
            "tool": self.tool,
            "arguments": _json_loads(self.arguments_json) if self.arguments_json else None,
            "depends_on": [s.strip() for s in (self.depends_on or "").split(",") if s.strip()]
            if self.depends_on
            else [],
            "on_fail": self.on_fail,
            "model": self.model,
            "enabled": bool(self.enabled),
        }


class WorkflowRun(Base):
    """One execution of a workflow (full or partial). A run left `in_progress`
    is resumed by `run_workflow_step`; `run_workflow` always starts fresh."""

    __tablename__ = "workflow_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    workflow_id = Column(
        Integer,
        ForeignKey("workflows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = Column(String(16), nullable=False, default="running")  # running|in_progress|completed|failed
    started_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    finished_at = Column(DateTime, nullable=True)

    workflow = relationship("Workflow", back_populates="runs")
    results = relationship(
        "WorkflowStepResult",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="WorkflowStepResult.step_sort_order",
        lazy="selectin",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "status": self.status,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class WorkflowStepResult(Base):
    """The captured result of one step in one run. `output_json` holds the raw
    upstream data the specialist agent fetched (truncated at 1 MB with a
    `_truncated` flag when larger). `meta_json` records fallback reasons etc.
    Unique on (run_id, step_sort_order) so re-running a step is an upsert."""

    __tablename__ = "workflow_step_results"
    __table_args__ = (
        UniqueConstraint("run_id", "step_sort_order", name="uq_workflow_step_result"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        Integer,
        ForeignKey("workflow_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_sort_order = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="running")  # running|completed|failed
    output_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    meta_json = Column(Text, nullable=True)  # e.g. {"fallback":"direct","reason":"..."}
    ran_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    run = relationship("WorkflowRun", back_populates="results")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "step_sort_order": self.step_sort_order,
            "status": self.status,
            "output": _json_loads(self.output_json),
            "error": self.error,
            "meta": _json_loads(self.meta_json),
            "ran_at": self.ran_at.isoformat() if self.ran_at else None,
        }


# ═══════════════════════════════════════════════════════════════
# pdf-mcp domain - local PDF/text vector search (sqlite-vec + sentence-transformers)
# Additive, optional ([pdf] extra). The pdf_chunks_vec vec0 virtual table is
# created at runtime by pdf_database.py (SQLAlchemy does not model virtual
# tables); these two metadata tables + the key/value store are created via
# Base.metadata.create_all like every other table.
# ═══════════════════════════════════════════════════════════════


class PdfDocument(Base):
    """An ingested PDF or text document. Vectors live in the sibling
    `pdf_chunks_vec` vec0 virtual table (created at runtime); this row holds
    the document metadata + the SHA-256 `file_hash` used for dedup."""

    __tablename__ = "pdf_documents"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(512), nullable=False)
    source_type = Column(String(16), nullable=False)  # file|url|text
    source_ref = Column(String(1024), nullable=True)  # path / filing ref
    url = Column(String(1024), nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)  # SHA-256, dedup key
    page_count = Column(Integer, nullable=True)
    char_count = Column(Integer, nullable=False, default=0)
    chunk_count = Column(Integer, nullable=False, default=0)
    embedding_model = Column(String(255), nullable=False)
    embedding_dim = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="active")  # active|no_text
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "doc_id": self.id,
            "name": self.name,
            "source_type": self.source_type,
            "source_ref": self.source_ref,
            "url": self.url,
            "page_count": self.page_count,
            "chunk_count": self.chunk_count,
            "embedding_model": self.embedding_model,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class PdfChunk(Base):
    """A chunk of an ingested document. The matching vector row in
    `pdf_chunks_vec` shares this row's `id` as its `rowid`."""

    __tablename__ = "pdf_chunks"
    __table_args__ = (UniqueConstraint("doc_id", "chunk_index", name="uq_pdf_chunk_doc_index"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    doc_id = Column(
        Integer,
        ForeignKey("pdf_documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(Integer, nullable=False)
    text = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    char_start = Column(Integer, nullable=False, default=0)
    char_end = Column(Integer, nullable=False, default=0)
    token_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "chunk_index": self.chunk_index,
            "text": self.text,
            "page_number": self.page_number,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "token_count": self.token_count,
        }


class PdfMeta(Base):
    """Key/value store for the pdf group (embedding_dim, embedding_model) so
    the configured model's dimension can be compared against the stored
    `pdf_chunks_vec` dimension to detect model swaps."""

    __tablename__ = "pdf_meta"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))
