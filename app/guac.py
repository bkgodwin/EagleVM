import base64
from urllib.parse import quote

import requests
from flask import current_app


class GuacamoleError(RuntimeError):
    pass


class GuacamoleClient:
    def __init__(self):
        cfg = current_app.config
        self.base_url = cfg["GUAC_BASE_URL"].rstrip("/")
        self.public_url = cfg["GUAC_PUBLIC_URL"].rstrip("/")
        self.datasource = cfg["GUAC_DATASOURCE"]
        self.admin_user = cfg["GUAC_ADMIN_USER"]
        self.admin_password = cfg["GUAC_ADMIN_PASSWORD"]
        if not self.admin_password:
            raise GuacamoleError("Guacamole admin password is not configured.")
        self.session = requests.Session()
        self.token = None

    def authenticate(self, username=None, password=None):
        username = username or self.admin_user
        password = password or self.admin_password
        response = self.session.post(
            f"{self.base_url}/api/tokens",
            data={"username": username, "password": password},
            timeout=30,
        )
        if response.status_code >= 400:
            raise GuacamoleError(f"Guacamole authentication failed: {response.text}")
        self.token = response.json()["authToken"]
        return self.token

    def _params(self):
        if not self.token:
            self.authenticate()
        return {"token": self.token}

    def _request(self, method, path, **kwargs):
        response = self.session.request(
            method,
            f"{self.base_url}/api/session/data/{self.datasource}{path}",
            params=self._params(),
            timeout=30,
            **kwargs,
        )
        if response.status_code >= 400:
            raise GuacamoleError(f"Guacamole API request failed: {response.text}")
        if response.text:
            return response.json()
        return None

    def ensure_user(self, username, password):
        payload = {
            "username": username,
            "password": password,
            "attributes": {
                "disabled": "",
                "expired": "",
                "access-window-start": "",
                "access-window-end": "",
                "valid-from": "",
                "valid-until": "",
                "timezone": "",
                "guac-full-name": username,
                "guac-organization": "VM Classroom",
                "guac-organizational-role": "Student",
            },
        }
        response = self.session.get(
            f"{self.base_url}/api/session/data/{self.datasource}/users/{quote(username)}",
            params=self._params(),
            timeout=30,
        )
        if response.status_code == 404:
            self._request("POST", "/users", json=payload)
        elif response.status_code >= 400:
            raise GuacamoleError(f"Guacamole user lookup failed: {response.text}")
        else:
            self._request("PUT", f"/users/{quote(username)}", json=payload)

    def create_or_update_connection(self, name, hostname, username, password, connection_id=None):
        cfg = current_app.config
        parameters = {
            "hostname": hostname,
            "port": "3389",
            "username": username or cfg["GUAC_RDP_USERNAME"],
            "password": password or cfg["GUAC_RDP_PASSWORD"],
            "domain": cfg["GUAC_RDP_DOMAIN"],
            "security": cfg["GUAC_SECURITY_MODE"],
            "ignore-cert": "true" if cfg["GUAC_IGNORE_CERT"] else "false",
            "enable-drive": "true" if cfg["GUAC_ENABLE_DRIVE"] else "false",
            "create-drive-path": "true" if cfg["GUAC_ENABLE_DRIVE"] else "false",
        }
        payload = {
            "parentIdentifier": cfg["GUAC_CONNECTION_GROUP"],
            "name": name,
            "protocol": "rdp",
            "parameters": parameters,
            "attributes": {"max-connections": "1", "max-connections-per-user": "1"},
        }
        if connection_id:
            self._request("PUT", f"/connections/{quote(str(connection_id))}", json=payload)
            return str(connection_id)
        created = self._request("POST", "/connections", json=payload)
        return str(created["identifier"])

    def assign_connection_to_user(self, username, connection_id):
        patch = [
            {
                "op": "add",
                "path": f"/connectionPermissions/{connection_id}",
                "value": "READ",
            }
        ]
        self._request("PATCH", f"/users/{quote(username)}/permissions", json=patch)

    def connection_url_for_user(self, connection_id, username, password):
        token = self.authenticate(username, password)
        raw = f"{connection_id}\0c\0{self.datasource}".encode("utf-8")
        client_id = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        return f"{self.public_url}/#/client/{client_id}?token={quote(token)}"


def guac_username_for(user):
    return f"student-{user.id}"


def guac_password_for(user):
    return f"vmclass-{user.id}-{user.password_hash[:16]}"
