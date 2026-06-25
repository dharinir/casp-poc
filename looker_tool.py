import datetime
import decimal
import json
import os
import sys

import google.auth
from google.cloud import bigquery
from googleapiclient import discovery
import looker_sdk
import looker_sdk.error
import mcp.server

mcp = mcp.server.FastMCP("Looker Alert Helper")


@mcp.tool()
def looker_get_alert_context(query_id: str) -> str:
  """Fetches the Looker query details, applied filters, and the underlying BigQuery base table."""
  try:
    # Initialize the Looker SDK (requires looker.ini or environment variables)
    sdk = looker_sdk.init40(
        config_file=os.getenv("LOOKERSDK_INI", "looker.ini")
    )

    # 1. Get the Query details (fields, filters, model, and explore)
    query = sdk.query(query_id=query_id)

    # 2. Look up the Explore in LookML to find the actual SQL table name
    explore = sdk.lookml_model_explore(
        lookml_model_name=query.model, explore_name=query.view
    )

    # 3. Extract the underlying BigQuery table from the lookml explore sql_table_name
    base_table = (
        explore.sql_table_name if explore.sql_table_name else explore.name
    )

    # Clean up Looker's variable syntax if present (e.g., removing `${view.SQL_TABLE_NAME}`)
    base_table = base_table.replace("`", "")

    context = {
        "metrics_and_dimensions": query.fields,
        "applied_filters": query.filters,
        "timeframe": "See filters for exact dates",
        "bigquery_base_table": base_table,
    }

    return json.dumps(context, indent=2)

  except looker_sdk.error.SDKError as e:
    return json.dumps({"error": f"Looker SDK Error: {str(e)}"})
  except Exception as e:  # pylint: disable=broad-except
    return json.dumps({"error": str(e)})


@mcp.tool()
def bq_graph_get_connected_tables(source_table_name: str) -> str:
  """Queries the BigQuery relationship graph to find connected tables and their join keys.

  Format expected: 'project.dataset.table' or 'dataset.table'
  """
  try:
    # Parse the input table name
    parts = source_table_name.split(".")
    table_id = parts[-1]
    dataset_id = parts[-2]
    project_id = parts[-3] if len(parts) == 3 else "dharinir-lags-codelab"
    client = bigquery.Client(project=project_id)

    # Query BigQuery's native graph/foreign keys
    # (Update this SQL if you are using a custom graph lookup table)
    sql_query = f"""
            SELECT 
                kc.table_name AS connected_table,
                kc.column_name AS join_key,
                'foreign_key' AS relationship_type
            FROM 
                `{project_id}.{dataset_id}.INFORMATION_SCHEMA.KEY_COLUMN_USAGE` kc
            JOIN 
                `{project_id}.{dataset_id}.INFORMATION_SCHEMA.CONSTRAINT_COLUMN_USAGE` cc
            ON kc.constraint_name = cc.constraint_name
            WHERE cc.table_name LIKE '{table_id}'
        """

    query_job = client.query(sql_query)
    results = [dict(row) for row in query_job]

    if not results:
      return json.dumps({
          "message": (
              f"No connected tables found for {source_table_name} in the Graph."
          )
      })

    return json.dumps(results, indent=2)

  except Exception as e:  # pylint: disable=broad-except
    return json.dumps({"error": f"Graph Traversal failed: {str(e)}"})


@mcp.tool()
def kc_get_table_schema(table_name: str) -> str:
  """Retrieves the column schema and descriptions from the Dataplex Knowledge Catalog.

  Input format: 'project.dataset.table'
  """
  try:
    service = discovery.build("datacatalog", "v1")

    # 1. Format the BigQuery table name into a Data Catalog linked resource string
    parts = table_name.split(".")
    if len(parts) != 3:
      return json.dumps(
          {"error": "table_name must be in format 'project.dataset.table'"}
      )

    linked_resource = f"//bigquery.googleapis.com/projects/{parts[0]}/datasets/{parts[1]}/tables/{parts[2]}"

    # 2. Look up the entry in Knowledge Catalog
    entry = service.entries().lookup(linkedResource=linked_resource).execute()

    # 3. Extract the schema (Columns, types, and human-readable descriptions)
    schema_info = []
    schema = entry.get("schema", {})
    columns = schema.get("columns", [])
    for col in columns:
      schema_info.append({
          "column_name": col.get("column", ""),
          "data_type": col.get("type", ""),
          "description": col.get("description", ""),
      })

    table_metadata = {
        "table_name": table_name,
        "table_description": entry.get("description", ""),
        "columns": schema_info,
    }

    return json.dumps(table_metadata, indent=2)

  except Exception as e:  # pylint: disable=broad-except
    return json.dumps({"error": f"Knowledge Catalog lookup failed: {str(e)}"})


