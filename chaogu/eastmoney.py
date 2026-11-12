"""东方财富普通账户只读接入。凭证只在进程内存；禁止通用代理和交易端点。"""
from __future__ import annotations

import base64
import hashlib
import html
import http.cookiejar
import json
import math
import os
import platform
import re
import secrets
import ssl
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from . import config

CONFIRMATION_PATH = config.DATA_DIR / "eastmoney-asset-confirmation.json"

BASE = "https://jywg.18.cn"
IDLE_SECONDS = 24 * 60 * 60
SYNC_SECONDS = 60
# 已核对的官方 LoginSec 脚本：变更后必须重新审查控件、短信、认证流程。
# 2026-10-20 复核：网页登录地址改为 /Login/AuthenticationSec；控件仍仅 Windows 且 __secStage=="2"；
# 新增 ErrCode=-13 开户手机号验证（loginLimitCert），本接入尚未实现，遇到时停止。
LOGIN_SCRIPT_SHA256 = "47a94dce992f1c103e4a2c1a32cc2e92a8c80c05a69757b62c587a73d445fc54"


class BrokerError(Exception):
    """只使用本地文案及已校验的证券代码/行号；不回显原始响应或凭证。"""


class Unsupported(BrokerError):
    pass


class Expired(BrokerError):
    pass


class SnapshotMismatch(BrokerError):
    """仅保留通过字段验证的财务诊断，原始响应及认证信息不进入诊断。"""

    def __init__(self, message, diagnostics, candidate=None):
        super().__init__(message)
        self.diagnostics = diagnostics
        self.candidate = candidate


def confirmation_for(diagnostics):
    """绑定差额和持仓数量，不能将后续缺失持仓自动归为理财。"""
    return {"kind": "cash_management", "amount": diagnostics["difference"],
            "holdings": sorted([[p["code"], p["quantity"]] for p in diagnostics["positions"]])}


