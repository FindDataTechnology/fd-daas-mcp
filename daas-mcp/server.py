"""
MCP Server for DAAS — multi-source data access.

Exposes tools that Claude Code can invoke directly:
  list_sources       — list all data sources
  search_functions   — search the DAAS registry
  get_function_detail — get function details (params, columns, description)
  list_categories    — list all categories with counts
  fetch_data         — execute a data function and return results as JSON
"""
from __future__ import annotations

import os
import sys
import json
from pathlib import Path
from dotenv import load_dotenv

# Load the repo-root .env (cli-anything/.env, where DAAS_DATABASE_URL is
# defined) first, then this MCP's own .env with override=True. parents[2]
# reaches the repo root from mcp/daas-mcp/server.py (parent.parent is mcp/,
# whose .env doesn't exist).
REPO_ROOT = Path(__file__).resolve().parents[2]  # cli-anything/
load_dotenv(REPO_ROOT / ".env")
load_dotenv(Path(__file__).parent / ".env", override=True)

from fastmcp import FastMCP

app = FastMCP(name="daas-mcp")

# Ensure the harness package is importable
_HARNESS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "daas-agent-harness")
if _HARNESS_ROOT not in sys.path:
    sys.path.insert(0, _HARNESS_ROOT)

# Register tools
from daas_tools import (
    list_sources,
    search_functions,
    get_function_detail,
    list_categories,
    fetch_data,
    # management tools
    create_datasource,
    update_datasource,
    delete_datasource,
    create_category,
    move_category,
    delete_category,
    get_category_tree,
    add_form,
    add_section,
    list_forms,
    create_collection,
    add_to_collection,
    list_collection_items,
    remove_from_collection,
    list_collections,
    rename_collection,
    update_collection,
    delete_collection,
    reorder_collection_items,
    set_collection_item_score,
    search_datasources,
)
from entity_tools import (
    search_entities,
    get_entity,
    list_entities,
    get_entity_coverage,
    link_entity_datasource,
    unlink_entity_datasource,
)
from entity_collection_tools import (
    create_entity_collection,
    list_entity_collections,
    get_entity_collection,
    update_entity_collection,
    delete_entity_collection,
    add_entity_to_collection,
    remove_entity_from_collection,
    list_entity_collection_items,
    reorder_entity_collection_items,
    list_entity_collection_changes,
    sync_entity_collection,
    cli_sync_entity_collection,
)
from indicator_collection_tools import (
    create_indicator_collection,
    list_indicator_collections,
    get_indicator_collection,
    update_indicator_collection,
    delete_indicator_collection,
    add_indicator_to_collection,
    remove_indicator_from_collection,
    list_indicator_collection_items,
    reorder_indicator_collection_items,
    set_indicator_collection_item_score,
    list_indicator_collection_changes,
    sync_indicator_collection,
    cli_sync_indicator_collection,
)
from pipeline_tools import (
    create_pipeline_collection,
    list_pipeline_collections,
    get_pipeline_collection,
    delete_pipeline_collection,
    list_pipeline_items,
    add_pipeline_item,
    remove_pipeline_item,
    enable_pipeline_item,
    disable_pipeline_item,
    update_pipeline_item,
    sync_pipeline_cron,
    cli_fetch_item,
    cli_register_cron,
    cli_unregister_cron,
    cli_sync_cron,
)
# process tools (relocated from process-mcp): LLM extraction + math indicators
from process_api import (
    list_models,
    list_source_tables,
    extract_text,
    extract_image,
    extract_file,
    list_indicator_ops,
    create_indicator,
    list_indicators,
    get_indicator,
    update_indicator,
    set_indicator_score,
    delete_indicator,
    run_indicator,
    calculate_indicator,
    cli_run_indicator,
)
# unified rule tools (json/script/position/llm rule store) - subsume the old
# process_api rule-CRUD tools (now backed by the `rules` table).
from rule_tools import (
    create_rule,
    list_rules,
    get_rule,
    update_rule,
    delete_rule,
    test_rule,
    run_rule,
    cli_run_rule,
)

app.tool(list_sources)
app.tool(search_functions)
app.tool(get_function_detail)
app.tool(list_categories)
app.tool(fetch_data)
# management tools
app.tool(create_datasource)
app.tool(update_datasource)
app.tool(delete_datasource)
app.tool(create_category)
app.tool(move_category)
app.tool(delete_category)
app.tool(get_category_tree)
app.tool(add_form)
app.tool(add_section)
app.tool(list_forms)
app.tool(create_collection)
app.tool(add_to_collection)
app.tool(list_collection_items)
app.tool(remove_from_collection)
app.tool(list_collections)
app.tool(rename_collection)
app.tool(update_collection)
app.tool(delete_collection)
app.tool(reorder_collection_items)
app.tool(set_collection_item_score)
app.tool(search_datasources)
# entity tools
app.tool(search_entities)
app.tool(get_entity)
app.tool(list_entities)
app.tool(get_entity_coverage)
app.tool(link_entity_datasource)
app.tool(unlink_entity_datasource)
# entity collection tools (named groups of entities + add-in/remove-out history)
app.tool(create_entity_collection)
app.tool(list_entity_collections)
app.tool(get_entity_collection)
app.tool(update_entity_collection)
app.tool(delete_entity_collection)
app.tool(add_entity_to_collection)
app.tool(remove_entity_from_collection)
app.tool(list_entity_collection_items)
app.tool(reorder_entity_collection_items)
app.tool(list_entity_collection_changes)
app.tool(sync_entity_collection)

