# guest_tokens.py
# Vollständige Endversion – Datei- und MongoDB-Backend integriert mit Aktivieren/Deaktivieren und Löschen
from __future__ import annotations
import os
import json
import time
import secrets
from typing import Dict, Any, List, Tuple, Optional

# ============================================================
# ==============  LOKALES DATEI-BACKEND  =====================
# ============================================================

class GuestDB:
    """
    Einfache Token-Datenbank mit Tageskontingent, lokal als JSON gespeichert.
    """

    def __init__(self, path: str = "guest_tokens.json"):
        self.path = path
        self.data: Dict[str, Any] = {"tokens": {}}
        self._load()

    # ---------------------- Persistence ----------------------

    def _load(self):
        """Lädt bestehende JSON-Datei, falls vorhanden."""
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
                if "tokens" not in self.data:
                    self.data["tokens"] = {}
        except Exception:
            self.data = {"tokens": {}}

    def _save(self):
        """Speichert Änderungen atomar."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ---------------------- Utilities -----------------------

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    @staticmethod
    def _now_ts() -> int:
        return int(time.time())

    # ---------------------- Public API ----------------------

    def create(self, name: str, quota_per_day: int = 5, unlimited: bool = False) -> str:
        token = secrets.token_urlsafe(24)
        is_unl = bool(unlimited or int(quota_per_day) <= 0)
        self.data["tokens"][token] = {
            "name": name.strip() or "Guest",
            "created": self._now_ts(),
            "active": True,
            "admin_disabled": False,
            "revoked": False,
            "deleted": False,
            "quota_per_day": 0 if is_unl else max(1, int(quota_per_day)),
            "unlimited": is_unl,
            "used": {}
        }
        self._save()
        return token

    def set_enabled(self, token: str, enabled: bool) -> bool:
        tok = self.data["tokens"].get(token)
        if not tok or tok.get("deleted"):
            return False
        if enabled:
            if tok.get("revoked"):
                return False
            tok["admin_disabled"] = False
            tok["active"] = True
        else:
            tok["admin_disabled"] = True
            tok["active"] = False
        self._save()
        return True

    def set_quota(self, token: str, quota_per_day: int, unlimited: Optional[bool] = None) -> bool:
        tok = self.data["tokens"].get(token)
        if not tok or tok.get("deleted"):
            return False
        if unlimited is True or (unlimited is None and int(quota_per_day) <= 0):
            tok["quota_per_day"] = 0
            tok["unlimited"] = True
        else:
            tok["quota_per_day"] = max(1, int(quota_per_day))
            tok["unlimited"] = False
        tok["updated_at"] = self._now_ts()
        self._save()
        return True

    def delete(self, token: str) -> bool:
        tok = self.data["tokens"].get(token)
        if not tok:
            return False
        tok["deleted"] = True
        tok["active"] = False
        tok["deleted_at"] = self._now_ts()
        self._save()
        masked = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"
        print(f"[guest] Guest token {masked} ({tok.get('name')}) permanently deleted.", flush=True)
        return True

    def rotate(self, token: str) -> Optional[str]:
        tok = self.data["tokens"].get(token)
        if not tok or tok.get("deleted"):
            return None
        new_token = self.create(
            tok.get("name", "Guest"),
            tok.get("quota_per_day", 5),
            unlimited=bool(tok.get("unlimited") or tok.get("quota_per_day", 5) <= 0)
        )
        self.delete(token)
        return new_token

    def revoke(self, token: str) -> bool:
        tok = self.data["tokens"].get(token)
        if not tok:
            return False
        tok["active"] = False
        tok["revoked"] = True
        self._save()
        return True

    def list(self) -> List[Tuple[str, Dict[str, Any]]]:
        out = []
        today = self._today()
        for token, tok in self.data["tokens"].items():
            if tok.get("deleted"):
                continue
            used = int(tok.get("used", {}).get(today, 0))
            quota = int(tok.get("quota_per_day", 5))
            is_unlimited = bool(tok.get("unlimited") or quota <= 0)

            if is_unlimited:
                rem = -1
                if tok.get("revoked"):
                    status_key = "revoked"
                    status_label = "Revoked"
                elif tok.get("admin_disabled") or not tok.get("active"):
                    status_key = "disabled"
                    status_label = "Disabled"
                else:
                    status_key = "unlimited"
                    status_label = "Unlimited"
            else:
                rem = max(0, quota - used)
                if tok.get("revoked"):
                    status_key = "revoked"
                    status_label = "Revoked"
                elif tok.get("admin_disabled") or not tok.get("active"):
                    status_key = "disabled"
                    status_label = "Disabled"
                elif rem == 0:
                    status_key = "quota_reached"
                    status_label = "Quota Full"
                else:
                    status_key = "active"
                    status_label = "Active"

            info = {
                "name": tok.get("name", "Guest"),
                "created": tok.get("created", 0),
                "active": bool(tok.get("active")),
                "admin_disabled": bool(tok.get("admin_disabled")),
                "quota_per_day": 0 if is_unlimited else quota,
                "unlimited": is_unlimited,
                "used_today": used,
                "remaining_today": rem,
                "status_key": status_key,
                "status_label": status_label,
            }
            out.append((token, info))
        return sorted(out, key=lambda kv: kv[1].get("created", 0), reverse=True)

    def remaining_today(self, token: str) -> int:
        tok = self.data["tokens"].get(token)
        if not tok or not tok.get("active") or tok.get("deleted") or tok.get("admin_disabled"):
            return 0
        if tok.get("unlimited") or int(tok.get("quota_per_day", 5)) <= 0:
            return -1
        today = self._today()
        used = int(tok.get("used", {}).get(today, 0))
        quota = int(tok.get("quota_per_day", 5))
        return max(0, quota - used)

    def validate(self, token: str) -> Optional[Dict[str, Any]]:
        tok = self.data["tokens"].get(token)
        if not tok or not tok.get("active") or tok.get("deleted") or tok.get("admin_disabled"):
            return None
        quota = int(tok.get("quota_per_day", 5))
        is_unlimited = bool(tok.get("unlimited") or quota <= 0)
        return {
            "name": tok.get("name", "Guest"),
            "created": tok.get("created", 0),
            "active": True,
            "quota_per_day": 0 if is_unlimited else quota,
            "unlimited": is_unlimited,
            "used": tok.get("used", {})
        }

    def consume(self, token: str) -> Optional[Dict[str, Any]]:
        tok = self.validate(token)
        if not tok:
            return None
        today = self._today()
        raw_tok = self.data["tokens"][token]
        used = int(raw_tok.setdefault("used", {}).get(today, 0))
        quota = int(raw_tok.get("quota_per_day", 5))
        is_unlimited = bool(raw_tok.get("unlimited") or quota <= 0)
        if not is_unlimited and used >= quota:
            return None
        raw_tok["used"][today] = used + 1
        self._save()
        return raw_tok


# ============================================================
# ==============  MONGODB-BACKEND (ATLAS)  ===================
# ============================================================

try:
    from pymongo import MongoClient, ReturnDocument
except ImportError:
    MongoClient = None
    ReturnDocument = None


class MongoGuestDB:
    """
    Token-Datenbank auf MongoDB-Basis mit Aktivieren/Deaktivieren und Löschen.
    """

    def __init__(self, uri: str, db_name: str = "printer", coll_name: str = "guest_tokens"):
        if MongoClient is None:
            raise RuntimeError("pymongo ist nicht installiert.")
        if not uri:
            raise RuntimeError("MONGO_URI fehlt in Environment Variables.")
        self.client = MongoClient(uri)
        self.coll = self.client[db_name][coll_name]
        self.coll.create_index("_id")

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d")

    @staticmethod
    def _now_ts() -> int:
        return int(time.time())

    def create(self, name: str, quota_per_day: int = 5, unlimited: bool = False) -> str:
        token = secrets.token_urlsafe(24)
        is_unl = bool(unlimited or int(quota_per_day) <= 0)
        doc = {
            "_id": token,
            "name": (name or "Guest").strip(),
            "created": self._now_ts(),
            "active": True,
            "admin_disabled": False,
            "revoked": False,
            "deleted": False,
            "quota_per_day": 0 if is_unl else max(1, int(quota_per_day)),
            "unlimited": is_unl,
            "used": {}
        }
        self.coll.insert_one(doc)
        return token

    def set_enabled(self, token: str, enabled: bool) -> bool:
        if enabled:
            # Re-enable only allowed if not revoked or deleted
            res = self.coll.update_one(
                {"_id": token, "deleted": {"$ne": True}, "revoked": {"$ne": True}},
                {"$set": {"active": True, "admin_disabled": False, "updated_at": self._now_ts()}}
            )
            return res.modified_count > 0 or res.matched_count > 0
        else:
            # Disable
            res = self.coll.update_one(
                {"_id": token, "deleted": {"$ne": True}},
                {"$set": {"active": False, "admin_disabled": True, "updated_at": self._now_ts()}}
            )
            return res.modified_count > 0 or res.matched_count > 0

    def set_quota(self, token: str, quota_per_day: int, unlimited: Optional[bool] = None) -> bool:
        if unlimited is True or (unlimited is None and int(quota_per_day) <= 0):
            res = self.coll.update_one(
                {"_id": token, "deleted": {"$ne": True}},
                {"$set": {"quota_per_day": 0, "unlimited": True, "updated_at": self._now_ts()}}
            )
        else:
            quota = max(1, int(quota_per_day))
            res = self.coll.update_one(
                {"_id": token, "deleted": {"$ne": True}},
                {"$set": {"quota_per_day": quota, "unlimited": False, "updated_at": self._now_ts()}}
            )
        return res.matched_count > 0 or res.modified_count > 0

    def delete(self, token: str) -> bool:
        doc = self.coll.find_one_and_update(
            {"_id": token},
            {"$set": {"deleted": True, "active": False, "deleted_at": self._now_ts()}},
            return_document=ReturnDocument.AFTER if ReturnDocument else None
        )
        if not doc:
            return False
        masked = f"{token[:4]}...{token[-4:]}" if len(token) > 8 else "***"
        print(f"[guest] Guest token {masked} ({doc.get('name')}) permanently deleted.", flush=True)
        return True

    def rotate(self, token: str) -> Optional[str]:
        doc = self.coll.find_one({"_id": token, "deleted": {"$ne": True}})
        if not doc:
            return None
        new_token = self.create(
            doc.get("name", "Guest"),
            doc.get("quota_per_day", 5),
            unlimited=bool(doc.get("unlimited") or doc.get("quota_per_day", 5) <= 0)
        )
        self.delete(token)
        return new_token

    def revoke(self, token: str) -> bool:
        res = self.coll.update_one({"_id": token}, {"$set": {"active": False, "revoked": True}})
        return res.matched_count > 0

    def list(self) -> List[Tuple[str, Dict[str, Any]]]:
        out = []
        today = self._today()
        for d in self.coll.find({"deleted": {"$ne": True}}).sort("created", -1):
            token = d["_id"]
            used = int(d.get("used", {}).get(today, 0))
            quota = int(d.get("quota_per_day", 5))
            is_unlimited = bool(d.get("unlimited") or quota <= 0)

            if is_unlimited:
                rem = -1
                if d.get("revoked"):
                    status_key = "revoked"
                    status_label = "Revoked"
                elif d.get("admin_disabled") or not d.get("active"):
                    status_key = "disabled"
                    status_label = "Disabled"
                else:
                    status_key = "unlimited"
                    status_label = "Unlimited"
            else:
                rem = max(0, quota - used)
                if d.get("revoked"):
                    status_key = "revoked"
                    status_label = "Revoked"
                elif d.get("admin_disabled") or not d.get("active"):
                    status_key = "disabled"
                    status_label = "Disabled"
                elif rem == 0:
                    status_key = "quota_reached"
                    status_label = "Quota Full"
                else:
                    status_key = "active"
                    status_label = "Active"

            info = {
                "name": d.get("name", "Guest"),
                "created": d.get("created", 0),
                "active": bool(d.get("active")),
                "admin_disabled": bool(d.get("admin_disabled")),
                "quota_per_day": 0 if is_unlimited else quota,
                "unlimited": is_unlimited,
                "used_today": used,
                "remaining_today": rem,
                "status_key": status_key,
                "status_label": status_label,
            }
            out.append((token, info))
        return out

    def remaining_today(self, token: str) -> int:
        d = self.coll.find_one(
            {"_id": token, "active": True, "deleted": {"$ne": True}, "admin_disabled": {"$ne": True}},
            projection={"quota_per_day": 1, "used": 1, "unlimited": 1}
        )
        if not d:
            return 0
        if d.get("unlimited") or int(d.get("quota_per_day", 5)) <= 0:
            return -1
        today = self._today()
        used = int(d.get("used", {}).get(today, 0))
        quota = int(d.get("quota_per_day", 5))
        return max(0, quota - used)

    def validate(self, token: str) -> Optional[Dict[str, Any]]:
        d = self.coll.find_one(
            {"_id": token, "active": True, "deleted": {"$ne": True}, "admin_disabled": {"$ne": True}}
        )
        if not d:
            return None
        quota = int(d.get("quota_per_day", 5))
        is_unlimited = bool(d.get("unlimited") or quota <= 0)
        return {
            "name": d.get("name", "Guest"),
            "created": d.get("created", 0),
            "active": True,
            "quota_per_day": 0 if is_unlimited else quota,
            "unlimited": is_unlimited,
            "used": d.get("used", {})
        }

    def consume(self, token: str) -> Optional[Dict[str, Any]]:
        today = self._today()
        d = self.validate(token)
        if not d:
            return None
        used = int(d.get("used", {}).get(today, 0))
        quota = int(d.get("quota_per_day", 5))
        is_unlimited = bool(d.get("unlimited") or quota <= 0)
        if not is_unlimited and used >= quota:
            return None
        d2 = self.coll.find_one_and_update(
            {"_id": token, "active": True, "deleted": {"$ne": True}, "admin_disabled": {"$ne": True}},
            {"$inc": {f"used.{today}": 1}},
            return_document=ReturnDocument.AFTER if ReturnDocument else None
        )
        if not d2:
            return None
        return {
            "name": d2.get("name", "Guest"),
            "created": d2.get("created", 0),
            "active": True,
            "quota_per_day": 0 if is_unlimited else int(d2.get("quota_per_day", 5)),
            "unlimited": is_unlimited,
            "used": d2.get("used", {})
        }


# ============================================================
# ==============  AUTOMATISCHE BACKEND-WAHL  =================
# ============================================================

def get_guest_db() -> GuestDB | MongoGuestDB:
    backend = os.getenv("GUEST_DB_BACKEND", "file").lower()
    if backend == "mongo":
        uri = os.getenv("MONGO_URI")
        db_name = os.getenv("MONGO_DB", "printer")
        coll_name = os.getenv("MONGO_COLL", "guest_tokens")
        return MongoGuestDB(uri, db_name, coll_name)
    else:
        path = os.getenv("GUEST_DB_FILE", "/data/guest_tokens.json")
        return GuestDB(path)