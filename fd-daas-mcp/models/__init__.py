from models.models import (  # noqa: F401
    Base,
    # gateway-mcp (upstream registry)
    GatewayUpstream,
    # workflow-mcp (manifest-driven runs)
    Workflow,
    WorkflowStep,
    WorkflowRun,
    WorkflowStepResult,
    # cron-mcp
    Schedule,
    Execution,
    Task,
    # daas-mcp
    DaasSource,
    DaasFunction,
    DaasFunctionColumn,
    Observation,
    Category,
    DatasourceForm,
    DatasourceSection,
    DatasourceCollection,
    DatasourceCollectionItem,
    # customer-dataset-mcp (wire 客户数据引擎：登记/修正/摄取/血缘)
    # 508fa75 ponytail 清理时误删本组导出 → cell 镜像 customer_dataset 源
    # 整体加载失败（2026-10-31 真机实证 failed=1、0 工具）；本组必须导出。
    CustomerDataset,
    CustomerDatasetCorrection,
    CustomerDatasetIngest,
    CustomerDatasetLineage,
    # daas-mcp pipeline collections (managed fetch+cron collections)
    PipelineCollection,
    PipelineCollectionItem,
    # dashboard
    Datasource,
    DatasourceColumn,
    Dashboard,
    # composite-mcp
    Composite,
    Upstream,
    CompositeTool,
    CompositeChain,
    # daas-mcp (process tools — relocated from process-mcp)
    Rule,
    ProcessResult,
    IndicatorRule,
    # daas-mcp indicator collections (named groups of indicators + score overrides + audit log)
    IndicatorCollection,
    IndicatorCollectionItem,
    IndicatorCollectionChange,
    # daas-mcp entity collections (watchlists keyed on natural (entity_type, code))
    EntityCollection,
    EntityCollectionItem,
    EntityCollectionChange,
    # research-mcp (persisted research bundle: entity+indicator collections, rules, dashboard, cron)
    Research,
    # alerts-mcp
    AlertRule,
    AlertEvent,
    # pdf-mcp (local PDF/text vector search - optional [pdf] extra)
    PdfDocument,
    PdfChunk,
    PdfMeta,
)
