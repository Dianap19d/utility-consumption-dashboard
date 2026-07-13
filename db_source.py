#!/usr/bin/env python3
"""Pull meter readings from the Azure SQL DB (ems-dev-db) as CSV-shaped dict rows.

Returns the same six keys build_data.py already expects, so it's a drop-in for the
CSV reader. Credentials come from .env (gitignored). If DB_TABLE is blank we locate
the table/view that carries the giz_ reading columns automatically.
"""
import os, struct

# Azure CLI's well-known public client id — lets us do delegated device-code auth
# without registering our own app. Scope is the Azure SQL resource.
_AAD_CLIENT = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
_SQL_SCOPE = ["https://database.windows.net/.default"]
_SQL_COPT_SS_ACCESS_TOKEN = 1256  # ODBC attr for passing an AAD access token
_CACHE_FILE = ".msal_cache.json"

COLS = [
    "giz_captionraw", "giz_readingdatetime", "giz_institutioncode",
    "giz_readingvalue", "giz_meterserialnumber", "giz_valueunit",
]


def load_env(path=".env"):
    """Minimal .env loader (KEY=VALUE lines) so we avoid a dependency."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


DRIVER = "ODBC Driver 18 for SQL Server"


def get_aad_token():
    """Return a cached Azure AD access token for Azure SQL (silent, no prompt).
    Run the device-code login first (begin_device_flow / finish_device_flow) to
    populate the cache."""
    app, cache = _msal_app()
    for acc in app.get_accounts():
        result = app.acquire_token_silent(_SQL_SCOPE, account=acc)
        if result and "access_token" in result:
            _save_cache(cache)
            return result["access_token"]
    raise RuntimeError(
        "No cached Azure token. Run the device-code sign-in first "
        "(python3 -c 'import db_source,sys; print(db_source.begin_device_flow()[\"message\"])' "
        "then finish_device_flow()).")


def _msal_app():
    import msal
    tenant = os.environ.get("DB_TENANT") or (
        os.environ.get("DB_USER", "").split("@")[-1] or "organizations")
    cache = msal.SerializableTokenCache()
    if os.path.exists(_CACHE_FILE):
        cache.deserialize(open(_CACHE_FILE, encoding="utf-8").read())
    app = msal.PublicClientApplication(
        _AAD_CLIENT, authority=f"https://login.microsoftonline.com/{tenant}",
        token_cache=cache)
    return app, cache


def _save_cache(cache):
    if cache.has_state_changed:
        with open(_CACHE_FILE, "w", encoding="utf-8") as fh:
            fh.write(cache.serialize())
        os.chmod(_CACHE_FILE, 0o600)


def begin_device_flow(flow_path="/tmp/aad_flow.json"):
    """Phase 1: start device flow, persist it, return the human message + code."""
    import json
    load_env()
    app, _ = _msal_app()
    flow = app.initiate_device_flow(scopes=_SQL_SCOPE)
    if "user_code" not in flow:
        raise RuntimeError(f"Device flow failed: {flow.get('error_description')}")
    with open(flow_path, "w", encoding="utf-8") as fh:
        json.dump(flow, fh)
    return flow


def finish_device_flow(flow_path="/tmp/aad_flow.json"):
    """Phase 2: block until the user completes sign-in, then cache the token."""
    import json
    load_env()
    app, cache = _msal_app()
    flow = json.load(open(flow_path, encoding="utf-8"))
    result = app.acquire_token_by_device_flow(flow)  # polls until done/expired
    if "access_token" not in result:
        raise RuntimeError(
            f"Auth failed: {result.get('error')}: {result.get('error_description')}")
    _save_cache(cache)
    return result["access_token"]


def _token_attr(token):
    """Pack an access token the way the ODBC driver expects (len-prefixed UTF-16-LE)."""
    tb = token.encode("utf-16-le")
    return {_SQL_COPT_SS_ACCESS_TOKEN: struct.pack("=i", len(tb)) + tb}


def _connect():
    """Connect via ODBC Driver 18. DB_AUTH selects the method:
      aad-token (default)  -> device-code login, token cached, no webview needed
      aad-interactive      -> browser login + MFA (needs a GUI session)
      aad-password         -> Entra ID user + password (fails if MFA enforced)
      sql                  -> plain SQL Server login + password
    """
    import pyodbc
    load_env()
    server, name = os.environ.get("DB_SERVER"), os.environ.get("DB_NAME")
    user, pwd = os.environ.get("DB_USER"), os.environ.get("DB_PASSWORD")
    auth = os.environ.get("DB_AUTH", "aad-token").strip().lower()
    if not server or not name:
        raise RuntimeError("Set DB_SERVER and DB_NAME in .env.")

    base = (
        f"Driver={{{DRIVER}}};Server=tcp:{server},1433;Database={name};"
        "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=120;"
    )
    if auth == "aad-token":
        return pyodbc.connect(base, timeout=120, attrs_before=_token_attr(get_aad_token()))
    if auth == "sql":
        cs = base + f"UID={user};PWD={pwd};"
    elif auth == "aad-password":
        cs = base + f"Authentication=ActiveDirectoryPassword;UID={user};PWD={pwd};"
    else:  # aad-interactive
        cs = base + f"Authentication=ActiveDirectoryInteractive;UID={user};"
    return pyodbc.connect(cs, timeout=120)


def _find_table(cur):
    """Return schema.table (or view) whose columns cover all COLS."""
    cur.execute(
        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME "
        "FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME IN (%s)"
        % ",".join(["?"] * len(COLS)),
        tuple(COLS),
    )
    have = {}
    for schema, table, col in cur.fetchall():
        have.setdefault((schema, table), set()).add(col)
    for (schema, table), cols in have.items():
        if set(COLS) <= cols:
            return f"[{schema}].[{table}]"
    raise RuntimeError(
        "Could not find a table/view carrying columns %s. Set DB_TABLE in .env."
        % ", ".join(COLS)
    )


def fetch_rows():
    """List of dicts keyed exactly like the CSV's DictReader rows."""
    conn = _connect()
    try:
        cur = conn.cursor()
        table = os.environ.get("DB_TABLE", "").strip()
        table = f"[{table}]" if (table and "." not in table) else (
            "[%s].[%s]" % tuple(table.split(".", 1)) if table else _find_table(cur))
        select = ", ".join(f"[{c}]" for c in COLS)
        cur.execute(f"SELECT {select} FROM {table}")
        rows = []
        for rec in cur.fetchall():
            row = {}
            for c, val in zip(COLS, rec):
                # normalise to the string shape the CSV path produces
                row[c] = "" if val is None else str(val)
            rows.append(row)
        return rows, table
    finally:
        conn.close()


if __name__ == "__main__":
    rows, table = fetch_rows()
    print(f"Table: {table}\nRows : {len(rows)}")
    if rows:
        print("Sample:", rows[0])
