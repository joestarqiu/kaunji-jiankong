"""GitHub Actions entrypoint for the six-index buy/sell monitor."""
from __future__ import annotations

import json
import os
import ssl
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "agent" / "data" / "index_monitor_state.json"
INDEXES = {
    "中证红利（000922.SH）": ("000922", [5350, 5150, 4900]),
    "沪深300（000300.SH）": ("000300", [4400, 4150, 3900]),
    "中证A500（000510.SH）": ("000510", [5400, 5000, 4700]),
    "中证500（000905.SH）": ("000905", [7100, 6600, 6100]),
    "中证港股通高股息（930914）": ("930914", [5000, 4600, 4200]),
    "中证红利低波动100（930955）": ("930955", [11000, 10400, 9800]),
}


def fetch(code: str) -> tuple[str, float, float, list[float]] | None:
    qs = urlencode({"secid": f"1.{code}", "klt": 101, "fqt": 0, "beg": (date.today() - timedelta(days=1100)).isoformat().replace("-", ""), "end": "20500101", "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56"})
    req = Request("https://push2his.eastmoney.com/api/qt/stock/kline/get?" + qs, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urlopen(req, timeout=20, context=ssl.create_default_context()) as response:
            data = json.loads(response.read().decode("utf-8")).get("data") or {}
        rows = data.get("klines") or []
        if not rows:
            return None
        parsed = [r.split(",") for r in rows]
        last, previous = parsed[-1], parsed[-2] if len(parsed) > 1 else parsed[-1]
        close = float(last[2]); prev = float(previous[2])
        return last[0], close, (close / prev - 1) * 100 if prev else 0, [float(r[2]) for r in parsed]
    except Exception:
        # Tencent provides a lightweight current quote fallback for A-share indices.
        try:
            req = Request(f"https://qt.gtimg.cn/q=s_sh{code}", headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as response: raw = response.read().decode("gb18030", errors="replace")
            fields = raw.split('="', 1)[1].rstrip('";').split("~")
            if len(fields) > 5 and fields[3]:
                close, change = float(fields[3]), float(fields[5])
                return date.today().isoformat(), close, change, [close]
        except Exception:
            pass
        return None


def fetch_yahoo(code: str) -> tuple[str, float, float, list[float]] | None:
    """Fallback for CSI strategy indices that Eastmoney does not expose."""
    symbol = f"{code}.SS"
    req = Request(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=3y&interval=1d",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urlopen(req, timeout=20, context=ssl.create_default_context()) as response:
            result = json.loads(response.read().decode("utf-8"))["chart"]["result"][0]
        closes = [float(x) for x in result["indicators"]["quote"][0]["close"] if x is not None]
        timestamps = result["timestamp"]
        if len(closes) < 2 or not timestamps:
            return None
        return datetime.fromtimestamp(timestamps[-1]).date().isoformat(), closes[-1], (closes[-1] / closes[-2] - 1) * 100, closes
    except Exception:
        return None


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"indexes": {name: {"buy_stage": 0, "sell_stage": 0} for name in INDEXES}}


def send_feishu(text: str) -> None:
    import urllib.request
    app_id, secret, open_id = os.environ["FEISHU_APP_ID"], os.environ["FEISHU_APP_SECRET"], os.environ["FEISHU_RECIPIENT_OPEN_ID"]
    def post(url: str, payload: dict, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token: headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers)
        with urllib.request.urlopen(req, timeout=20) as r: return json.loads(r.read().decode("utf-8"))
    token = post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal", {"app_id": app_id, "app_secret": secret})["tenant_access_token"]
    result = post("https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id", {"receive_id": open_id, "msg_type": "text", "content": json.dumps({"text": text}, ensure_ascii=False)}, token)
    if result.get("code") != 0: raise RuntimeError(result)


def main() -> None:
    state = load_state(); lines = [f"【大A投研看板】指数买卖日报（{date.today()}）", ""]
    for name, (code, buys) in INDEXES.items():
        item = state["indexes"].setdefault(name, {"buy_stage": 0, "sell_stage": 0})
        result = fetch(code) or fetch_yahoo(code)
        stale = False
        if result:
            day, close, change, history = result
            item.update({"date": day, "close": close, "change": change, "history": history[-800:]})
        elif item.get("close") is not None:
            day, close, change, history = item["date"], item["close"], None, item.get("history", [])
            stale = True
        else:
            lines += [name, "数据暂不可用，尚无可沿用的历史数据。", ""]; continue
        q = sorted(history); n = len(q)
        p80, p90, p95 = (q[min(n - 1, int(n * p) - 1)] for p in (0.80, 0.90, 0.95)) if n else (None, None, None)
        buy_stage = item.get("buy_stage", 0)
        buy_triggered = None
        if buy_stage < 3 and close <= buys[buy_stage]:
            item["buy_stage"] = buy_stage + 1
            buy_triggered = buy_stage + 1
        sell_stage = item.get("sell_stage", 0)
        sell_targets = [p80, p90, p95]
        sell_triggered = None
        if n >= 200 and sell_stage < 3 and sell_targets[sell_stage] is not None and close >= sell_targets[sell_stage]:
            item["sell_stage"] = sell_stage + 1
            sell_triggered = sell_stage + 1
        tag = f"（沿用{day}，非当日数据）" if stale else ""
        next_buy = buys[min(item["buy_stage"], 2)]
        if buy_triggered:
            buy_text = f"买入结论：达到第{buy_triggered}档，建议按计划买入。"
        else:
            buy_text = f"买入结论：暂不买入，等待第{min(item['buy_stage'] + 1, 3)}档 {next_buy:.2f}点。"
        reductions = [20, 50, 80]
        if sell_triggered:
            sell_text = f"卖出结论：达到第{sell_triggered}档，建议累计减仓至{reductions[sell_triggered - 1]}%。"
        elif n < 200:
            sell_text = "卖出结论：暂不卖出（历史点位不足，暂不计算卖出分位）。"
        elif item["sell_stage"] == 0:
            sell_text = f"卖出结论：暂不卖出，继续持有观察；第1档约 {p80:.2f}点。"
        elif item["sell_stage"] < 3:
            sell_text = f"卖出结论：已完成第{item['sell_stage']}档减仓，当前等待第{item['sell_stage'] + 1}档约 {sell_targets[item['sell_stage']]:.2f}点。"
        else:
            sell_text = "卖出结论：三档减仓已完成，继续评估持仓。"
        lines += [name, f"数据日期：{day}{tag}", f"收盘：{close:.2f}点；涨跌幅：{'未知' if change is None else f'{change:+.2f}%'}", buy_text, sell_text, "",]
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True); STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    message = "\n".join(lines)
    if os.getenv("MONITOR_DRY_RUN") == "1":
        print(message)
    else:
        send_feishu(message)


if __name__ == "__main__": main()
