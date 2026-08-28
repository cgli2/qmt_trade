"""规则化新闻情绪打分（中文财经词典，无 LLM 依赖）。

真实数据源（akshare 东财快讯）不提供情绪字段，DataHub 在 get_news 出口对
``sentiment is None`` 的条目统一补分，保证 UI 展示与 news_sentiment_5d 等
因子拿到的不是恒 0。打分只基于标题+摘要的关键词命中与简单否定翻转，
取值 [-1, 1]：正数偏多、负数偏空、0 为中性/无信号。
"""

from __future__ import annotations

#: 正面关键词 → 权重（财经语境下的利好表述）
_POSITIVE: dict[str, float] = {
    "业绩预增": 1.0, "预增": 0.8, "业绩大增": 1.0, "净利润增长": 0.8, "利润增长": 0.7,
    "扭亏": 0.8, "扭亏为盈": 1.0, "超预期": 0.8, "创新高": 0.7, "历史新高": 0.8,
    "中标": 0.7, "签订": 0.4, "签约": 0.5, "大单": 0.3, "订单": 0.3, "回购": 0.6,
    "增持": 0.7, "股东增持": 0.8, "分红": 0.5, "高送转": 0.5, "获批": 0.7,
    "通过审批": 0.6, "专利": 0.3, "突破": 0.4, "利好": 0.8, "涨停": 0.4,
    "战略合作": 0.5, "重组": 0.3, "并购": 0.2, "扩产": 0.4, "产能扩张": 0.5,
    "上调评级": 0.7, "买入评级": 0.5, "目标价上调": 0.6, "业绩快报增长": 0.7,
}

#: 负面关键词 → 权重（财经语境下的利空表述）
_NEGATIVE: dict[str, float] = {
    "业绩预减": 1.0, "预减": 0.8, "业绩预亏": 1.0, "预亏": 0.9, "亏损": 0.7,
    "业绩下滑": 0.8, "利润下滑": 0.7, "低于预期": 0.6, "不及预期": 0.6,
    "减持": 0.7, "股东减持": 0.8, "质押": 0.3, "爆仓": 0.8, "立案": 1.0,
    "调查": 0.6, "被查": 0.9, "处罚": 0.8, "警示函": 0.7, "违规": 0.7,
    "退市": 1.0, "退市风险": 1.0, "戴帽": 0.8, "ST": 0.5, "跌停": 0.4,
    "利空": 0.8, "诉讼": 0.4, "仲裁": 0.4, "违约": 0.8, "商誉减值": 0.8,
    "计提减值": 0.6, "终止重组": 0.5, "停产": 0.6, "召回": 0.5, "事故": 0.5,
    "下调评级": 0.7, "卖出评级": 0.5, "目标价下调": 0.6, "高管辞职": 0.3,
}

#: 否定前缀：命中词紧邻其前 3 字内出现时正负翻转（如"未获批""不减持"）
_NEGATORS = ("未", "不", "无", "非", "难")

#: 强否定词：出现在命中词前 12 字内即翻转整句（如"澄清：未收到立案调查通知"）
_CLAUSE_NEGATORS = ("否认", "辟谣", "澄清", "终止", "取消", "未", "不", "无")


def _hits(text: str, lexicon: dict[str, float]) -> tuple[float, float, bool]:
    """返回 (直向权重, 被否定翻转的权重, 是否有命中)。"""
    direct = 0.0
    flipped = 0.0
    for word, w in lexicon.items():
        idx = text.find(word)
        while idx >= 0:
            prefix = text[max(0, idx - 3):idx]
            near_neg = any(prefix.endswith(n) or prefix == n for n in _NEGATORS)
            clause_neg = any(n in text[max(0, idx - 12):idx] for n in _CLAUSE_NEGATORS)
            if near_neg or clause_neg:
                flipped += w
            else:
                direct += w
            idx = text.find(word, idx + len(word))
    return direct, flipped, (direct + flipped) > 0


def score_sentiment(title: str, content: str = "") -> float:
    """标题+摘要 → [-1, 1] 情绪分。标题权重加倍（标题信号更强）。无命中返回 0。"""
    if not title and not content:
        return 0.0
    # 直向命中：正面词计正、负面词计负；被否定翻转的命中反向计入
    tp, tp_flip, _h1 = _hits(title, _POSITIVE)
    tn, tn_flip, _h2 = _hits(title, _NEGATIVE)
    cp, cp_flip, _h3 = _hits(content[:300], _POSITIVE)
    cn, cn_flip, _h4 = _hits(content[:300], _NEGATIVE)
    # 标题命中权重 ×2（标题信号更强）
    pos = 2 * tp + cp + 2 * tn_flip + cn_flip   # 否定负词 → 正面（如"未亏损"）
    neg = 2 * tn + cn + 2 * tp_flip + cp_flip   # 否定正词 → 负面（如"未获批"）
    total = pos + neg
    if total <= 0:
        return 0.0
    return max(-1.0, min(1.0, round((pos - neg) / total, 2)))
