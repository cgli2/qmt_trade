"""通知中枢（设计 L6 / 6.12）：飞书、企微、钉钉、控制台、文件。

这一层有三条硬性纪律，都是从"告警系统反过来把主系统搞挂"的教训里来的：

1. **永不抛异常**。通知失败只记日志。一个 webhook 超时绝不能让下单流程崩掉——
   这是 P4「失败安全」的一部分：通知是旁路，不是主干。
2. **永不刷屏**。同 key 告警节流 + 单日总量上限。告警风暴等于没有告警：
   200 条消息里没人找得到那条真正要命的。
3. **分级路由**。`CRITICAL` 必达（无视节流），`INFO` 只落日志不打扰。

另外刻意做了 `MemoryChannel`：测试与回测里不发真消息，但要能断言"该报的报了"。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Iterable

from ..core.config import Secrets
from ..core.logging import get_logger, get_trace_id

logger = get_logger("ops.notify")


class Level(IntEnum):
    """告警级别。数值可比较，方便做 ``min_level`` 过滤。"""

    DEBUG = 10
    INFO = 20
    WARN = 30
    ERROR = 40
    CRITICAL = 50

    @classmethod
    def parse(cls, v: "Level | str | int") -> "Level":
        if isinstance(v, Level):
            return v
        if isinstance(v, int):
            try:
                return cls(v)
            except ValueError:                    # 落在档位之间 → 就近取
                return min(cls, key=lambda m: abs(int(m) - v))
        name = str(v).strip().upper()
        alias = {"WARNING": "WARN", "FATAL": "CRITICAL", "ERR": "ERROR"}
        return cls[alias.get(name, name)]


_EMOJI = {Level.DEBUG: "·", Level.INFO: "ℹ", Level.WARN: "⚠",
          Level.ERROR: "✖", Level.CRITICAL: "🔥"}


@dataclass
class Message:
    title: str
    body: str = ""
    level: Level = Level.INFO
    #: 节流键。同一 key 在 throttle_seconds 内只发一条；为空则用 title
    key: str = ""
    fields: dict[str, Any] = field(default_factory=dict)
    at: datetime = field(default_factory=datetime.now)
    trace_id: str = ""

    def __post_init__(self) -> None:
        self.level = Level.parse(self.level)
        if not self.key:
            self.key = self.title
        if not self.trace_id:
            self.trace_id = get_trace_id()

    def render(self, *, with_time: bool = True) -> str:
        head = f"{_EMOJI.get(self.level, '')} [{self.level.name}] {self.title}"
        lines = [head]
        if with_time:
            lines.append(f"时间: {self.at:%Y-%m-%d %H:%M:%S}")
        if self.body:
            lines.append(self.body)
        for k, v in self.fields.items():
            lines.append(f"- {k}: {_fmt(v)}")
        return "\n".join(lines)


def _fmt(v: Any) -> str:
    if isinstance(v, float):
        return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 1e6 else f"{v:,.2f}"
    return str(v)


# ==================================================================== 通道
class Channel:
    """通道基类。``send`` 抛异常由 Notifier 兜住并降级，不影响其他通道。"""

    name = "base"
    #: 通道级最低级别（settings 里每个频道可单独设 min_level），默认不过滤
    min_level = Level.DEBUG

    def send(self, msg: Message) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class ConsoleChannel(Channel):
    name = "console"

    def send(self, msg: Message) -> None:
        lvl = {Level.DEBUG: logger.debug, Level.INFO: logger.info,
               Level.WARN: logger.warning, Level.ERROR: logger.error,
               Level.CRITICAL: logger.critical}.get(msg.level, logger.info)
        lvl("%s", msg.render(with_time=False).replace("\n", " | "))


class MemoryChannel(Channel):
    """只进内存，供测试/回测断言。"""

    name = "memory"

    def __init__(self) -> None:
        self.sent: list[Message] = []

    def send(self, msg: Message) -> None:
        self.sent.append(msg)

    def titles(self) -> list[str]:
        return [m.title for m in self.sent]

    def clear(self) -> None:
        self.sent.clear()


class FileChannel(Channel):
    """按天追加到文件，断网时的最后归档。"""

    name = "file"

    def __init__(self, dir_path: str | Path = "logs/alerts"):
        self.dir = Path(dir_path)

    def send(self, msg: Message) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        f = self.dir / f"{msg.at:%Y-%m-%d}.log"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": msg.at.isoformat(), "level": msg.level.name,
                "title": msg.title, "body": msg.body,
                "fields": {k: str(v) for k, v in msg.fields.items()},
                "trace_id": msg.trace_id}, ensure_ascii=False) + "\n")


class _WebhookChannel(Channel):
    """HTTP webhook 基类。超时短、不重试——通知迟到没关系，卡住主流程才要命。"""

    name = "webhook"

    def __init__(self, url: str, *, timeout: float = 5.0,
                 sender: Callable[[str, dict, float], None] | None = None):
        self.url = url
        self.timeout = float(timeout)
        #: 注入点：测试时替换成假的发送器，避免真发 HTTP
        self._sender = sender or _http_post_json

    def payload(self, msg: Message) -> dict:  # pragma: no cover - 抽象
        raise NotImplementedError

    def send(self, msg: Message) -> None:
        if not self.url:
            raise ValueError(f"{self.name} webhook 地址未配置")
        self._sender(self.url, self.payload(msg), self.timeout)


class WecomChannel(_WebhookChannel):
    """企业微信群机器人。"""

    name = "wecom"

    def payload(self, msg: Message) -> dict:
        return {"msgtype": "text", "text": {"content": msg.render()}}


class DingTalkChannel(_WebhookChannel):
    """钉钉群机器人。"""

    name = "dingtalk"

    def payload(self, msg: Message) -> dict:
        return {"msgtype": "text", "text": {"content": msg.render()}}


class FeishuChannel(_WebhookChannel):
    """飞书群机器人（v2 webhook）。

    与企微/钉钉有两点不同，都要命的：
    1. 报文格式是 ``{"msg_type": "text", "content": {"text": ...}}``；
    2. 业务错误（签名校验失败、机器人被停用等）返回 **HTTP 200 + code 非 0**，
       只看状态码就会出现"显示发送成功，群里啥也没有"的假成功。
       所以这里解析响应体，code 非 0 一律抛异常。
    """

    name = "feishu"

    def __init__(self, url: str, *, timeout: float = 5.0, secret: str = "",
                 sender: Callable[[str, dict, float], dict] | None = None):
        super().__init__(url, timeout=timeout, sender=sender or _http_post_json)
        #: 「签名校验」安全设置的密钥；为空则不签名
        self.secret = secret or ""

    def payload(self, msg: Message) -> dict:
        data: dict[str, Any] = {"msg_type": "text",
                                "content": {"text": msg.render()}}
        if self.secret:
            ts = str(int(time.time()))
            data["timestamp"] = ts
            data["sign"] = _feishu_sign(ts, self.secret)
        return data

    def send(self, msg: Message) -> None:
        if not self.url:
            raise ValueError(f"{self.name} webhook 地址未配置")
        resp = self._sender(self.url, self.payload(msg), self.timeout) or {}
        # HTTP 2xx 只代表网络通了，飞书的真实结果在响应体 code 里
        code = resp.get("code", resp.get("StatusCode", 0))
        if code:
            raise RuntimeError(f"飞书返回业务错误 code={code} msg={resp.get('msg') or resp.get('StatusMessage') or '-'}")


def _feishu_sign(timestamp: str, secret: str) -> str:
    """飞书官方签名算法：HMAC-SHA256(key=timestamp\\nsecret, msg=b'')。"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _http_post_json(url: str, payload: dict, timeout: float) -> dict:
    """POST JSON，返回解析后的响应体（解析失败给空 dict）。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        code = getattr(resp, "status", 200)
        if code >= 300:
            raise urllib.error.HTTPError(url, code, "webhook 返回非 2xx", {}, None)
        try:
            return json.loads(resp.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}


# ==================================================================== 中枢
class Notifier:
    """多通道通知中枢。线程安全（盘中监控与调度线程会并发调用）。"""

    def __init__(self, settings=None, *, channels: Iterable[Channel] | None = None):
        cfg = (settings.section("ops").get("notify", {}) if settings is not None else {})
        cfg = cfg if isinstance(cfg, dict) else {}
        self.enabled = bool(cfg.get("enabled", True))
        self.min_level = Level.parse(cfg.get("min_level", "INFO"))
        self.throttle_seconds = float(cfg.get("throttle_seconds", 60))
        self.daily_max = int(cfg.get("daily_max", 200))
        self.timeout = float(cfg.get("timeout_seconds", 5))
        self._lock = threading.RLock()
        self._last_sent: dict[str, float] = {}
        self._day = date.today()
        self._count = 0
        self.suppressed = 0
        self.failures: list[tuple[str, str]] = []
        self.channels: list[Channel] = (
            list(channels) if channels is not None
            else self._build_channels(cfg.get("channels") or ["console"]))

    # ------------------------------------------------------------ 构造
    def _build_channels(self, items: Iterable[Any]) -> list[Channel]:
        """频道配置兼容两种写法：字符串 ``console`` 或 dict ``{type: feishu, ...}``。"""
        out: list[Channel] = []
        for item in items:
            if isinstance(item, dict):
                cc = {str(k).lower(): v for k, v in item.items()}
                n = str(cc.get("type", "")).strip().lower()
            else:
                cc, n = {}, str(item).strip().lower()
            if not n:
                continue
            if cc.get("enabled") is False:
                continue
            try:
                env_key = str(cc.get("webhook_env") or cc.get("env") or "")
                if n == "console":
                    ch: Channel = ConsoleChannel()
                elif n == "file":
                    ch = FileChannel()
                elif n == "memory":
                    ch = MemoryChannel()
                elif n == "feishu":
                    env_key = env_key or "FEISHU_WEBHOOK"
                    url = Secrets.get(env_key, "") or ""
                    if not url:
                        logger.warning("未配置 %s，跳过飞书通道", env_key)
                        continue
                    ch = FeishuChannel(url, timeout=self.timeout,
                                       secret=Secrets.get("FEISHU_SECRET", "") or "")
                elif n == "wecom":
                    env_key = env_key or "WECOM_WEBHOOK"
                    url = Secrets.get(env_key, "") or ""
                    if not url:
                        logger.warning("未配置 %s，跳过企微通道", env_key)
                        continue
                    ch = WecomChannel(url, timeout=self.timeout)
                elif n == "dingtalk":
                    env_key = env_key or "DINGTALK_WEBHOOK"
                    url = Secrets.get(env_key, "") or ""
                    if not url:
                        logger.warning("未配置 %s，跳过钉钉通道", env_key)
                        continue
                    ch = DingTalkChannel(url, timeout=self.timeout)
                else:
                    logger.warning("未知通知通道 %s，忽略", n)
                    continue
                if cc.get("min_level"):
                    ch.min_level = Level.parse(cc["min_level"])
                out.append(ch)
            except Exception as exc:                      # 构造失败不致命
                logger.warning("通道 %s 初始化失败: %s", n, exc)
        if not out:
            out.append(ConsoleChannel())                  # 永远至少有个出口
        return out

    def add_channel(self, ch: Channel) -> None:
        with self._lock:
            self.channels.append(ch)

    # ------------------------------------------------------------ 发送
    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day, self._count = today, 0
            self._last_sent.clear()

    def _allow(self, msg: Message) -> tuple[bool, str]:
        """节流判定。CRITICAL 一律放行——刹车信号不能被限流吃掉。"""
        if not self.enabled:
            return False, "通知已关闭"
        if msg.level < self.min_level:
            return False, "低于 min_level"
        if msg.level >= Level.CRITICAL:
            return True, ""
        self._roll_day()
        if self.daily_max > 0 and self._count >= self.daily_max:
            return False, "超出单日上限"
        last = self._last_sent.get(msg.key)
        if last is not None and (time.time() - last) < self.throttle_seconds:
            return False, "节流中"
        return True, ""

    def send(self, msg: Message) -> bool:
        """返回是否实际投递。**任何异常都在这里被吃掉**。"""
        with self._lock:
            ok, why = self._allow(msg)
            if not ok:
                self.suppressed += 1
                logger.debug("通知抑制(%s): %s", why, msg.title)
                return False
            self._last_sent[msg.key] = time.time()
            self._count += 1
            chans = list(self.channels)

        delivered = False
        attempted = False
        for ch in chans:
            if msg.level < ch.min_level:               # 通道级过滤
                continue
            attempted = True
            try:
                ch.send(msg)
                delivered = True
            except Exception as exc:
                self.failures.append((ch.name, str(exc)))
                logger.warning("通道 %s 发送失败: %s", ch.name, exc)
        if attempted and not delivered:
            # 所有通道都挂了 —— 至少保证日志里有痕迹
            logger.error("全部通知通道失败，消息内容: %s", msg.render(with_time=False))
        return delivered

    def send_test(self, title: str, body: str = "", *, channel: str = "") -> dict:
        """手动测试入口：绕过节流/级别限制，返回逐通道真实投递结果。

        普通 ``send`` 为了不打扰主流程会吞掉细节，测试必须如实暴露
        "哪个通道挂了、为什么挂"，否则就是假成功。
        """
        if not self.enabled:
            return {"ok": False, "error": "通知已关闭（ops.notify.enabled=false）"}
        chans = [c for c in self.channels if not channel or c.name == channel]
        if channel and not chans:
            names = sorted({c.name for c in self.channels})
            return {"ok": False,
                    "error": f"频道 {channel} 不存在或未启用，当前可用: {names or '无'}"}
        msg = Message(title=title, body=body, level=Level.INFO,
                      key=f"test:{time.time()}")
        results: list[dict] = []
        ok_all = False
        for ch in chans:
            try:
                ch.send(msg)
                ok_all = True
                results.append({"channel": ch.name, "ok": True})
            except Exception as exc:
                self.failures.append((ch.name, str(exc)))
                logger.warning("测试发送失败 [%s]: %s", ch.name, exc)
                results.append({"channel": ch.name, "ok": False, "error": str(exc)})
        error = None if ok_all else "; ".join(
            f"{r['channel']}: {r.get('error', '-')}" for r in results if not r["ok"])
        return {"ok": ok_all, "channels": results, "error": error}

    # ------------------------------------------------------------ 语法糖
    def notify(self, title: str, body: str = "", *, level: Level | str = Level.INFO,
               key: str = "", **fields) -> bool:
        return self.send(Message(title=title, body=body, level=Level.parse(level),
                                 key=key, fields=fields))

    def info(self, title: str, body: str = "", **kw) -> bool:
        return self.notify(title, body, level=Level.INFO, **kw)

    def warn(self, title: str, body: str = "", **kw) -> bool:
        return self.notify(title, body, level=Level.WARN, **kw)

    def error(self, title: str, body: str = "", **kw) -> bool:
        return self.notify(title, body, level=Level.ERROR, **kw)

    def critical(self, title: str, body: str = "", **kw) -> bool:
        return self.notify(title, body, level=Level.CRITICAL, **kw)

    # ------------------------------------------------------------ 集成
    def bind_killswitch(self, ks) -> None:
        """KillSwitch 状态变更自动播报。切档是必达级别。"""

        def _cb(k) -> None:
            lvl = Level.CRITICAL if k.mode.value != "NORMAL" else Level.WARN
            self.send(Message(
                title=f"KillSwitch → {k.mode.value}",
                body=k.reason or "-", level=lvl,
                key=f"killswitch:{k.mode.value}",
                fields={"手动": k.manual}))

        ks.on_change(_cb)

    def stats(self) -> dict[str, Any]:
        return {"sent": self._count, "suppressed": self.suppressed,
                "failures": len(self.failures),
                "channels": [c.name for c in self.channels]}


def build_notifier(settings=None, **kw) -> Notifier:
    return Notifier(settings, **kw)
