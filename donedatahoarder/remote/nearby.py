"""Connection picker orchestration, independent of Textual and the database."""
from __future__ import annotations

from dataclasses import replace
import threading

from .client import RemoteError
from .profiles import ProfileStore, pair_device


class NearbyManager:
    def __init__(self, *, store=None, browser=None):
        self.store = store if store is not None else ProfileStore()
        self.browser = browser
        self.error = ""
        self._started = False
        self._closed = False
        self._lifecycle = threading.RLock()

    def start(self):
        with self._lifecycle:
            if self._started or self._closed:
                return
            self._started = True
            try:
                if self.browser is None:
                    from .discovery import DiscoveryBrowser
                    self.browser = DiscoveryBrowser()
                self.browser.start()
            except Exception:
                self.error = "Nearby discovery unavailable. Install DDH's nearby extra or enter a workstation HTTPS address."

    def candidates(self):
        return self.browser.snapshot() if self.browser is not None else []

    def choices(self):
        profiles = {profile.server_id: profile for profile in self.store.list()}
        candidates = {candidate.server_id: candidate for candidate in self.candidates()}
        result = []
        for server_id in sorted(profiles.keys() | candidates.keys()):
            profile, candidate = profiles.get(server_id), candidates.get(server_id)
            result.append({"server_id": server_id, "name": profile.name if profile else candidate.name,
                           "saved": profile is not None, "nearby": candidate is not None,
                           "auto_reconnect": profile.auto_reconnect if profile else False,
                           "url": candidate.endpoint if candidate else profile.last_url})
        return result

    def connect(self, *, server_id=None, url="", invitation="", device_name="Omarchy laptop", auto_reconnect=False):
        candidates = {candidate.server_id: candidate for candidate in self.candidates()}
        candidate = candidates.get(server_id)
        if invitation:
            from .pairing import parse_invitation
            identity = parse_invitation(invitation)
            if server_id and identity["server_id"] != server_id:
                raise RemoteError("This invitation belongs to a different workstation. Select its nearby entry.")
            candidate = candidates.get(identity["server_id"])
            endpoint = url or (candidate.endpoint if candidate else "")
            if not endpoint:
                raise RemoteError("Choose a nearby workstation or enter its HTTPS address.")
            if candidate is not None and not url:
                from .profiles import select_pairing_endpoint
                endpoint = select_pairing_endpoint(candidate.endpoints, invitation)
            profile = pair_device(endpoint, invitation, device_name, store=self.store,
                                  name=candidate.name if candidate else "Workstation",
                                  auto_reconnect=auto_reconnect)
        else:
            if not server_id:
                raise RemoteError("Choose a saved workstation or paste its first-use invitation.")
            profile = self.store.load(server_id)
            if profile is None:
                raise RemoteError("Paste the invitation generated with remote-serve --pair on this workstation.")
            endpoint = url or (candidate.endpoint if candidate else profile.last_url)
            if candidate is not None and not url:
                from .profiles import choose_verified_endpoint
                endpoint = choose_verified_endpoint(candidate.endpoints, profile.certificate_pem, profile.hostname)
            profile = replace(profile, auto_reconnect=auto_reconnect)

        def resolve():
            current = next((item for item in self.candidates() if item.server_id == profile.server_id), None)
            return current.endpoints if current else None

        connection = self.store.connection(profile, url=endpoint,
                                           resolver=resolve if auto_reconnect else None)
        try:
            connection.connect()
            # Persist preference only after the endpoint has proved its identity.
            self.store.save(replace(profile, last_url=connection.url, name=connection.name))
            return connection
        except BaseException:
            connection.close()
            raise

    def close(self):
        with self._lifecycle:
            self._closed = True
            if self.browser is not None:
                self.browser.close()
