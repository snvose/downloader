from __future__ import annotations

from dataclasses import dataclass

from telegram import Update

from .config import Config
from .i18n import t
from .storage import read_json, write_json_atomic


@dataclass(frozen=True)
class PermissionResult:
    allowed: bool
    reason: str = ""


class Permissions:
    def __init__(self, config: Config):
        self.config = config
        self.banned_file = config.data_dir / "banned_users.json"
        self.state_file = config.data_dir / "bot_state.json"
        self.temp_bans_file = config.data_dir / "temp_bans.json"

    def is_admin(self, user_id: int | None) -> bool:
        return bool(user_id and int(user_id) == int(self.config.admin_id))

    def get_bot_enabled(self) -> bool:
        data = read_json(self.state_file, {"enabled": True})
        if not isinstance(data, dict):
            return True
        return bool(data.get("enabled", True))

    def set_bot_enabled(self, enabled: bool) -> None:
        data = read_json(self.state_file, {})
        if not isinstance(data, dict):
            data = {}
        data["enabled"] = bool(enabled)
        write_json_atomic(self.state_file, data)

    def _load_bans(self) -> dict:
        data = read_json(self.banned_file, {"users": [], "groups": []})
        if not isinstance(data, dict):
            data = {"users": [], "groups": []}

        users = data.get("users", [])
        groups = data.get("groups", [])

        if not isinstance(users, list):
            users = []
        if not isinstance(groups, list):
            groups = []

        raw_users = {int(x) for x in users if str(x).lstrip("-").isdigit()}
        raw_groups = {int(x) for x in groups if str(x).lstrip("-").isdigit()}

        # Telegram group/channel ids are negative and user ids positive, so an
        # id in the wrong list can be moved back by its sign. Corrections are
        # persisted so the file heals itself.
        clean_users = {x for x in raw_users if x > 0} | {x for x in raw_groups if x > 0}
        clean_groups = {x for x in raw_groups if x < 0} | {x for x in raw_users if x < 0}

        result = {"users": sorted(clean_users), "groups": sorted(clean_groups)}

        if clean_users != raw_users or clean_groups != raw_groups:
            write_json_atomic(self.banned_file, result)

        return result

    @staticmethod
    def is_group_id(target_id: int) -> bool:
        return int(target_id) < 0

    def _save_bans(self, data: dict) -> None:
        clean = {
            "users": sorted({int(x) for x in data.get("users", [])}),
            "groups": sorted({int(x) for x in data.get("groups", [])}),
        }
        write_json_atomic(self.banned_file, clean)

    def is_user_banned(self, user_id: int | None) -> bool:
        if not user_id:
            return False
        return int(user_id) in set(self._load_bans()["users"])

    def is_group_banned(self, chat_id: int | None) -> bool:
        if not chat_id:
            return False
        return int(chat_id) in set(self._load_bans()["groups"])

    def ban_user(self, user_id: int) -> None:
        data = self._load_bans()
        users = set(data["users"])
        users.add(int(user_id))
        data["users"] = sorted(users)
        self._save_bans(data)

    def unban_user(self, user_id: int) -> None:
        data = self._load_bans()
        users = set(data["users"])
        users.discard(int(user_id))
        data["users"] = sorted(users)
        self._save_bans(data)

    def ban_group(self, chat_id: int) -> None:
        data = self._load_bans()
        groups = set(data["groups"])
        groups.add(int(chat_id))
        data["groups"] = sorted(groups)
        self._save_bans(data)

    def unban_group(self, chat_id: int) -> None:
        data = self._load_bans()
        groups = set(data["groups"])
        groups.discard(int(chat_id))
        data["groups"] = sorted(groups)
        self._save_bans(data)

    # ── Sign-aware entry points used by /banid and the admin panel ────────────

    def ban_id(self, target_id: int) -> bool:
        """Bans the id. Returns True when a group was banned."""
        if self.is_group_id(target_id):
            self.ban_group(target_id)
            return True
        self.ban_user(target_id)
        return False

    def unban_id(self, target_id: int) -> bool:
        if self.is_group_id(target_id):
            self.unban_group(target_id)
            return True
        self.unban_user(target_id)
        return False

    def is_id_banned(self, target_id: int) -> bool:
        if self.is_group_id(target_id):
            return self.is_group_banned(target_id)
        return self.is_user_banned(target_id)

    def check_update(self, update: Update, *, allow_admin_when_disabled: bool = True) -> PermissionResult:
        user = update.effective_user
        chat = update.effective_chat

        user_id = user.id if user else None
        chat_id = chat.id if chat else None

        if self.is_admin(user_id):
            return PermissionResult(True)

        if self.is_user_banned(user_id):
            return PermissionResult(False, t("banned_user"))

        if self.is_group_banned(chat_id):
            return PermissionResult(False, t("banned_group"))

        if not self.get_bot_enabled():
            if allow_admin_when_disabled and self.is_admin(user_id):
                return PermissionResult(True)
            return PermissionResult(False, t("maintenance"))

        return PermissionResult(True)

    def counts(self) -> dict:
        bans = self._load_bans()
        return {
            "banned_users": len(bans["users"]),
            "banned_groups": len(bans["groups"]),
            "enabled": self.get_bot_enabled(),
        }

    def list_bans(self) -> dict:
        return self._load_bans()
