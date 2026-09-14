# TradeView.vue 修改补丁（待人工/下一会话应用）
# 因 target-anchor gate 拦截，本文件作为修改方案载体落盘

## 修改 1：实盘订单表加名称列（第 348-364 行区域）
# 表头：
- <thead><tr><th>标的</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>时间</th></tr></thead>
+ <thead><tr><th>代码</th><th>名称</th><th>方向</th><th>数量</th><th>价格</th><th>状态</th><th>时间</th></tr></thead>

# 行模板：
- <td><a class="sym-link" @click="openSymbol(o.symbol)">{{ o.symbol }}</a></td>
+ <td><a class="sym-link" @click="openSymbol(o.symbol, o.name || symbolName(o.symbol))">{{ o.symbol }}</a></td>
+ <td class="tiny">{{ o.name || symbolName(o.symbol) || "-" }}</td>

# 空态 colspan：
- <td colspan="6" class="muted">无订单</td>
+ <td colspan="7" class="muted">无订单</td>

## 修改 2：script setup 新增（放在 symbols ref 定义之后）
const symbolMap = computed(() => Object.fromEntries(symbols.value.map((s: any) => [s.symbol, s.name])));
function symbolName(sym?: string) { return sym ? (symbolMap.value[sym] || "") : ""; }

## 修改 3：Tab active 态强化（scoped style）
.tv-tab { opacity: .78; }
.tv-tab.active { opacity: 1; box-shadow: inset 0 -3px 0 0 var(--primary); }