def save_confirmation(diagnostics):
    record = confirmation_for(diagnostics)
    if record["amount"] <= 0:
        raise BrokerError("只有正的未展开资产差额可核对为现金理财")
    CONFIRMATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, filename = tempfile.mkstemp(dir=CONFIRMATION_PATH.parent, prefix=".asset-confirmation-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, ensure_ascii=False)
        os.replace(filename, CONFIRMATION_PATH)
    finally:
        if os.path.exists(filename):
            os.unlink(filename)


def reconcile_confirmed(exc):
    try:
        record = json.loads(CONFIRMATION_PATH.read_text())
    except (OSError, ValueError):
        return None
    if exc.candidate and record == confirmation_for(exc.diagnostics) and record["amount"] > 0:
        return validate_snapshot({**exc.candidate, "extra_assets": [
            {"kind": "cash_management", "value": record["amount"], "source": "user_confirmed_difference"}]})
    return None


def _num(obj, key, negative=False, integer=False):
    v = obj.get(key)
    if isinstance(v, bool) or v is None or str(v).strip() == "":
        raise BrokerError(f"账户数据字段 {key} 缺失，已保留上次快照")
    try:
        n = float(v)
    except (ValueError, TypeError):
        raise BrokerError("账户数据格式异常，已保留上次快照") from None
    if not math.isfinite(n) or abs(n) > 1e12 or (not negative and n < 0) or (integer and not n.is_integer()):
        raise BrokerError("账户数值异常，已保留上次快照")
    return int(n) if integer else n


def is_a_share(code):
    return bool(re.fullmatch(r"(?:00|30|60|68|92|[48]\d)\d{4}", code))


def _security_code(value, index):
    # JSON 数字可能丢失前导零；字符串只接受六位代码和明确的交易所装饰。
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 999999:
        value = f"{value:06d}"
    match = re.fullmatch(r"(?:(SH|SZ|BJ)[.:-]?)?(\d{6})(?:[.:-](SH|SZ|BJ))?",
                         value.strip().upper() if isinstance(value, str) else "")
    if not match or (match[1] and match[3] and match[1] != match[3]):
        raise BrokerError(f"第 {index} 条持仓的证券代码格式无法识别，已保留上次快照")
    return match[2], match[1] or match[3]


def validate_snapshot(data):
    """验证完整快照并投影到财务字段；不接受部分或推断出的空持仓。"""
    if not isinstance(data, dict) or not isinstance(data.get("positions"), list):
        raise BrokerError("账户数据不完整，已保留上次快照")
    clean = {k: _num(data, k, negative=k == "pnl") for k in ("cash", "equity", "market_value", "pnl")}
    if clean["market_value"] > clean["equity"] + .1:
        raise BrokerError("券商市值超过普通账户总资产，资产口径需核对，已保留上次快照")
    extras = data.get("extra_assets", [])
    if not isinstance(extras, list) or len(extras) > 1:
        raise BrokerError("未展开资产格式异常，已保留上次快照")
    if extras:
        e = extras[0]
        if not isinstance(e, dict) or e.get("kind") != "cash_management" or e.get("source") != "user_confirmed_difference":
            raise BrokerError("未展开资产来源未核实，已保留上次快照")
        clean["extra_assets"] = [{"kind": "cash_management", "value": _num(e, "value"),
                                  "source": "user_confirmed_difference"}]
    groups, exchanges = {}, {}
    for index, p in enumerate(data["positions"], 1):
        if not isinstance(p, dict):
            raise BrokerError(f"第 {index} 条持仓数据不完整，已保留上次快照")
        code, exchange = _security_code(p.get("code"), index)
        name = p.get("name")
        if not isinstance(name, str) or not name.strip():
            raise BrokerError(f"证券 {code} 的名称缺失，已保留上次快照")
        if exchange and exchanges.get(code, exchange) != exchange:
            raise BrokerError(f"证券 {code} 的交易所标识冲突，已保留上次快照")
        if exchange:
            exchanges[code] = exchange
        row = {"code": code, "name": name.strip()[:30]}
        for k in ("quantity", "sellable", "cost", "price", "value", "pnl"):
            try:
                row[k] = _num(p, k, negative=k in ("cost", "pnl"),
                              integer=is_a_share(code) and k in ("quantity", "sellable"))
            except BrokerError as exc:
                raise BrokerError(f"证券 {code}：{exc}") from None
        if row["sellable"] > row["quantity"]:
            raise BrokerError(f"证券 {code} 的可卖数量超过持仓数量，已保留上次快照")
        groups.setdefault(code, []).append(row)
    # 每一行均计入市值核对，不猜测性删除重复行或忽略非 A 股资产。
    total = math.fsum(p["value"] for rows in groups.values() for p in rows)
    extra_value = sum(e["value"] for e in clean.get("extra_assets", []))
    if abs(total + extra_value - clean["market_value"]) > max(.1, len(groups) * .02):
        repeated = [code for code, rows in groups.items() if len(rows) > 1]
        detail = ("；存在同代码多条记录：" + "、".join(repeated[:5])) if repeated else "；可能存在分页或未返回的资产"
        raise SnapshotMismatch("持仓明细市值与券商资产汇总不一致" + detail + "，已保留上次快照",
            {"asset_market_value": clean["market_value"], "detail_market_value": total,
             "difference": round(clean["market_value"] - total, 2),
             "row_count": len(data["positions"]),
             "positions": [{k: p[k] for k in ("code", "quantity", "price", "value")}
                           for rows in groups.values() for p in rows]},
            {**clean, "positions": [p for rows in groups.values() for p in rows]})
    positions = []
    for code, rows in groups.items():
        row = dict(rows[0])
        if len(rows) > 1:
            if any(p["name"] != row["name"] or p["price"] != row["price"] for p in rows):
                raise BrokerError(f"证券 {code} 有多条记录且名称或现价不一致，无法安全合并，已保留上次快照")
            for key in ("quantity", "sellable", "value", "pnl"):
                row[key] = sum(p[key] for p in rows)
            if row["quantity"]:
                row["cost"] = math.fsum(p["cost"] * p["quantity"] for p in rows) / row["quantity"]
        positions.append(row)
    clean["positions"] = sorted(positions, key=lambda p: p["code"])
    return clean


class Redirected(Expired):
    def __init__(self, to_login, destination=None):
        super().__init__("券商会话已失效或登录流程已变化，请重新登录")
        self.to_login = to_login
        self.destination = destination


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 不允许把 Cookie、验证码或加密密码转发到其他地址。
        target = urllib.parse.urlsplit(urllib.parse.urljoin(BASE, newurl))
        # 只保留主机和受限字符路径；永不保存查询参数（可能含认证信息）。
        safe_path = target.path if re.fullmatch(r"/[A-Za-z0-9/_-]*",target.path) else "/[redacted]"
        raise Redirected(target.scheme == "https" and target.netloc == "jywg.18.cn" and target.path.lower() == "/login",
                         {"host":target.hostname,"path":safe_path})


class OfficialClient:
    """固定 HTTPS 主机与路径白名单，无下单、撤单、转账能力。"""
    GET = {"/Login", "/Login/YZM", "/Login/ExitLogin", "/Search/Position",
           "/finance/ttb/ttbsearch", "/finance/ttb/hisearned", "/Finance/TTB/TTBIndex", "/Finance/TTB/TTBSearch", "/Finance/TTB/HisEarned"}
    POST = {"/Login/AuthenticationSec", "/Login/GetMsgVerifyCode_12", "/Login/GetCheckMsgVerCode_12",
            "/Com/queryAssetAndPositionV1"}

    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect(),
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()))
        self.key = ""
        self.rand = ""
        self.validatekey = ""
        self.account = ""
        self.resources = set()
        self.query_ready = False

    def close(self):
        self.cookies.clear()
        self.key = self.rand = self.validatekey = self.account = ""
        self.resources.clear()
        self.query_ready = False

    def _request(self, path, data=None, query=None):
        method = "POST" if data is not None else "GET"
        allowed = self.POST if method == "POST" else self.GET | self.resources
        if path not in allowed:
            raise Unsupported("未识别的官方流程，已停止请求")
        url = BASE + path + ("?" + urllib.parse.urlencode(query) if query else "")
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        os_ua = "Macintosh; Intel Mac OS X 10_15_7" if platform.system() == "Darwin" else "X11; Linux x86_64"
        headers = {"User-Agent": f"Mozilla/5.0 ({os_ua}) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36",
                   "Referer": BASE + "/Login", "Accept": "*/*", "gw_reqtimestamp": str(int(time.time()*1000))}
        if data is not None:
            headers.update({"Content-Type": "application/x-www-form-urlencoded", "Origin": BASE,
                            "X-Requested-With": "XMLHttpRequest"})
        try:
            with self.opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=12) as r:
                raw = r.read(2_000_001)
                if len(raw) > 2_000_000:
                    raise BrokerError("券商返回数据过大，已停止读取")
                return raw, r.headers.get_content_type()
        except (Expired, BrokerError):
            raise
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise Expired("券商拒绝访问，请重新登录或在官方界面核查账户状态") from None
            raise BrokerError("券商服务暂不可用，请稍后手动重试") from None
        except Exception:
            raise BrokerError("无法安全连接东方财富，请检查网络及系统证书后重试") from None

    def _text(self, path):
        raw, _ = self._request(path)
        try:
            return raw.decode("utf-8-sig")
        except UnicodeError:
            raise Unsupported("官方页面编码发生变化，接入已暂停") from None

    def _json(self, path, data):
        raw, _ = self._request(path, data, {"validatekey": self.validatekey})
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeError):
            raise Expired("券商未返回有效账户响应，请重新登录") from None
        if not isinstance(result, dict) or "Status" not in result:
            raise BrokerError("券商响应结构不完整，已保留上次快照")
        if str(result["Status"]) in ("-2", "-99"):
            raise Expired("券商会话已失效，请重新登录")
        return result

    def _scripts(self, page):
        paths = []
        for src in re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)', page, re.I):
            u = urllib.parse.urlsplit(html.unescape(src))
            if not u.scheme and not u.netloc and re.fullmatch(r"/JsBundles/[A-Za-z0-9]+", u.path):
                self.resources.add(u.path)
                paths.append(u.path)
        return paths

    def initialize(self):
        page = self._text("/Login")
        paths = self._scripts(page)
        if "/JsBundles/LoginSec" not in paths or "/JsBundles/BaseJS" not in paths:
            raise Unsupported("官方登录页面已变化，接入已暂停")
        script = self._text("/JsBundles/LoginSec")
        if hashlib.sha256(script.encode()).hexdigest() != LOGIN_SCRIPT_SHA256:
            raise Unsupported("官方登录流程已更新，需重新验证后才能接入")
        stage = re.search(r'var\s+__secStage\s*=\s*["\']([012])["\']', page)
        if not stage:
            raise Unsupported("无法确认官方安全控件要求，已停止登录")
        # 经核对的脚本：u=EmUtil.SecHelper.isWin() && __secStage=="2"。
        if stage[1] == "2" and platform.system() == "Windows":
            raise Unsupported("官方要求 Windows 安全控件，当前接入尚不支持该控件，请使用官方客户端")
        base = self._text("/JsBundles/BaseJS")
        if 'n.encrypt=function(n){return t.encrypt(n)}}(EMTradeEncrypt)' not in base:
            raise Unsupported("官方密码加密流程已变化，接入已暂停")
        key = re.search(r't\.setPublicKey\("(-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----)"\)', base)
        if not key:
            raise Unsupported("无法验证官方密码加密公钥，已停止登录")
        self.key = key[1].replace(r"\n", "\n")
        if not re.fullmatch(r"-----BEGIN PUBLIC KEY-----\n[A-Za-z0-9+/=\n]+\n-----END PUBLIC KEY-----", self.key):
            raise Unsupported("官方公钥格式发生变化，已停止登录")
        return {"public_key": self.key}

    def captcha(self):
        self.rand = "0." + str(secrets.randbelow(10**16)).zfill(16)
        raw, mime = self._request("/Login/YZM", query={"randNum": self.rand})
        if mime not in ("image/jpeg", "image/png", "image/gif") or len(raw) > 200_000:
            raise BrokerError("验证码读取失败，请手动刷新")
        return "data:" + mime + ";base64," + base64.b64encode(raw).decode()

    def login(self, account, encrypted, captcha):
        if not re.fullmatch(r"\d{12}", account) or not re.fullmatch(r"[A-Za-z0-9+/]{171}=", encrypted):
            raise BrokerError("请核对资金账号和加密密码")
        if not self.rand or not re.fullmatch(r"[A-Za-z0-9]{4}", captcha):
            raise BrokerError("请输入当前图形验证码")
        r = self._json("/Login/AuthenticationSec", {"userId": account, "password": encrypted,
                       "randNumber": self.rand, "identifyCode": captcha, "duration": "1800",
                       "authCode": "", "type": "Z", "secInfo": ""})
        self.rand = ""
        if str(r["Status"]) == "0":
            self.account = ""
            return "authenticated"
        if str(r.get("ErrCode")) == "-11":
            self.account = account
            return "sms_required"
        if str(r.get("ErrCode")) == "-13":
            raise Unsupported("本次登录需验证开户手机号，该流程尚未接入，请先在官方网页或客户端完成验证后再试")
        if "控件" in str(r.get("Message", "")):
            raise Unsupported("官方要求此电脑尚不支持的安全控件，已停止登录，请使用官方客户端")
        raise BrokerError("登录未通过，请核对账号、密码和验证码；若账户受限，请在官方界面处理")

    def sms(self, encrypted_mobile, code, send=False):
        if not self.account or not re.fullmatch(r"[A-Za-z0-9+/]{171}=", encrypted_mobile):
            raise BrokerError("手机验证已失效，请重新开始登录")
        if not re.fullmatch(r"[A-Za-z0-9]{4}" if send else r"\d{4,8}", code):
            raise BrokerError("验证码格式不正确")
        data = {"MobileNo": encrypted_mobile, "User_id": self.account}
        if send:
            if not self.rand:
                raise BrokerError("请先刷新图形验证码")
            data.update(vCode=code, randNumber=self.rand)
            path = "/Login/GetMsgVerifyCode_12"
        else:
            data["Vercode"] = code
            path = "/Login/GetCheckMsgVerCode_12"
        r = self._json(path, data)
        if str(r["Status"]) != "0":
            raise BrokerError("手机验证未通过，请核对验证码或在官方界面处理")
        if not send:
            self.account = ""  # 官方短信解除限制后要求重新输入密码，不自动重登。

    def _query_contract(self):
        page = self._text("/Search/Position")
        if 'id="txtZjzh"' in page:
            raise Expired("券商会话已失效，请重新登录")
        token = re.search(r'<input\b(?=[^>]*\bid=["\']em_validatekey["\'])(?=[^>]*\bvalue=["\']([^"\']+))[^>]*>', page, re.I)
        if not token:
            raise Unsupported("已登录，但无法确认官方账户查询校验信息；自动同步尚未启用")
        # 登录页的资源布局不能证明 API 是否可用。校验值取自官方页面，
        # 下一步仅请求固定白名单里的只读端点，并严格验证实际 JSON 响应。
        self.validatekey = html.unescape(token[1])
        self.query_ready = True

    def query(self):
        if not self.query_ready:
            self._query_contract()
        result = self._json("/Com/queryAssetAndPositionV1", {"moneyType": "RMB"})
        if str(result["Status"]) != "0" or not isinstance(result.get("Data"), list):
            raise BrokerError("资金或持仓读取失败，已保留上次快照")
        if len(result["Data"]) != 1:
            raise Unsupported("暂只支持单个普通人民币 A 股资金账户")
        a = result["Data"][0]
        if not isinstance(a, dict) or not isinstance(a.get("positions"), list):
            raise BrokerError("资金或持仓响应不完整，已保留上次快照")
        if a.get("Bz", "RMB") not in ("RMB", "0", 0):
            raise Unsupported("暂只支持人民币账户")
        rows = a["positions"]
        for key in ("Total", "TotalCount", "PositionCount"):
            if key in a and _num(a, key, integer=True) != len(rows):
                raise BrokerError("持仓返回不完整，已保留上次快照")

        ps = []
        for p in rows:
            if not isinstance(p, dict):
                raise BrokerError("持仓响应不完整，已保留上次快照")
            ps.append({dest: p.get(src) for dest, src in {
                "code": "Zqdm", "name": "Zqmc", "quantity": "Zqsl", "sellable": "Kysl",
                "cost": "Cbjg", "price": "Zxjg", "value": "Zxsz", "pnl": "Ljyk"}.items()})
        data = {"cash": a.get("Kyzj"), "equity": a.get("Zzc"), "market_value": a.get("Zxsz"),
                "pnl": sum(_num(p, "pnl", negative=True) for p in ps), "positions": ps}
        return validate_snapshot(data)

    def product_capabilities(self):
        """只检查已从官方导航确认的查询页。候选路径仅返回供适配，不自动执行。"""
        result=[]
        for path in ("/Finance/TTB/TTBIndex", "/Finance/TTB/TTBSearch", "/Finance/TTB/HisEarned", "/Search/Position"):
            try:
                page=self._text(path)
            except Redirected as exc:
                result.append({"page":path,"status":"专项产品页发生重定向，未跟随；证券会话需独立核对",
                               "redirect":exc.destination,"query_candidates":[]})
                continue
            if 'id="txtZjzh"' in page:
                result.append({"page":path,"status":"产品页面要求另行登录或当前会话不适用", "query_candidates":[]})
                continue
            paths=self._scripts(page)
            candidates=set()
            for resource in paths:
                # 只读官方页面引用的同源产品脚本，不读取认证表单值或提交任何产品请求。
                if not re.search(r"ttb|finance",resource,re.I):continue
                script=self._text(resource)
                for candidate in re.findall(r"/[A-Za-z][A-Za-z0-9_/]{3,100}",script):
                    if any(word in candidate.lower() for word in ("query","search","earned","position")):
                        candidates.add(candidate)
            result.append({"page":path,"status":"页面可访问，产品数据接口仍待核验", "query_candidates":sorted(candidates), "scripts": paths,
                           "external_scripts": sorted(set(u.scheme+"://"+u.netloc+u.path for src in re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)',page,re.I)
                               for u in [urllib.parse.urlsplit(html.unescape(src))]
                               if u.scheme=="https" and u.hostname=="xcwebresourcewg.securities.eastmoney.com"
                               and re.fullmatch(r"/[A-Za-z0-9/_.-]+\.js",u.path))),
                           "page_scripts": sorted(set(u.path for src in re.findall(r'<script\b[^>]*\bsrc=["\']([^"\']+)',page,re.I)
                               for u in [urllib.parse.urlsplit(html.unescape(src))]
                               if not u.netloc and re.fullmatch(r"/[A-Za-z0-9/_.-]+",u.path))),
                           "inline_query_paths": sorted(set(x for x in re.findall(r'["\'](/[A-Za-z0-9/_-]+)["\']',page)
                               if re.search(r"query|search|earned|position|index|fund",x,re.I))),
                           "notices": sorted(set(re.sub(r"\d{5,}","[编号]",html.unescape(x.strip())) for x in
                               re.findall(r">([^<>]{3,200})<",re.sub(r"<(script|style)\b[^>]*>.*?</\1>","",page,flags=re.S|re.I))
                               if re.search(r"下线|升级|客户端|暂停|停止|迁移|天天基金|不再|手机|开通",x))),
                           "query_pages": sorted(set(html.unescape(x) for x in re.findall(
                               r'href=["\'](/[A-Za-z0-9/_-]+)["\']',page,re.I)
                               if re.search(r"fund|finance",x,re.I) and re.search(r"search|position|index|earned",x,re.I)))})
        return {"sources":result,"supported":False,
                "message":"仅检查官方查询页面与静态脚本；未查询产品数据，不能据此宣称已覆盖持仓。"}

    def logout(self):
        try:
            self._request("/Login/ExitLogin", query={"clear": "1"})
        except Redirected as e:
            # 只把官方登录页重定向视为退出流程完成，绝不跟随。
            if not e.to_login:
                raise BrokerError("官方退出结果未确认") from None


