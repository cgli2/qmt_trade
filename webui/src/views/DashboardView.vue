<script setup lang="ts">
// 工作台：关键状态一眼看清 + 高频操作一键直达 + 精选/报告/持仓摘要，
// 下方保留体检/调度/密钥等系统管理区块。
import { computed, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import api from "@/api";
import { useApp } from "@/store";
import { tryReq, pushToast } from "@/toast";
import Modal from "@/components/Modal.vue";

const app = useApp();
const router = useRouter();
const loading = ref(false);
const ov = ref<any>(null);
const health = ref<any>(null);
const sched = ref<any>(null);
const positions = ref<any[]>([]);
const posSource = ref("book"); // book=本地账本 / broker=券商实时持仓（live 账本空时回落）
const posNote = ref("");
const finals = ref<any[]>([]);
const reports = ref<any[]>([]);
const secrets = ref<any[]>([]);
const runResult = ref<any>(null);
const showResult = ref(false);
const secretEdit = ref<{ key: string; value: string } | null>(null);
const jobEdit = ref<any>(null); // 正在编辑的调度任务
const jobSaving = ref(false);
const busy = ref(""); // 当前正在执行的一键操作文案

async function load() {
  loading.value = true;
  const m = app.mode;
  const [o, h, s, pos, fin, rep, sec] = await Promise.all([
    tryReq(() => api.overview(m)),
    tryReq(() => api.health(m)),
    tryReq(() => api.schedulerJobs(m)),
    tryReq(() => api.positions(m)),
    tryReq(() => api.selectionFinal(m)),
    tryReq(() => api.reportList()),
    tryReq(() => api.secrets()),
  ]);
  ov.value = o;
  health.value = h;
  sched.value = s;
  positions.value = pos?.positions || [];
  posSource.value = pos?.source || "book";
  posNote.value = pos?.note || "";
  finals.value = fin?.picks || [];
  reports.value = (rep?.reports || []).slice(0, 3);
  secrets.value = sec || [];
  loading.value = false;
}

// ---------- 一键操作 ----------
async function pollJob(id: string, doneMsg: () => string) {
  for (let i = 0; i < 80; i++) {
    await new Promise((res) => setTimeout(res, 3000));
    const j = await tryReq(() => api.job(id));
    if (j === undefined) {
      // 404：后端重启后内存 Job 丢失，继续轮询只会反复弹错，立即停止
      busy.value = "";
      pushToast("任务记录已失效（后端可能重启过），请在对应页面查看结果", "info");
      return;
    }
    if (j?.status === "done") { pushToast(doneMsg(), "ok"); busy.value = ""; await load(); return; }
    if (j?.status === "error") {
      pushToast("任务失败：" + (j.error || "未知错误"), "err");
      busy.value = "";
      return;
    }
  }
  busy.value = "";
  pushToast("任务超时，请稍后在对应页面查看结果", "info");
}

async function doSelection() {
  if (busy.value) return;
  busy.value = "选股中…";
  const r = await tryReq(() => api.selectionRun({}, app.mode));
  if (!r?.job_id) { busy.value = ""; return; }
  pollJob(r.job_id, () => "选股完成，前往「选股研判」查看候选与精选");
}

async function doResearch() {
  if (busy.value) return;
  busy.value = "AI 研判中（多 Agent，需数分钟）…";
  const r = await tryReq(() => api.runPlan(app.mode, undefined, true));
  busy.value = "";
  if (r) { pushToast("AI 研判完成，前往「选股研判」查看精选", "ok"); load(); }
}

async function doPlan() {
  if (busy.value) return;
  busy.value = "执行交易计划…";
  const r = await tryReq(() => api.runPlan(app.mode));
  busy.value = "";
  if (r) {
    runResult.value = r;
    showResult.value = true;
    load();
  }
}

async function doReview() {
  if (busy.value) return;
  busy.value = "每日复盘中…";
  const r = await tryReq(() => api.strategyReview(app.mode));
  if (!r?.job_id) { busy.value = ""; return; }
  pollJob(r.job_id, () => "复盘完成，日报已生成（见「绩效报告」）");
}

async function doEvolve() {
  if (busy.value) return;
  busy.value = "策略进化中…";
  const r = await tryReq(() => api.strategyEvolve(app.mode));
  if (!r?.job_id) { busy.value = ""; return; }
  pollJob(r.job_id, () => "进化完成（因子权重/策略池已更新）");
}

// ---------- 系统管理 ----------
async function kill(action: string) {
  if (action !== "reset" && !confirm(`确认执行 ${action}？该操作会立即改变系统交易状态。`)) return;
  const r = await tryReq(() => api.setKillswitch(app.mode, action), `总开关已 ${action}`);
  if (r) { ov.value = { ...ov.value, killswitch: r }; load(); }
}

async function runJob(name: string) {
  loading.value = true;
  const r = await tryReq(() => api.schedulerRun(name, app.mode));
  loading.value = false;
  if (r) {
    runResult.value = r;
    showResult.value = true;
    pushToast(`${name} ${r.ok ? "执行成功" : "执行失败"}`, r.ok ? "ok" : "err");
    load();
  }
}

async function saveSecret() {
  if (!secretEdit.value) return;
  const { key, value } = secretEdit.value;
  if (!value) { pushToast("值不能为空", "err"); return; }
  const r = await tryReq(() => api.setSecret(key, value), `${key} 已写入 config/.env`);
  if (r) { secretEdit.value = null; load(); }
}

// ---------- 调度任务展示 / 编辑 ----------
const JOB_KIND_LABEL: Record<string, string> = { cron: "定时", interval: "高频" };

function fmtNextRun(t?: string) {
  if (!t) return "-";
  const d = new Date(String(t).replace(" ", "T"));
  if (isNaN(d.getTime())) return t;
  const diffMin = Math.round((d.getTime() - Date.now()) / 60000);
  if (diffMin < 1) return `${t}（即将运行）`;
  if (diffMin < 60) return `${t}（${diffMin} 分钟后）`;
  if (diffMin < 60 * 24) return `${t}（${Math.round(diffMin / 60)} 小时后）`;
  return `${t}（${Math.round(diffMin / 1440)} 天后）`;
}

function fmtSeconds(s?: number) {
  if (!s || s <= 0) return "-";
  return s >= 60 ? `${Math.round(s / 60)} 分钟` : `${s} 秒`;
}

function openJobEdit(j: any) {
  const pad = (n: number) => String(n).padStart(2, "0");
  jobEdit.value = {
    ...j,
    editTime: `${pad(j.hour || 0)}:${pad(j.minute || 0)}`,
    editWeekday: j.day_of_week ?? 6,
    editSeconds: j.seconds ?? 3,
    editStart: j.start || "09:30",
    editEnd: j.end || "15:00",
  };
}

async function saveJobTime() {
  const j = jobEdit.value;
  if (!j) return;
  jobSaving.value = true;
  const body: any = { name: j.name };
  if (j.kind === "interval") {
    body.interval_seconds = Number(j.editSeconds);
    body.start = j.editStart;
    body.end = j.editEnd;
  } else {
    body.time = j.editTime;
    if (j.name === "evolve") body.day_of_week = Number(j.editWeekday);
  }
  const r = await tryReq(() => api.schedulerUpdateJob(body));
  jobSaving.value = false;
  if (r) {
    pushToast(`「${j.name}」调度已更新（${r.hint || "已生效"}）`, "ok");
    jobEdit.value = null;
    load();
  }
}

function killBadge(mode?: string) {
  if (mode === "NORMAL") return "ok";
  if (mode === "REDUCE_ONLY") return "warn";
  return "danger";
}
function fmtDate(s: string) {
  return s && s.length === 8 ? `${s.slice(4, 6)}-${s.slice(6, 8)}` : s;
}
const ACTION_LABEL: Record<string, string> = { buy: "买入", sell: "卖出", hold: "持有", watch: "观察" };
function actionClass(a: string) {
  if (a === "buy") return "ok";
  if (a === "sell") return "danger";
  if (a === "hold" || a === "watch") return "info";
  return "muted";
}
const posCount = computed(() => positions.value.filter((p: any) => Number(p.volume) > 0).length);
// 持仓概要用市值/浮盈补充信息量（后端字段 volume/market_value/unrealized_pnl）
const posValue = computed(() => positions.value.reduce((s: number, p: any) => s + Number(p.market_value || 0), 0));
const posPnl = computed(() => positions.value.reduce((s: number, p: any) => s + Number(p.unrealized_pnl || 0), 0));
function fmtWan(v: number) {
  return Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(2) + " 亿" : (v / 1e4).toFixed(1) + " 万";
}
function fmtSigned(v: number) { return (v > 0 ? "+" : "") + fmtWan(v); }

const MODE_LABEL: Record<string, string> = { paper: "纸面交易", live: "实盘交易", sim: "模拟数据" };
const KS_LABEL: Record<string, string> = { NORMAL: "正常", REDUCE_ONLY: "只减不加", FLATTEN: "强制清仓" };
const LEVEL_LABEL: Record<string, string> = { INFO: "提示", WARN: "警告", ERROR: "严重" };
function ksState(mode?: string) {
  if (mode === "NORMAL") return "st-ok";
  if (mode === "REDUCE_ONLY") return "st-warn";
  return "st-danger";
}
// 系统健康：异常时直接展示第一条失败检查项（此前只有“异常”徽章，看不到原因）
const failedChecks = computed(() => (health.value?.checks || []).filter((c: any) => !c.ok));
const healthIssue = computed(() => {
  const first = failedChecks.value[0];
  if (first) return `${first.name}：${first.message}`;
  const reasons = health.value?.degrade_reasons || [];
  if (reasons.length) return reasons.join("；");
  return health.value?.killswitch?.reason || "";
});

onMounted(load);
watch(() => app.mode, load);
</script>

<template>
  <div :class="{ loading }">
    <!-- 一键操作区 -->
    <div class="card quick">
      <div class="q-btns">
        <button class="btn primary" :disabled="!!busy" @click="doSelection">🔄 一键选股</button>
        <button class="btn primary" :disabled="!!busy" @click="doResearch">🧠 AI 研判</button>
        <button class="btn" :disabled="!!busy" @click="doPlan">📤 执行交易计划</button>
        <button class="btn" :disabled="!!busy" @click="doReview">📝 每日复盘</button>
        <button class="btn" :disabled="!!busy" @click="doEvolve">🧬 策略进化</button>
      </div>
      <span v-if="busy" class="tiny muted">{{ busy }}</span>
      <span v-else class="tiny muted">按「选股 → 研判 → 计划 → 复盘 → 进化」顺序使用；调度任务也会自动执行</span>
    </div>

    <!-- 关键状态：图标 + 状态色条 + 中文可读状态 -->
    <div class="grid cols-4 dash-stats">
      <div class="stat dash-stat">
        <span class="ds-icon">🧭</span>
        <div class="ds-body">
          <div class="label">运行模式</div>
          <div class="value">{{ MODE_LABEL[ov?.mode] || ov?.mode || "-" }}</div>
          <div class="tiny muted ds-sub">
            LLM 决策层
            <span class="badge sm" :class="ov?.llm_enabled ? 'ok' : 'muted'">
              {{ ov?.llm_enabled ? "已启用" : "未启用" }}
            </span>
          </div>
        </div>
      </div>
      <div class="stat dash-stat" :class="ksState(ov?.killswitch?.mode)">
        <span class="ds-icon">🛡️</span>
        <div class="ds-body">
          <div class="label">交易总开关</div>
          <div class="value sm">
            <span class="badge" :class="killBadge(ov?.killswitch?.mode)" :title="ov?.killswitch?.mode">
              {{ KS_LABEL[ov?.killswitch?.mode] || ov?.killswitch?.mode || "-" }}
            </span>
          </div>
          <div class="tiny muted ds-sub ellipsis-2">{{ ov?.killswitch?.reason || "运行正常，无降级原因" }}</div>
        </div>
      </div>
      <div class="stat dash-stat">
        <span class="ds-icon">💼</span>
        <div class="ds-body">
          <div class="label">持仓概要</div>
          <div class="value">{{ posCount }} <span class="tiny muted">只</span>
            <span v-if="posSource === 'broker'" class="badge info" style="font-size: 11px;">券商</span>
          </div>
          <div class="tiny muted ds-sub">
            市值 {{ fmtWan(posValue) }} · 浮盈
            <b :class="posPnl > 0 ? 'pnl-pos' : posPnl < 0 ? 'pnl-neg' : 'muted'">{{ fmtSigned(posPnl) }}</b>
          </div>
          <div v-if="posNote" class="tiny warn ds-sub" :title="posNote">{{ posNote }}</div>
        </div>
      </div>
      <div class="stat dash-stat" :class="health == null ? '' : health.healthy ? 'st-ok' : 'st-danger'">
        <span class="ds-icon">🩺</span>
        <div class="ds-body">
          <div class="label">系统健康</div>
          <div class="value sm">
            <span class="badge" :class="health == null ? 'muted' : health.healthy ? 'ok' : 'danger'">
              {{ health == null ? "-" : health.healthy ? "健康" : "异常" }}
            </span>
            <span v-if="health && !health.healthy" class="tiny muted">（{{ failedChecks.length }} 项未通过）</span>
          </div>
          <div class="tiny muted ds-sub ellipsis-2" :title="healthIssue || ''">
            {{ health == null ? "加载中…" : health.healthy ? (ov?.is_live ? "⚠️ 实盘连接中" : "模拟/纸面，不碰真金") : healthIssue }}
          </div>
        </div>
      </div>
    </div>

    <!-- 今日精选 + 最新报告 -->
    <div class="grid cols-2" style="margin-bottom: 16px">
      <div class="card">
        <h3>⭐ 最新精选
          <span class="sub">多 Agent 投票 · {{ finals.length }} 只</span>
          <div class="spacer"></div>
          <router-link class="link" to="/selection">选股研判 →</router-link>
        </h3>
        <div v-if="finals.length" class="mini-list">
          <router-link
            v-for="p in finals.slice(0, 3)" :key="p.symbol"
            class="mini-item" to="/selection"
          >
            <b class="pill">{{ p.symbol }}</b>
            <span class="badge sm" :class="actionClass(p.action)">{{ ACTION_LABEL[p.action] || p.action }}</span>
            <span class="tiny muted ellipsis">{{ (p.reason || "").slice(0, 60) }}</span>
          </router-link>
        </div>
        <div v-else class="muted" style="font-size:13px">
          尚无精选。点击顶部「一键选股」→「AI 研判」生成。
        </div>
      </div>

      <div class="card">
        <h3>📄 最新报告
          <div class="spacer"></div>
          <router-link class="link" to="/reports">绩效报告 →</router-link>
        </h3>
        <div v-if="reports.length" class="mini-list">
          <router-link
            v-for="r in reports" :key="r.name"
            class="mini-item" to="/reports"
          >
            <span class="badge sm" :class="r.kind === 'stage' ? 'ok' : r.kind === 'weekly' ? 'info' : ''">{{ r.kind_label }}</span>
            <span class="tiny">{{ fmtDate(r.date) }}<template v-if="r.date_end"> ~ {{ fmtDate(r.date_end) }}</template></span>
            <span class="tiny muted ellipsis">{{ r.name }}</span>
          </router-link>
        </div>
        <div v-else class="muted" style="font-size:13px">暂无报告，运行复盘/进化后自动生成。</div>
      </div>
    </div>

    <!-- Kill Switch -->
    <div class="card">
      <h3>🛡️ 交易总开关（Kill Switch）<span class="sub">失败安全：异常时自动降级为 REDUCE_ONLY</span></h3>
      <div class="row">
        <button class="btn warn" @click="kill('engage')">降级 REDUCE_ONLY</button>
        <button class="btn danger" @click="kill('flatten')">强制平仓 FLATTEN</button>
        <button class="btn ghost" @click="kill('reset')">恢复 NORMAL</button>
        <div class="spacer"></div>
        <button class="btn ghost" @click="load">刷新</button>
      </div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h3>🩺 健康体检</h3>
        <table>
          <thead><tr><th>检查项</th><th>级别</th><th>状态</th><th>说明</th></tr></thead>
          <tbody>
            <tr v-for="c in health?.checks || []" :key="c.name">
              <td>{{ c.name }}</td>
              <td class="muted tiny">{{ LEVEL_LABEL[c.level] || c.level }}</td>
              <td><span class="badge" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? "OK" : "FAIL" }}</span></td>
              <td class="tiny">{{ c.message }}</td>
            </tr>
            <tr v-if="!(health?.checks || []).length"><td colspan="4" class="muted">暂无数据</td></tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <h3>🕒 最近任务执行</h3>
        <table>
          <thead><tr><th>任务</th><th>状态</th><th>最近执行</th></tr></thead>
          <tbody>
            <tr v-for="j in health?.recent_jobs || []" :key="j.name">
              <td>{{ j.name }}</td>
              <td>
                <span class="badge" :class="j.status === 'ok' ? 'ok' : (j.status === '-' ? 'muted' : 'warn')">
                  {{ j.status }}
                </span>
              </td>
              <td class="tiny muted">{{ j.last_run }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h3>📅 调度任务 <span class="sub">自动按日程执行，点「编辑」可调整执行时刻，保存后立即生效</span></h3>
      <table>
        <thead>
          <tr>
            <th style="width:130px">任务</th>
            <th style="width:80px">类型</th>
            <th style="width:180px">执行计划</th>
            <th style="width:200px">下次执行</th>
            <th>用途说明</th>
            <th style="width:150px">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in sched?.jobs || []" :key="j.name">
            <td><b class="pill">{{ j.name }}</b></td>
            <td>
              <span class="badge sm" :class="j.kind === 'interval' ? 'info' : ''">
                {{ JOB_KIND_LABEL[j.kind] || j.kind }}
              </span>
            </td>
            <td>
              <span class="pill sched-label">{{ j.label }}</span>
              <span v-if="j.cron" class="tiny muted" :title="`原始 cron 表达式：${j.cron}`">
                {{ j.cron }}
              </span>
            </td>
            <td class="tiny">{{ fmtNextRun(j.next_run) }}</td>
            <td class="tiny muted">{{ j.description || "—" }}</td>
            <td>
              <button class="btn sm ghost" @click="openJobEdit(j)">编辑</button>
              <button class="btn sm" @click="runJob(j.name)">立即运行</button>
            </td>
          </tr>
          <tr v-if="!(sched?.jobs || []).length"><td colspan="6" class="muted">暂无数据</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>🔑 密钥管理 <span class="sub">只写入 config/.env，页面永不回显明文</span></h3>
      <table>
        <thead><tr><th>环境变量</th><th>状态</th><th>掩码</th><th style="width:90px">操作</th></tr></thead>
        <tbody>
          <tr v-for="s in secrets" :key="s.key">
            <td class="pill">{{ s.key }}</td>
            <td><span class="badge" :class="s.set ? 'ok' : 'muted'">{{ s.set ? "已配置" : "未配置" }}</span></td>
            <td class="muted tiny">{{ s.masked || "-" }}</td>
            <td><button class="btn sm ghost" @click="secretEdit = { key: s.key, value: '' }">设置</button></td>
          </tr>
        </tbody>
      </table>
    </div>

    <Modal v-if="secretEdit" :title="`设置密钥 ${secretEdit.key}`" @close="secretEdit = null">
      <div class="field">
        <label>值（写入 config/.env，不会出现在 YAML / 数据库）</label>
        <input v-model="secretEdit.value" type="password" placeholder="粘贴密钥/Webhook 地址" />
      </div>
      <template #actions>
        <button class="btn ghost" @click="secretEdit = null">取消</button>
        <button @click="saveSecret">保存</button>
      </template>
    </Modal>

    <Modal v-if="jobEdit" :title="`编辑调度任务 · ${jobEdit.name}`" @close="jobEdit = null">
      <p class="tiny muted" style="margin-top:0">{{ jobEdit.description }}</p>
      <template v-if="jobEdit.kind === 'interval'">
        <div class="field">
          <label>巡检间隔（秒，1~3600）</label>
          <input v-model.number="jobEdit.editSeconds" type="number" min="1" max="3600" />
        </div>
        <div class="field">
          <label>执行窗口开始（HH:MM）</label>
          <input v-model="jobEdit.editStart" type="time" />
        </div>
        <div class="field">
          <label>执行窗口结束（HH:MM）</label>
          <input v-model="jobEdit.editEnd" type="time" />
        </div>
      </template>
      <template v-else>
        <div v-if="jobEdit.name === 'evolve'" class="field">
          <label>每周执行日</label>
          <select v-model.number="jobEdit.editWeekday">
            <option v-for="(w, i) in ['周一', '周二', '周三', '周四', '周五', '周六', '周日']"
                    :key="w" :value="i">{{ w }}</option>
          </select>
        </div>
        <div class="field">
          <label>执行时间</label>
          <input v-model="jobEdit.editTime" type="time" />
        </div>
      </template>
      <p class="tiny muted">保存后写入 config/settings.yaml（scheduler.jobs），常驻调度器立即按新时刻生效。</p>
      <template #actions>
        <button class="btn ghost" @click="jobEdit = null">取消</button>
        <button :disabled="jobSaving" @click="saveJobTime">{{ jobSaving ? "保存中…" : "保存" }}</button>
      </template>
    </Modal>

    <Modal v-if="showResult" title="任务执行结果" @close="showResult = false">
      <pre class="json">{{ runResult?.rendered || JSON.stringify(runResult, null, 2) }}</pre>
      <template #actions>
        <button class="btn ghost" @click="showResult = false">关闭</button>
      </template>
    </Modal>
  </div>
</template>

<style scoped>
/* ---- 关键状态卡：图标 + 左侧状态色条，默认靛蓝、按状态切换绿/橙/红 ---- */
.dash-stats { margin-bottom: 16px; }
.dash-stat { display: flex; gap: 12px; align-items: flex-start; position: relative; --accent: var(--primary); }
.dash-stat::before {
  content: ""; position: absolute; left: 0; top: 12px; bottom: 12px; width: 3px;
  border-radius: 0 3px 3px 0; background: var(--accent); opacity: 0.85;
}
.dash-stat.st-ok { --accent: var(--ok); }
.dash-stat.st-warn { --accent: var(--warn); }
.dash-stat.st-danger { --accent: var(--danger); }
.ds-icon {
  width: 38px; height: 38px; flex: 0 0 38px; border-radius: 11px; font-size: 18px;
  display: inline-flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
.ds-body { flex: 1; min-width: 0; }
.ds-sub { margin-top: 5px; display: flex; align-items: center; gap: 5px; flex-wrap: wrap; line-height: 1.5; }
.ellipsis-2 { display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.pnl-pos { color: var(--ok); font-weight: 700; }
.pnl-neg { color: var(--danger); font-weight: 700; }

.quick {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.q-btns { display: flex; gap: 8px; flex-wrap: wrap; }
.btn.primary {
  background: var(--primary);
  border-color: var(--primary);
  color: #fff;
  font-weight: 700;
}
.btn:disabled { opacity: 0.55; cursor: not-allowed; }
.link { font-size: 13px; font-weight: 400; }
.mini-list { display: flex; flex-direction: column; gap: 8px; }
.mini-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg-elev);
  color: var(--text);
  text-decoration: none;
  transition: border-color 0.15s;
}
.mini-item:hover { border-color: var(--primary); }
.mini-item .ellipsis {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.badge.sm { font-size: 11px; padding: 1px 6px; }
.sched-label { font-weight: 600; margin-right: 6px; }
</style>