class BigQueryJSONEncoder(json.JSONEncoder):

  def default(self, obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
      return obj.isoformat()
    if isinstance(obj, decimal.Decimal):
      return float(obj)
    return super().default(obj)


@mcp.tool()
def execute_bigquery_sql(sql_query: str) -> str:
  """Executes a raw Standard SQL query against BigQuery and returns the results."""
  try:
    client = bigquery.Client(project="dharinir-lags-codelab")

    # Run the query
    query_job = client.query(sql_query)

    # Fetch results
    results = [dict(row) for row in query_job]

    # If the query returns a massive dataset, truncate it so we don't blow up the LLM token window
    if len(results) > 50:
      truncated_msg = {
          "warning": (
              f"Result set too large ({len(results)} rows). Truncated to 50."
          )
      }
      results = results[:50]
      results.append(truncated_msg)

    return json.dumps(results, cls=BigQueryJSONEncoder, indent=2)

  except Exception as e:  # pylint: disable=broad-except
    # Returning the error as a string is crucial!
    # This allows JetSki to read the syntax error and fix its own SQL.
    return json.dumps({
        "error": (
            f"BigQuery Execution failed: {str(e)}. Please check your SQL syntax"
            " and try again."
        )
    })


@mcp.tool()
def kc_search_looker_assets(search_keywords: str) -> str:
  """Searches the Knowledge Catalog specifically for Looker modeled assets."""
  try:
    credentials, _ = google.auth.default()
    quota_project = "dharinir-lags-codelab"
    credentials = credentials.with_quota_project(quota_project)
    service = discovery.build("datacatalog", "v1", credentials=credentials)

    query = f"system=looker {search_keywords}"

    body = {
        "scope": {"includeOrgIds": ["433637338589"]},
        "query": query,
        "orderBy": "relevance",
    }

    request = service.catalog().search(body=body)
    response = request.execute()

    results = []
    for result in response.get("results", []):
      results.append({
          "asset_name": result.get("relativeResourceName", ""),
          "asset_type": result.get("searchResultSubtype", ""),
          "description": result.get("description", ""),
      })

    if not results:
      return json.dumps(
          {"message": f"No Looker assets found matching '{search_keywords}'."}
      )

    return json.dumps(results, indent=2)

  except Exception as e:
    return json.dumps({"error": f"Knowledge Catalog Search failed: {str(e)}"})


@mcp.tool()
def kc_get_metadata_and_lineage(table_name: str) -> str:
  """Gets Schema from Dataplex and checks downstream Lineage.

  Args:
      table_name: The table name in 'project.dataset.table' format.
  """
  try:
    parts = table_name.split(".")
    if len(parts) != 3:
      return json.dumps({"error": "Table name must be 'project.dataset.table'"})

    project_id, dataset_id, table_id = parts[0], parts[1], parts[2]

    # We need credentials with quota project for both APIs
    credentials, _ = google.auth.default()
    quota_project = "dharinir-lags-codelab"
    credentials = credentials.with_quota_project(quota_project)

    # 1. Fetch Schema from Knowledge Catalog (Data Catalog)
    catalog_service = discovery.build(
        "datacatalog", "v1", credentials=credentials
    )
    linked_resource = (
        f"//bigquery.googleapis.com/projects/{project_id}/"
        f"datasets/{dataset_id}/tables/{table_id}"
    )

    entry = (
        catalog_service.entries()
        .lookup(linkedResource=linked_resource)
        .execute()
    )

    schema_info = []
    schema = entry.get("schema", {})
    columns = schema.get("columns", [])
    for col in columns:
      schema_info.append({
          "column": col.get("column", ""),
          "type": col.get("type", ""),
          "description": col.get("description", ""),
      })

    # 2. Fetch Lineage to check for downstream Looker consumption
    lineage_service = discovery.build(
        "datalineage", "v1", credentials=credentials
    )
    target_node = f"bigquery:{project_id}.{dataset_id}.{table_id}"

    # Parent format: projects/{project}/locations/{location}
    # We use 'us' as default location.
    parent = f"projects/{project_id}/locations/us"

    body = {"source": {"fullyQualifiedName": target_node}}

    downstream_systems = []
    try:
      # Call searchLinks
      request = (
          lineage_service.projects()
          .locations()
          .searchLinks(parent=parent, body=body)
      )
      response = request.execute()

      links = response.get("links", [])
      for link in links:
        target_fqn = link.get("target", {}).get("fullyQualifiedName", "")
        if target_fqn:
          downstream_systems.append(target_fqn)
    except Exception:  # pylint: disable=broad-except
      # Lineage API might not be enabled or permission denied, handle gracefully
      pass

    has_looker_assets = any(
        "looker" in sys.lower() for sys in downstream_systems
    )

    return json.dumps(
        {
            "table": table_name,
            "description": entry.get("description", "No description provided."),
            "schema": schema_info,
            "lineage": {
                "downstream_nodes_found": len(downstream_systems),
                "is_modeled_in_looker": has_looker_assets,
            },
        },
        indent=2,
    )

  except Exception as e:  # pylint: disable=broad-except
    return json.dumps({"error": f"Metadata/Lineage lookup failed: {str(e)}"})


@mcp.tool()
def kc_find_tables_for_looker_views(
    views: list[str],
    project_id: str = "dharinir-lags-codelab",
    datasets: list[str] = ["sales_inv", "looker_ds"],
) -> str:
  """Finds BigQuery tables for a list of Looker view names using lineage and fallbacks.

  Args:
      views: List of Looker view names (e.g. ['inventory', 'products',
        'store']).
      project_id: The GCP project ID.
      datasets: List of BigQuery datasets to search in.
  """
  try:
    credentials, _ = google.auth.default()
    credentials = credentials.with_quota_project(project_id)

    bq_service = discovery.build("bigquery", "v2", credentials=credentials)
    lineage_service = discovery.build(
        "datalineage", "v1", credentials=credentials
    )
    parent = f"projects/{project_id}/locations/us-central1"

    mapping = {}
    remaining_views = set(views)

    # 1. Try mapping via Lineage
    for dataset_id in datasets:
      if not remaining_views:
        break
      try:
        tables_response = (
            bq_service.tables()
            .list(projectId=project_id, datasetId=dataset_id)
            .execute()
        )
        tables = tables_response.get("tables", [])

        for table in tables:
          table_id = table.get("tableReference", {}).get("tableId")
          table_fqn = f"bigquery:{project_id}.{dataset_id}.{table_id}"

          body = {"source": {"fullyQualifiedName": table_fqn}}
          try:
            response = (
                lineage_service.projects()
                .locations()
                .searchLinks(parent=parent, body=body)
                .execute()
            )
            links = response.get("links", [])
            for link in links:
              target_fqn = link.get("target", {}).get("fullyQualifiedName", "")
              for view in list(remaining_views):
                if target_fqn.lower().endswith(f".{view.lower()}"):
                  mapping[view] = {
                      "table": f"{project_id}.{dataset_id}.{table_id}",
                      "method": "lineage",
                  }
                  remaining_views.remove(view)
          except Exception:
            pass
      except Exception:
        pass

    # 2. Try mapping via Fallback (name matching)
    if remaining_views:
      for dataset_id in datasets:
        try:
          tables_response = (
              bq_service.tables()
              .list(projectId=project_id, datasetId=dataset_id)
              .execute()
          )
          tables = tables_response.get("tables", [])

          for table in tables:
            table_id = table.get("tableReference", {}).get("tableId")

            for view in list(remaining_views):
              v_lower = view.lower()
              t_lower = table_id.lower()

              match = False
              if v_lower == t_lower:
                match = True
              elif v_lower.endswith("s") and v_lower[:-1] == t_lower:
                match = True
              elif t_lower.endswith("s") and t_lower[:-1] == v_lower:
                match = True

              if match:
                mapping[view] = {
                    "table": f"{project_id}.{dataset_id}.{table_id}",
                    "method": "fallback",
                }
                remaining_views.remove(view)
        except Exception:
          pass

    # Unmatched views
    for view in remaining_views:
      mapping[view] = {"table": None, "method": "unmatched"}

    return json.dumps(mapping, indent=2)

  except Exception as e:
    return json.dumps({"error": f"Failed to map Looker views: {str(e)}"})


@mcp.tool()
def looker_list_dashboards() -> str:
  """Lists all dashboards available in the Looker instance."""
  try:
    sdk = looker_sdk.init40(
        config_file=os.getenv("LOOKERSDK_INI", "looker.ini")
    )
    dashboards = sdk.all_dashboards(fields="id,title,description")
    results = [
        {"id": d.id, "title": d.title, "description": d.description}
        for d in dashboards
    ]
    return json.dumps(results, indent=2)
  except Exception as e:
    return json.dumps({"error": str(e)})


@mcp.tool()
def looker_get_dashboard(dashboard_id: str) -> str:
  """Retrieves Looker dashboard metadata and the data for each tile."""
  try:
    sdk = looker_sdk.init40(
        config_file=os.getenv("LOOKERSDK_INI", "looker.ini")
    )
    dashboard = sdk.dashboard(dashboard_id=dashboard_id)

    tiles = []
    for element in dashboard.dashboard_elements or []:
      tile_info = {
          "title": element.title or element.title_text,
          "type": element.type,
      }

      query_id = None
      if element.query_id:
        query_id = element.query_id
      elif element.result_maker and element.result_maker.query_id:
        query_id = element.result_maker.query_id

      if query_id:
        try:
          # Fetch query metadata
          query_obj = sdk.query(query_id=str(query_id))
          tile_info["model"] = query_obj.model
          tile_info["explore"] = query_obj.view
          tile_info["fields"] = query_obj.fields

          query_data_str = sdk.run_query(
              query_id=query_id, result_format="json"
          )
          tile_info["data"] = json.loads(query_data_str)
        except Exception as e:
          tile_info["error"] = f"Failed to run query {query_id}: {str(e)}"

      tiles.append(tile_info)

    results = {
        "id": dashboard.id,
        "title": dashboard.title,
        "description": dashboard.description,
        "tiles": tiles,
    }
    return json.dumps(results, indent=2)
  except Exception as e:
    return json.dumps({"error": f"Failed to get dashboard: {str(e)}"})


if __name__ == "__main__":
  if len(sys.argv) > 1 and sys.argv[1] == "--cli":
    if len(sys.argv) < 3:
      print("Usage: looker_tool.py --cli <tool_name> [args]")
      sys.exit(1)
    tool_name = sys.argv[2]
    args = sys.argv[3:]
    if tool_name == "kc_search_looker_assets":
      print(kc_search_looker_assets(*args))
    elif tool_name == "bq_graph_get_connected_tables":
      print(bq_graph_get_connected_tables(*args))
    elif tool_name == "kc_get_metadata_and_lineage":
      print(kc_get_metadata_and_lineage(*args))
    elif tool_name == "kc_find_tables_for_looker_views":
      views_list = args[0].split(",") if "," in args[0] else args
      print(kc_find_tables_for_looker_views(views_list))
    elif tool_name == "execute_bigquery_sql":
      print(execute_bigquery_sql(" ".join(args)))
    elif tool_name == "looker_get_alert_context":
      print(looker_get_alert_context(*args))
    elif tool_name == "looker_list_dashboards":
      print(looker_list_dashboards())
    elif tool_name == "looker_get_dashboard":
      print(looker_get_dashboard(*args))
    elif tool_name == "print_project_files":
      sdk = looker_sdk.init40(
          config_file=os.getenv("LOOKERSDK_INI", "looker.ini")
      )
      projects = sdk.all_projects(fields="id,name")
      print("Projects found in Looker:")
      for p in projects:
        print(f"Project ID: {p.id}, Name: {p.name}")
      # Try using the first project or dharinir-lags-codelab
      project_id = projects[0].id if projects else "dharinir-lags-codelab"
      print(f"\nFetching files for project_id: {project_id}")
      files = sdk.all_project_files(project_id=project_id)
      for f in files:
        print(f"File: {f.path} (type: {f.type})")
        if f.type in ["model", "view"]:
          try:
            encoded_proj = sdk.encode_path_param(project_id)
            content = sdk.get(
                path=f"/projects/{encoded_proj}/file/content",
                structure=str,
                query_params={"file_path": f.path},
            )
            print(f"--- CONTENT FOR {f.path} ---")
            print(content)
            print("----------------------------\n")
          except Exception as e:
            print(f"Error reading file {f.path}: {e}")
    else:
      print(f"Unknown tool: {tool_name}")
  else:
    mcp.run()
