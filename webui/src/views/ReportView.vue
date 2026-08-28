<script setup lang="ts">
// 绩效报告：日报/周报/阶段报告清单 + Markdown 内容查看（复盘/evolve 任务落盘产出）。
import { computed, onMounted, ref } from "vue";
import api from "@/api";
import { tryReq } from "@/toast";

const reports = ref<any[]>([]);
const kind = ref(""); // "" = 全部
const cur = ref<any>(null);
const content = ref("");
const loadingList = ref(false);
const loadingDoc = ref(false);

const memory = ref<{ short_term: { date: string; items: string[] }; long_term: any[] }>(
  { short_term: { date: "", items: [] }, long_term: [] },
);
const loadingMem = ref(false);

const KINDS = [
  { v: "", label: "全部" },
  { v: "daily", label: "日报" },
  { v: "weekly", label: "周报" },
  { v: "stage", label: "阶段报告" },
  { v: "reflection", label: "自我复盘" },
];

async function loadList() {
  loadingList.value = true;
  const r = await tryReq(() => api.reportList());
  reports.value = r?.reports || [];
  loadingList.value = false;
  // 默认打开最新一份（优先阶段报告 > 周报 > 日报）
  if (!cur.value && reports.value.length) {
    const pref = ["stage", "weekly", "daily"]
      .flatMap((k) => reports.value.filter((x: any) => x.kind === k));
    await open(pref[0] || reports.value[0]);
  }
}

async function open(r: any) {
  cur.value = r;
  loadingDoc.value = true;
  const res = await tryReq(() => api.reportContent(r.name));
  content.value = res?.content || "";
  loadingDoc.value = false;
}

const filtered = computed(() =>
  kind.value ? reports.value.filter((r: any) => r.kind === kind.value) : reports.value);

function fmtDate(s: string) {
  return s && s.length === 8 ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}
function fmtSize(n: number) {
  return n >= 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B";
}

