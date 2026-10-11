"""Group channels: a name and a fixed set of members, both set at creation.

There is no server. Every copy of a group message describes the group, and
each member keeps what the first copy it saw said; nothing later can rename
the group or change who is in it.
"""
import os

MAX_GROUP_NAME = 40
MAX_GROUP_MEMBERS = 32      # counting ourselves


class Group:
    def __init__(self, gid: str, name: str, members: list):
        self.gid = gid
        self.name = name
        self.members = members      # (ip, port) of everyone but us


class Groups:
    def __init__(self):
        self._groups: dict[str, Group] = {}

    def get(self, gid: str) -> Group | None:
        return self._groups.get(gid)

    def create(self, name: str, members) -> Group:
        """Raises ValueError with a message fit to show the user."""
        name = name.strip()
        members = list(dict.fromkeys(members))
        if not name:
            raise ValueError("Give the group a name")
        if len(name) > MAX_GROUP_NAME:
            raise ValueError(f"The name is over {MAX_GROUP_NAME} characters")
        if not members:
            raise ValueError("Pick at least one member")
        if len(members) >= MAX_GROUP_MEMBERS:
            raise ValueError(f"A group holds at most {MAX_GROUP_MEMBERS} people")
        group = Group(os.urandom(8).hex(), name, members)
        self._groups[group.gid] = group
        return group

    def receive(self, gid: str, name: str, members) -> Group | None:
        """The group a message belongs to; `members` as the sender describes
        them, sender first. None if the sender is not in a group we know."""
        group = self._groups.get(gid)
        if group is None:
            group = self._groups[gid] = Group(gid, name, list(members))
        elif members[0] not in group.members:
            return None
        return group