class Session:
    def __init__(self, factory=OfficialClient, wall=time.time, monotonic=time.monotonic, save=None):
        self.factory, self.wall, self.monotonic, self.save = factory, wall, monotonic, save
        self.lock = threading.RLock()
        self.client = None
        self.state = "disconnected"
        self.error = ""
        self.auto = False
        self.nonce = ""
        self.last_wall = self.last_mono = self.last_sync = 0
        self.has_snapshot = False
        self.snapshot_version = ""
        self.analysis_pending = False
        self.query_diagnostics = None
        self.pending_snapshot = None
        self.diagnostic_version = ""
        self.purged = None          # 上一次会话结束时清掉的账户数据行数

    def _touch(self):
        self.last_wall, self.last_mono = self.wall(), self.monotonic()

    def _expired(self):
        return self.client is not None and (self.wall() < self.last_wall or
            self.wall()-self.last_wall >= IDLE_SECONDS or self.monotonic()-self.last_mono >= IDLE_SECONDS)

    def _expire(self):
        if self._expired():
            self._logout("连续 24 小时无操作或系统时间变化，已退出；请重新登录")
            return True
        return False

    def _logout(self, reason=""):
        """会话结束的唯一出口：清内存态 + 清本机保存的账户数据。

        为什么清除放在这里、而不是只放在「退出账户」按钮上
        ------------------------------------------------
        会话结束有 5 条路径，全都走这个函数：用户点退出、连续 24 小时无操作、
        认证过期/被券商踢下线、进程收到 SIGTERM、以及重新打开登录框时放弃旧会话。
        只清内存不清库，就会留下「已断开」但金额和持仓还挂在界面上、而且
        data/portfolio.db 里一直躺着一份完整账户数据的中间态——
        用户退出登录的本意是"这台机器上不要了"，所以退出必须等同于清除。
        """
        client, self.client = self.client, None
        self.auto = self.has_snapshot = self.analysis_pending = False
        self.nonce = self.snapshot_version = ""
        self.state, self.error = "disconnected", reason
        self.query_diagnostics = None
        self.pending_snapshot = None
        self.diagnostic_version = ""
        if client:
            try:
                client.logout()
            except Exception:
                self.error = (reason + "；" if reason else "") + "本机会话已清除，官方退出请求未确认，请在官方客户端核查"
            finally:
                client.close()
        self.purged = self._purge()

    def _purge(self):
        """删掉本机保存的券商账户数据；失败必须让用户看见，不能静默。"""
        try:
            from .portfolio import purge_broker_data
            return purge_broker_data()
        except Exception:
            self.error = (self.error + "；" if self.error else "") + \
                "本机账户数据清除失败，请手动清理 data/portfolio.db 中的券商数据"
            return None

    def _status(self):
        return {"state": self.state, "connected": self.state == "connected", "auto_sync": self.auto,
                "snapshot_current_session": self.has_snapshot,
                "query_error": self.error, "query_diagnostics": self.query_diagnostics,
                "diagnostic_version": self.diagnostic_version,
                "can_reconcile": bool(self.pending_snapshot and self.query_diagnostics and self.query_diagnostics["difference"] > 0),
                "can_confirm": self.state == "connected" and self.has_snapshot and not self.error,
                "snapshot_version": self.snapshot_version, "idle_timeout_seconds": IDLE_SECONDS, "sync_interval_seconds": SYNC_SECONDS}

    def status(self):
        with self.lock:
            self._expire()
            return self._status()

    def initialize(self):
        with self.lock:
            if self.client:
                self._logout()
            self.client = self.factory()
            self._touch()
            try:
                info = self.client.initialize()
                self.nonce = secrets.token_urlsafe(24)
                self.state, self.error = "ready", ""
                return {**self._status(), **info, "flow": self.nonce}
            except Exception as e:
                self._failure(e, close=True)
                return self._status()

    def _check_flow(self, data):
        if self._expire() or not self.client or not self.nonce or not secrets.compare_digest(str(data.get("flow", "")), self.nonce):
            raise BrokerError("登录窗口已失效，请重新打开")

    def _failure(self, exc, close=False):
        self.query_diagnostics = exc.diagnostics if isinstance(exc, SnapshotMismatch) else None
        self.pending_snapshot = exc.candidate if isinstance(exc, SnapshotMismatch) else None
        self.diagnostic_version = secrets.token_urlsafe(18) if self.pending_snapshot else ""
        message = str(exc) if isinstance(exc, BrokerError) else "账户操作失败，已保留上次快照，请手动重试"
        if close or isinstance(exc, (Expired, Unsupported)):
            self._logout(message)
            if isinstance(exc, Unsupported):
                self.state = "unsupported"
        else:
            self.error = message

    def captcha(self, data):
        with self.lock:
            self._check_flow(data)
            if self.state not in ("ready", "sms_required"):
                raise BrokerError("当前无需验证码")
            self._touch()
            return {"image": self.client.captcha()}

    def login(self, data):
        with self.lock:
            self._check_flow(data)
            if self.state != "ready":
                raise BrokerError("请重新初始化登录")
            self._touch()
            auto_analyze = data.get("auto_analyze", False)
            if not isinstance(auto_analyze, bool):
                raise BrokerError("自动分析选项无效")
            try:
                state = self.client.login(str(data.get("account", "")), str(data.get("encrypted_password", "")), str(data.get("captcha", "")))
                self.error = ""
                if state == "authenticated":
                    self.state = "connected"
                    self.analysis_pending = auto_analyze
                    self._refresh()
                elif state == "sms_required":
                    self.state = state
                else:
                    raise BrokerError("无法确认登录结果，请重新登录")
            except Exception as e:
                self._failure(e)
            finally:
                data.clear()  # 调用方不可重用凭证；Python/浏览器无法保证物理内存即时擦除。
            return self._status()

    def sms(self, data, send=False):
        with self.lock:
            self._check_flow(data)
            if self.state != "sms_required":
                raise BrokerError("当前无需短信验证")
            self._touch()
            try:
                self.client.sms(str(data.get("encrypted_mobile", "")), str(data.get("code", "")), send)
                self.error = ""
                if not send:
                    self.state = "ready"
            except Exception as e:
                self._failure(e)
            finally:
                data.clear()
            return self._status()

    def _refresh(self):
        if self._expire() or self.state != "connected":
            return
        self.last_sync = self.monotonic()
        self.query_diagnostics = None
        self.pending_snapshot = None
        self.diagnostic_version = ""
        try:
            try:
                payload = validate_snapshot(self.client.query())
            except SnapshotMismatch as exc:
                payload = reconcile_confirmed(exc)
                if payload is None:
                    raise
            if self._expire():
                return
            if self.save:
                self.save(payload)
            else:
                from .portfolio import save_broker_snapshot
                save_broker_snapshot(payload)
            self.has_snapshot, self.error = True, ""
            self.snapshot_version = secrets.token_urlsafe(18)
        except Exception as e:
            self._failure(e)

    def reconcile(self, version):
        with self.lock:
            if self._expire() or self.state != "connected" or not self._status()["can_reconcile"] or not secrets.compare_digest(str(version), self.diagnostic_version):
                raise BrokerError("市值核对已过期，请立即同步后重新核对")
            self._touch()
            save_confirmation(self.query_diagnostics)
            # 重新查询并匹配金额/持仓数量，不能提交已经变化的候选快照。
            self._refresh()
            return self._status()

    def claim_analysis(self):
        """每次登录授权一次；首次完整快照前不领取，状态轮询不能触发。"""
        with self.lock:
            self._expire()
            if self.analysis_pending and self.state == "connected" and self.has_snapshot and not self.error:
                self.analysis_pending = False
                return self.nonce
            return None

    def analysis_allowed(self, login_id):
        with self.lock:
            self._expire()
            return (self.state == "connected" and self.has_snapshot and not self.error
                    and bool(login_id) and secrets.compare_digest(login_id, self.nonce))

    def product_capabilities(self):
        with self.lock:
            if self._expire() or self.state != "connected":
                raise BrokerError("请先在本机看板登录东方财富")
            self._touch()
            try:
                return self.client.product_capabilities()
            except Expired as exc:
                # 专项权限失效不等于证券登录失效；以原本已适配的只读账户查询确认。
                try:
                    self.client.query()
                except SnapshotMismatch:
                    pass  # 有财务响应，仅汇总口径需核对，认证仍有效。
                except Expired as account_error:
                    self._failure(account_error)
                    raise
                except BrokerError:
                    pass
                return {"sources":[],"supported":False,
                        "message":"专项查询被拒绝；未将其误判为证券账户退出。请使用官方导出文件核对。"}

    def refresh(self):
        with self.lock:
            if not self._expire() and self.state == "connected":
                self._touch()
                self._refresh()
            return self._status()

    def confirm(self, version):
        with self.lock:
            self._expire()
            if not self._status()["can_confirm"] or not secrets.compare_digest(str(version), self.snapshot_version):
                raise BrokerError("请先成功读取并核对当前登录账户的完整快照")
            self._touch()
            self.auto = True
            return self._status()

    def activity(self):
        with self.lock:
            if not self._expire() and self.client:
                self._touch()
            return self._status()

    def logout(self):
        with self.lock:
            self._logout()
            # purged 只在这次响应里回显一次：前端要拿它告诉用户"清掉了什么"，
            # 但不能留在 status 里被轮询反复当成新事件。
            purged, self.purged = self.purged, None
            return {**self._status(), "purged": purged or {}}

    def tick(self):
        with self.lock:
            if not self._expire() and self.auto and self.state == "connected" and self.monotonic()-self.last_sync >= SYNC_SECONDS:
                self._refresh()