# indicator collection tools (named groups of indicators + per-item score override + audit log)
app.tool(create_indicator_collection)
app.tool(list_indicator_collections)
app.tool(get_indicator_collection)
app.tool(update_indicator_collection)
app.tool(delete_indicator_collection)
app.tool(add_indicator_to_collection)
app.tool(remove_indicator_from_collection)
app.tool(list_indicator_collection_items)
app.tool(reorder_indicator_collection_items)
app.tool(set_indicator_collection_item_score)
app.tool(list_indicator_collection_changes)
app.tool(sync_indicator_collection)
# pipeline collection tools (managed fetch+cron collections)
app.tool(create_pipeline_collection)
app.tool(list_pipeline_collections)
app.tool(get_pipeline_collection)
app.tool(delete_pipeline_collection)
app.tool(list_pipeline_items)
app.tool(add_pipeline_item)
app.tool(remove_pipeline_item)
app.tool(enable_pipeline_item)
app.tool(disable_pipeline_item)
app.tool(update_pipeline_item)
app.tool(sync_pipeline_cron)
# process tools (relocated from process-mcp): LLM extraction (11) + indicators (8)
app.tool(list_models)
app.tool(list_source_tables)
app.tool(create_rule)
app.tool(list_rules)
app.tool(get_rule)
app.tool(update_rule)
app.tool(delete_rule)
app.tool(run_rule)
app.tool(test_rule)
app.tool(extract_text)
app.tool(extract_image)
app.tool(extract_file)
app.tool(list_indicator_ops)
app.tool(create_indicator)
app.tool(list_indicators)
app.tool(get_indicator)
app.tool(update_indicator)
app.tool(set_indicator_score)
app.tool(delete_indicator)
app.tool(run_indicator)
app.tool(calculate_indicator)

if __name__ == "__main__":
    # CLI branches for cron-mcp shell tasks: run a path in-process and exit.
    if "--fetch-item" in sys.argv:
        i = sys.argv.index("--fetch-item")
        if i + 1 >= len(sys.argv):
            print('{"error": "--fetch-item requires an item id"}')
            sys.exit(2)
        try:
            item_id = int(sys.argv[i + 1])
        except ValueError:
            print('{"error": "--fetch-item requires an integer item id"}')
            sys.exit(2)
        sys.exit(cli_fetch_item(item_id))
    if "--register-cron" in sys.argv:
        i = sys.argv.index("--register-cron")
        if i + 1 >= len(sys.argv):
            print('{"error": "--register-cron requires an item id"}')
            sys.exit(2)
        try:
            item_id = int(sys.argv[i + 1])
        except ValueError:
            print('{"error": "--register-cron requires an integer item id"}')
            sys.exit(2)
        sys.exit(cli_register_cron(item_id))
    if "--unregister-cron" in sys.argv:
        i = sys.argv.index("--unregister-cron")
        if i + 1 >= len(sys.argv):
            print('{"error": "--unregister-cron requires an item id"}')
            sys.exit(2)
        try:
            item_id = int(sys.argv[i + 1])
        except ValueError:
            print('{"error": "--unregister-cron requires an integer item id"}')
            sys.exit(2)
        sys.exit(cli_unregister_cron(item_id))
    if "--sync-cron" in sys.argv:
        sys.exit(cli_sync_cron())
    # process-tool CLI branches (relocated from process-mcp): run a rule or
    # indicator in-process, print a JSON summary, and exit without starting
    # the stdio server. cron-mcp tasks invoke these via
    # `uv run --directory mcp/daas-mcp python server.py --run-rule <name>`.
    if "--run-rule" in sys.argv:
        i = sys.argv.index("--run-rule")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--run-rule requires a rule name"}))
            sys.exit(2)
        sys.exit(cli_run_rule(sys.argv[i + 1]))
    if "--run-indicator" in sys.argv:
        i = sys.argv.index("--run-indicator")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--run-indicator requires an indicator name"}))
            sys.exit(2)
        sys.exit(cli_run_indicator(sys.argv[i + 1]))
    # entity-collection sync CLI branch: run sync_entity_collection(name)
    # in-process, print a JSON summary, exit. cron-mcp tasks invoke this via
    # `uv run --directory mcp/daas-mcp python server.py --sync-entity-collection <name>`.
    if "--sync-entity-collection" in sys.argv:
        i = sys.argv.index("--sync-entity-collection")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--sync-entity-collection requires a collection name"}))
            sys.exit(2)
        sys.exit(cli_sync_entity_collection(sys.argv[i + 1]))
    # indicator-collection sync CLI branch: run sync_indicator_collection(name)
    # in-process, print a JSON summary, exit. cron-mcp tasks invoke this via
    # `uv run --directory mcp/daas-mcp python server.py --sync-indicator-collection <name>`.
    if "--sync-indicator-collection" in sys.argv:
        i = sys.argv.index("--sync-indicator-collection")
        if i + 1 >= len(sys.argv):
            print(json.dumps({"error": "--sync-indicator-collection requires a collection name"}))
            sys.exit(2)
        sys.exit(cli_sync_indicator_collection(sys.argv[i + 1]))

    app.run(transport="stdio", show_banner=False)
