<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { tryReq } from "@/toast";

const app = useApp();
const loading = ref(false);
const tab = ref<"events" | "news" | "hard">("events");
const symbols = ref("");
const start = ref(defaultStart());
const end = ref("");
const events = ref<any[]>([]);
const news = ref<any[]>([]);
const hard = ref<any[]>([]);
const hardCats = ref<string[]>([]);

function defaultStart() {
  const d = new Date();
  d.setMonth(d.getMonth() - 3);
  return d.toISOString().slice(0, 10);
}

const hardCount = computed(() => events.value.filter((e) => e.is_hard_negative).length);

async function load() {
  loading.value = true;
  const m = app.mode;
  const sym = symbols.value.trim() || undefined;
  const s = start.value || undefined;
  const e = end.value || undefined;
  if (tab.value === "events") {
    const r = await tryReq(() => api.eventEvents(m, sym, s, e, 200));
    events.value = r?.events || [];
    hardCats.value = r?.hard_negative_categories || hardCats.value;
  } else if (tab.value === "news") {
    const r = await tryReq(() => api.eventNews(m, sym, s, e, 200));
    news.value = r?.news || [];
    hardCats.value = r?.hard_negative_categories || hardCats.value;
  } else {
    const r = await tryReq(() => api.eventHardNegatives(m, sym, s, e));
    hard.value = r?.hard_negatives || [];
  }
  loading.value = false;
}

function switchTab(t: any) { tab.value = t; load(); }
function sentBadge(v: any) {
  const n = Number(v);
  if (n > 0.1) return "ok";
  if (n < -0.1) return "danger";
  return "muted";
}
// 情绪分 → 中文标签（悬停可见原始分值）
function sentLabel(v: any) {
  if (v == null) return "-";
  const n = Number(v);
  if (n > 0.1) return "利好";
  if (n < -0.1) return "利空";
  return "中性";
}
function sentTitle(v: any) {
  return v != null ? `情绪分 ${Number(v).toFixed(2)}（-1 最利空 / +1 最利好）` : "暂无情绪分";
}

onMounted(load);
watch(() => app.mode, load);
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>📰 事件驱动
        <span class="sub">事件/新闻经因子层与硬负面一票否决融入决策——规则先行，不等 LLM（P3）</span>
      </h3>
      <div class="row">
        <div style="flex:2"><label>标的（逗号分隔，留空=全市场）</label>
          <input v-model="symbols" placeholder="600076.SH,000001.SZ" /></div>
        <div><label>开始日期</label><input v-model="start" type="date" /></div>
        <div><label>结束日期</label><input v-model="end" type="date" /></div>
        <div style="flex:0 0 auto"><label>&nbsp;</label><button @click="load">查询</button></div>
      </div>
      <div style="margin-top:12px; display:flex; gap:6px">
        <button class="btn sm" :class="tab === 'events' ? '' : 'ghost'" @click="switchTab('events')">公司事件</button>
        <button class="btn sm" :class="tab === 'news' ? '' : 'ghost'" @click="switchTab('news')">新闻舆情</button>
        <button class="btn sm" :class="tab === 'hard' ? '' : 'ghost'" @click="switchTab('hard')">
          硬负面事件 <span v-if="tab === 'hard'">({{ hard.length }})</span>
        </button>
      </div>
      <div class="tiny muted" style="margin-top:10px" v-if="hardCats.length">
        硬负面类别（命中即触发规则减仓/禁买，LLM 无权覆盖）：
        <span v-for="c in hardCats" :key="c" class="badge danger" style="margin-right:4px">{{ c }}</span>
      </div>
    </div>

    <div class="card" v-if="tab === 'events'">
      <h3>公司事件 <span class="sub">{{ events.length }} 条，其中硬负面 {{ hardCount }} 条</span></h3>
      <div style="max-height:560px;overflow:auto">
        <table>
          <thead>
            <tr><th style="width:130px">时间</th><th style="width:100px">标的</th><th style="width:120px">类别</th>
              <th>标题</th><th style="width:70px">重要性</th><th style="width:80px">情绪</th><th style="width:80px">硬负面</th></tr>
          </thead>
          <tbody>
            <tr v-for="e in events" :key="e.id">
              <td class="tiny pill">{{ String(e.ann_time).replace("T", " ").slice(0, 16) }}</td>
              <td class="tiny">{{ e.symbol }}</td>
              <td><span class="badge muted">{{ e.category }}</span></td>
              <td>{{ e.title }}<div class="tiny muted" v-if="e.detail">{{ String(e.detail).slice(0, 80) }}</div></td>
              <td class="pill tiny">{{ e.importance ?? "-" }}</td>
              <td><span class="badge" :class="sentBadge(e.sentiment)" :title="sentTitle(e.sentiment)">{{ sentLabel(e.sentiment) }}</span></td>
              <td><span v-if="e.is_hard_negative" class="badge danger">否决</span><span v-else class="muted tiny">-</span></td>
            </tr>
            <tr v-if="!events.length"><td colspan="7" class="muted">无事件数据</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card" v-else-if="tab === 'news'">
      <h3>新闻舆情 <span class="sub">{{ news.length }} 条 · 用于 news_sentiment_* 因子</span></h3>
      <div style="max-height:560px;overflow:auto">
        <table>
          <thead><tr><th style="width:130px">时间</th><th style="width:100px">标的</th><th>标题</th>
            <th style="width:110px">来源</th><th style="width:80px">情绪</th></tr></thead>
          <tbody>
            <tr v-for="(n, i) in news" :key="i">
              <td class="tiny pill">{{ String(n.publish_time || n.time || "").replace("T", " ").slice(0, 16) }}</td>
              <td class="tiny">{{ n.symbol || "-" }}</td>
              <td>{{ n.title }}</td>
              <td class="tiny muted">{{ n.source || "-" }}</td>
              <td><span class="badge" :class="sentBadge(n.sentiment)" :title="sentTitle(n.sentiment)">{{ sentLabel(n.sentiment) }}</span></td>
            </tr>
            <tr v-if="!news.length"><td colspan="5" class="muted">无新闻数据</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card" v-else>
      <h3>⛔ 硬负面事件 <span class="sub">{{ hard.length }} 条 · 命中即规则先行减仓，不等 LLM 判断</span></h3>
      <table>
        <thead><tr><th style="width:130px">时间</th><th style="width:100px">标的</th><th style="width:140px">类别</th><th>标题</th><th style="width:80px">情绪</th></tr></thead>
        <tbody>
          <tr v-for="h in hard" :key="h.id">
            <td class="tiny pill">{{ String(h.ann_time).replace("T", " ").slice(0, 16) }}</td>
            <td class="tiny"><b>{{ h.symbol }}</b></td>
            <td><span class="badge danger">{{ h.category }}</span></td>
            <td>{{ h.title }}</td>
            <td><span class="badge" :class="sentBadge(h.sentiment)" :title="sentTitle(h.sentiment)">{{ sentLabel(h.sentiment) }}</span></td>
          </tr>
          <tr v-if="!hard.length"><td colspan="5" class="muted">该区间无硬负面事件 ✅</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