session = Session()


def purge_orphaned():
    """启动时清掉上一次进程遗留的券商账户数据。

    正常停止会走 dashboard.serve() 的 finally -> session.logout() -> 清除，
    但 SIGKILL、断电、崩溃时 finally 不执行，库里就会留下一份"没有会话却在磁盘上"
    的完整账户数据。看板一起来就清掉：此刻一定不存在活会话，留着它唯一的后果
    就是下一个人打开看板还能看到上一个人的钱。

    纯手动记账的用户库里没有券商数据，has_broker_data() 为假，直接跳过，
    不会白白删掉他的 analysis 缓存。
    """
    from . import portfolio
    try:
        if not portfolio.has_broker_data():
            return None
        counts = portfolio.purge_broker_data()
    except Exception:
        return None
    total = sum(v for v in counts.values() if isinstance(v, int))
    if total:
        print(f"  已清除上次进程遗留的账户数据（{total} 行）")
    return counts


def start(stop):
    purge_orphaned()

    def loop():
        while not stop.wait(1):
            try:
                session.tick()
            except Exception:
                # 绝不打印异常上下文（可能含认证信息）；下次用户请求可手动恢复。
                with session.lock:
                    session._failure(BrokerError("账户后台同步异常，请手动刷新"))
    thread = threading.Thread(target=loop, name="eastmoney-readonly", daemon=True)
    thread.start()
    return thread
