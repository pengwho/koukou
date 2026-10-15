"""鑰匙圈卡在 macOS 授權視窗時，伺服器不能跟著卡住。"""

import threading

from koukou import credentials as C


class SlowKeyring:
    """模擬還沒按「允許」的鑰匙圈：get/set 都卡到 release 為止。"""

    def __init__(self):
        self.release = threading.Event()
        self.data = {}

    def get_password(self, service, user):
        self.release.wait()
        return self.data.get((service, user))

    def set_password(self, service, user, value):
        self.release.wait()
        self.data[(service, user)] = value

    def delete_password(self, service, user):
        self.data.pop((service, user), None)


def test_waiting_keychain_does_not_block(monkeypatch):
    kr = SlowKeyring()
    monkeypatch.setattr(C, "_keyring", lambda: kr)
    monkeypatch.setattr(C, "KEYCHAIN_TIMEOUT", 0.2)
    monkeypatch.setattr(C, "_session", {})
    monkeypatch.setattr(C.config, "env_value", lambda name: None)

    assert C.load() is None  # 沒卡住，只是先當作沒有
    assert C.keychain_waiting()
    # 存帳密：這次執行照樣能用，只是鑰匙圈還沒存進去
    assert C.save("0912345678", "secret", remember=True) is False
    assert C.load() == ("0912345678", "secret")

    kr.release.set()  # 使用者按了「永遠允許」
    C._pending.join(1)
    assert not C.keychain_waiting()
    C._session.clear()
    assert C.save("0912345678", "secret", remember=True) is True
    C._session.clear()
    assert C.load() == ("0912345678", "secret")


def test_api_reports_keychain_waiting(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from koukou.web.app import create_app

    kr = SlowKeyring()
    monkeypatch.setattr(C, "_keyring", lambda: kr)
    monkeypatch.setattr(C, "KEYCHAIN_TIMEOUT", 0.2)
    monkeypatch.setattr(C, "_session", {})
    monkeypatch.setattr(C.config, "env_value", lambda name: None)
    web = TestClient(create_app(tmp_path / "k.sqlite3"), base_url="http://127.0.0.1")
    res = web.get("/api/credentials").json()
    assert res == {"configured": False, "phone": None, "keychain_waiting": True}
    kr.release.set()
    C._pending.join(1)
