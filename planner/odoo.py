"""Connections to Odoo, following ProcessPro's rule: write through XML-RPC business methods as the
real user, read with SQL. Nothing here writes Odoo tables with SQL.
"""
import os
import sys
import xmlrpc.client

import pandas as pd
import psycopg

from planner import config


def read_env_file():
    values = {}
    if os.path.exists(".env"):
        with open(".env", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    values[key] = value
    return values


ENV = read_env_file()


def setting(name, default=None):
    return os.getenv(name) or ENV.get(name) or default


URL = f"http://127.0.0.1:{setting('ODOO_PORT', '8069')}"
DATABASE = setting("ODOO_DB", config.TARGET_DB)
# Local-only sandbox defaults, the same ones ProcessPro uses. Override them in .env.
ADMIN_PASSWORD = setting("ODOO_PASSWORD", "admin")
USER_PASSWORD = setting("P2P_USER_PASSWORD", "p2p-sandbox")
MASTER_PASSWORD = setting("ODOO_MASTER_PASSWORD", "admin")


def login_of(name):
    return "admin" if name == "admin" else name.lower().replace(" ", ".")


class Odoo:
    """XML-RPC client that acts as a named person, so Odoo records who did what."""

    def __init__(self, database=None):
        self.database = database or DATABASE
        self.common = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/common")
        self.models = xmlrpc.client.ServerProxy(f"{URL}/xmlrpc/2/object", allow_none=True)
        self.uids = {}

    def uid(self, user):
        if user not in self.uids:
            password = ADMIN_PASSWORD if user == "admin" else USER_PASSWORD
            uid = self.common.authenticate(self.database, login_of(user), password, {})
            if not uid:
                sys.exit(f"Login failed for {login_of(user)} on {self.database}")
            self.uids[user] = uid
        return self.uids[user]

    def call(self, user, model, method, *args, **kwargs):
        password = ADMIN_PASSWORD if user == "admin" else USER_PASSWORD
        try:
            return self.models.execute_kw(self.database, self.uid(user), password, model, method,
                                          list(args), kwargs)
        except xmlrpc.client.Fault as fault:
            # The method worked but returned nothing, which XML-RPC can't send back. Not an error.
            if "cannot marshal None" in fault.faultString:
                return None
            raise

    def admin(self, model, method, *args, **kwargs):
        return self.call("admin", model, method, *args, **kwargs)

    def ref(self, xml_id):
        module, name = xml_id.split(".")
        return self.admin("ir.model.data", "check_object_reference", module, name)[1]

    def upsert(self, model, domain, values):
        """Update the record that matches domain, or create it. Returns its id."""
        found = self.admin(model, "search", domain, limit=1)
        if found:
            self.admin(model, "write", found, values)
            return found[0]
        return self.admin(model, "create", values)


def connect(database=None):
    """Read-only use: SQL straight from Postgres, like a process-mining or planning extract."""
    return psycopg.connect(host="127.0.0.1", port=int(setting("DB_PORT", 5432)), dbname=database or DATABASE,
                           user=setting("DB_USER"), password=setting("DB_PASSWORD"))


def sql(connection, query, params=None):
    cursor = connection.execute(query, params)
    return pd.DataFrame(cursor.fetchall(), columns=[column.name for column in cursor.description])