// ---------- 轻量 Markdown → HTML（先转义再渲染，无第三方依赖） ----------
function esc(s: string) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
function inline(s: string) {
  return s
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/`([^`]+)`/g, "<code>$1</code>");
}
function mdToHtml(md: string) {
  const lines = esc(md).split(/\r?\n/);
  const out: string[] = [];
  let inTable = false, inList = false;
  const closeTable = () => { if (inTable) { out.push("</tbody></table>"); inTable = false; } };
  const closeList = () => { if (inList) { out.push("</ul>"); inList = false; } };
  for (const raw of lines) {
    const t = raw.trim();
    if (!t) { closeTable(); closeList(); continue; }
    if (/^\|?[\s:|-]+\|?$/.test(t) && t.includes("-")) continue; // 表格分隔行
    if (t.startsWith("|")) {
      closeList();
      const cells = t.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      if (!inTable) {
        inTable = true;
        out.push('<table><tbody><tr>' + cells.map((c) => `<th>${inline(c)}</th>`).join("") + "</tr>");
      } else {
        out.push("<tr>" + cells.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>");
      }
      continue;
    }
    closeTable();
    const h = t.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList();
      const lv = Math.min(h[1].length + 1, 5);
      out.push(`<h${lv}>${inline(h[2])}</h${lv}>`);
      continue;
    }
    if (/^-{3,}$/.test(t)) { closeList(); out.push("<hr/>"); continue; }
    if (/^[-*]\s+/.test(t)) {
      if (!inList) { inList = true; out.push("<ul>"); }
      out.push(`<li>${inline(t.replace(/^[-*]\s+/, ""))}</li>`);
      continue;
    }
    closeList();
    out.push(`<p>${inline(t)}</p>`);
  }
  closeTable(); closeList();
  return out.join("\n");
}
const html = computed(() => mdToHtml(content.value));

async function loadMemory() {
  loadingMem.value = true;
  const r = await tryReq(() => api.memory());
  memory.value = r || { short_term: { date: "", items: [] }, long_term: [] };
  loadingMem.value = false;
}

onMounted(() => {
  loadList();
  loadMemory();
});
</script>

<template>
  <div class="rpt-layout">
    <!-- 左：报告清单 -->
    <aside class="rpt-side">
      <!-- 记忆面板：短期（明日待办）/ 长期（持久原则），随时预览 -->
      <section class="card mem-card">
        <h3>🧠 记忆
          <div class="spacer"></div>
          <button class="btn sm ghost" :disabled="loadingMem" @click="loadMemory">刷新</button>
        </h3>

        <div class="mem-sec">
          <div class="mem-h">📌 短期记忆（明日待办）
            <span class="sub" v-if="memory.short_term.date">{{ memory.short_term.date }}</span>
          </div>
          <ul v-if="memory.short_term.items.length" class="mem-list">
            <li v-for="(x, i) in memory.short_term.items" :key="i">{{ x }}</li>
          </ul>
          <div v-else class="muted tiny">暂无（盘后复盘自动生成）</div>
        </div>

        <div class="mem-sec">
          <div class="mem-h">🏛 长期记忆（持久原则）
            <span class="sub">{{ memory.long_term.length }} 条</span>
          </div>
          <ul v-if="memory.long_term.length" class="mem-list">
            <li v-for="(m, i) in memory.long_term.slice(0, 10)" :key="i">
              <span class="mem-tag" v-if="m.tag">{{ m.tag }}</span>
              {{ m.text }}
              <span class="mem-occ" v-if="m.occurrences > 1">×{{ m.occurrences }}</span>
            </li>
          </ul>
          <div v-else class="muted tiny">暂无（跨日累积沉淀）</div>
        </div>
      </section>

      <section class="card">
        <h3>📄 报告清单
          <span class="sub">{{ filtered.length }} 份</span>
          <div class="spacer"></div>
          <button class="btn sm ghost" :disabled="loadingList" @click="loadList">刷新</button>
        </h3>
        <div class="kind-tabs">
          <button
            v-for="k in KINDS" :key="k.v"
            class="btn sm" :class="kind === k.v ? '' : 'ghost'"
            @click="kind = k.v"
          >{{ k.label }}</button>
        </div>
        <div class="rpt-list">
          <button
            v-for="r in filtered" :key="r.name"
            class="rpt-item" :class="{ on: cur?.name === r.name }"
            @click="open(r)"
          >
            <span class="badge sm" :class="r.kind === 'stage' ? 'ok' : r.kind === 'weekly' ? 'info' : r.kind === 'reflection' ? 'refl' : ''">
              {{ r.kind_label }}
            </span>
            <span class="rd">{{ fmtDate(r.date) }}<template v-if="r.date_end"> ~ {{ fmtDate(r.date_end) }}</template></span>
            <span class="rs tiny muted">{{ fmtSize(r.size) }}</span>
          </button>
          <div v-if="!filtered.length" class="muted" style="font-size:13px; padding:8px 4px">
            暂无报告。运行复盘（review）/进化（evolve）任务后自动生成。
          </div>
        </div>
      </section>
    </aside>

    <!-- 右：报告内容 -->
    <main class="rpt-main">
      <section class="card">
        <h3 v-if="cur">
          {{ cur.kind_label }}：{{ fmtDate(cur.date) }}<template v-if="cur.date_end"> ~ {{ fmtDate(cur.date_end) }}</template>
          <span class="sub">{{ cur.name }}</span>
        </h3>
        <div v-if="loadingDoc" class="muted">加载中…</div>
        <div v-else-if="cur" class="md-body" v-html="html"></div>
        <div v-else class="muted">选择左侧报告查看内容。</div>
      </section>
    </main>
  </div>
</template>

<style scoped>
.rpt-layout {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.rpt-side { width: 320px; flex: 0 0 320px; }
.rpt-main { flex: 1; min-width: 0; }
.kind-tabs { display: flex; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
.rpt-list {
  display: flex;
  flex-direction: column;
  gap: 5px;
  max-height: 640px;
  overflow: auto;
}
.rpt-item {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  text-align: left;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--text);
  cursor: pointer;
  transition: all 0.15s;
}
.rpt-item:hover { border-color: var(--primary); }
.rpt-item.on { border-color: var(--primary); background: var(--bg-2); }
.rpt-item .rd { flex: 1; font-size: 13px; font-variant-numeric: tabular-nums; }
.badge.sm { font-size: 11px; padding: 1px 6px; }
.badge.refl { background: #ede7f6; color: #5e35b1; border: 1px solid #d1c4e9; }

/* 记忆面板 */
.mem-card .mem-sec { margin-bottom: 12px; }
.mem-card .mem-sec:last-child { margin-bottom: 0; }
.mem-h { font-size: 13px; font-weight: 600; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; }
.mem-list { list-style: none; padding: 0; margin: 0; display: flex; flex-direction: column; gap: 5px; max-height: 220px; overflow: auto; }
.mem-list li { font-size: 12.5px; line-height: 1.5; padding: 5px 8px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg-elev); color: var(--text); }
.mem-tag { display: inline-block; font-size: 10.5px; padding: 0 5px; border-radius: 4px;
  background: #ede7f6; color: #5e35b1; margin-right: 4px; }
.mem-occ { color: var(--primary); font-weight: 600; margin-left: 4px; }
.tiny { font-size: 12px; }
.md-body { line-height: 1.75; font-size: 14px; }
.md-body :deep(h2) { margin: 18px 0 8px; font-size: 18px; }
.md-body :deep(h3) { margin: 14px 0 6px; font-size: 16px; }
.md-body :deep(h4), .md-body :deep(h5) { margin: 10px 0 4px; font-size: 14px; }
.md-body :deep(p) { margin: 6px 0; }
.md-body :deep(ul) { margin: 6px 0; padding-left: 22px; }
.md-body :deep(code) {
  background: var(--bg-2);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0 4px;
  font-size: 12px;
}
.md-body :deep(hr) { border: none; border-top: 1px solid var(--border); margin: 12px 0; }
@media (max-width: 980px) {
  .rpt-layout { flex-direction: column; }
  .rpt-side { width: 100%; flex: none; }
}
</style>
