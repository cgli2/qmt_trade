<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";

const app = useApp();
const loading = ref(false);
const pool = ref<any>(null);
const jobs = ref<any[]>([]);
const detail = ref<any>(null);
const evolveDate = ref("");
const reviewDate = ref("");
let timer: any = null;

// ---------------- 策略目录 ----------------
const catalog = ref<any>(null);
const stratDetail = ref<any>(null);
const management = ref<any>({ definitions: [], instances: [] });
const instance = ref<any>(null);
const paramsText = ref("{}");
const instanceName = ref("");
const instanceNote = ref("");

async function loadManagement() { management.value = await tryReq(() => api.strategyManagement(app.mode)) || { definitions: [], instances: [] }; }
function newInstance(s: any) { instance.value = { strategy_id: s.id }; instanceName.value = s.name; instanceNote.value = ""; paramsText.value = JSON.stringify(s.params || {}, null, 2); }
function editInstance(i: any) { instance.value = i; instanceName.value = i.name; instanceNote.value = i.draft?.note || ""; paramsText.value = JSON.stringify(i.draft?.params || i.versions?.find((v: any) => v.id === i.active_version)?.params || {}, null, 2); }
function formBody() { let params: any; try { params = JSON.parse(paramsText.value); } catch { pushToast("参数必须是合法 JSON 对象", "error"); return null; } if (!params || Array.isArray(params) || typeof params !== "object") { pushToast("参数必须是 JSON 对象", "error"); return null; } return { instance_id: instance.value?.id, strategy_id: instance.value?.strategy_id, name: instanceName.value, note: instanceNote.value, params }; }
async function saveDraft() { const body = formBody(); if (!body) return; const r = await tryReq(() => api.strategyDraft(body, app.mode), "草稿已保存"); if (r?.instance) { instance.value = r.instance; await loadManagement(); } }
async function publish() { const body = formBody(); if (!body) return; if (!body.instance_id) { await saveDraft(); body.instance_id = instance.value?.id; } const r = await tryReq(() => api.strategyPublish(body, app.mode), "版本已发布"); if (r?.instance) { instance.value = r.instance; await loadManagement(); } }
async function rollback(i: any, v: any) { const r = await tryReq(() => api.strategyRollback(i.id, v.id, app.mode), `已回滚至 ${v.id}`); if (r?.instance) { instance.value = r.instance; await loadManagement(); } }
async function toggle(i: any) { const r = await tryReq(() => api.strategyEnabled(i.id, !i.enabled, app.mode), i.enabled ? "实例已停用" : "实例已启用"); if (r) loadManagement(); }

async function loadCatalog() {
  catalog.value = await tryReq(() => api.strategyCatalog());
}
function openStrategy(s: any) { stratDetail.value = s; }

// ---------------- 策略池 ----------------
const rows = computed(() =>
  Object.entries(pool.value?.snapshot?.strategies || {}).map(([name, r]: any) => ({ name, ...r }))
);
const totalWeight = computed(() =>
  rows.value.reduce((s, r) => s + (Number(r.weight) || 0), 0)
);

async function load() {
  loading.value = true;
  pool.value = await tryReq(() => api.strategyPool(app.mode));
  loading.value = false;
  refreshJobs();
}

async function refreshJobs() {
  const r = await tryReq(() => api.jobs(15));
  jobs.value = (r?.jobs || r || []).filter((j: any) => ["evolve", "review"].includes(j.kind));
}

async function rebalance() {
  loading.value = true;
  const r = await tryReq(() => api.strategyRebalance(app.mode), "策略权重已重算并落盘");
  loading.value = false;
  if (r) { detail.value = { title: "调权结果", body: r.report || JSON.stringify(r, null, 2) }; load(); }
}

async function evolve() {
  const r = await tryReq(() => api.strategyEvolve(app.mode, evolveDate.value || undefined));
  if (r?.job_id) { pushToast(`进化任务已提交（${r.job_id}），后台运行中`, "ok"); startPoll(); }
}

async function review() {
  const r = await tryReq(() => api.strategyReview(app.mode, reviewDate.value || undefined));
  if (r?.job_id) { pushToast(`复盘任务已提交（${r.job_id}），后台运行中`, "ok"); startPoll(); }
}

function startPoll() {
  stopPoll();
  timer = setInterval(async () => {
    await refreshJobs();
    if (!jobs.value.some((j) => j.status === "running" || j.status === "pending")) {
      stopPoll();
      load();
    }
  }, 2000);
}
function stopPoll() { if (timer) { clearInterval(timer); timer = null; } }

function showJob(j: any) {
  const body = j.error
    ? j.error
    : (j.result?.render || j.result?.rendered || JSON.stringify(j.result, null, 2));
  detail.value = { title: `${j.kind} · ${j.id}`, body: body || "（无输出）" };
}

function statusBadge(s: string) {
  return s === "done" ? "ok" : s === "error" ? "danger" : s === "running" ? "warn" : "muted";
}
function poolBadge(s: string) {
  const v = String(s).toUpperCase();
  if (v.includes("ACTIVE")) return "ok";
  if (v.includes("PROBATION") || v.includes("OBSERV")) return "warn";
  if (v.includes("RETIRED") || v.includes("DISABLED")) return "danger";
  return "muted";
}
function ts(t: number) { return t ? new Date(t * 1000).toLocaleTimeString() : "-"; }

onMounted(() => { loadCatalog(); loadManagement(); load(); });
watch(() => app.mode, () => { load(); loadManagement(); });
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>⚙️ 策略实例管理 <span class="sub">定义只读；每个运行实例独立保存草稿、发布版本与启停状态</span><div class="spacer"></div><button class="btn sm ghost" @click="loadManagement">刷新</button></h3>
      <div class="grid cols-2">
        <div>
          <h4>已注册策略定义</h4>
          <table><thead><tr><th>策略</th><th>阶段</th><th></th></tr></thead><tbody>
            <tr v-for="s in management.definitions" :key="s.id"><td><b>{{ s.name }}</b><div class="tiny muted">{{ s.id }}</div></td><td class="pill tiny">{{ s.stage }}</td><td><button class="btn sm" @click="newInstance(s)">新建实例</button></td></tr>
            <tr v-if="!management.definitions?.length"><td colspan="3" class="muted">加载中…</td></tr>
          </tbody></table>
        </div>
        <div>
          <h4>运行实例</h4>
          <table><thead><tr><th>实例</th><th>版本</th><th>状态</th><th></th></tr></thead><tbody>
            <tr v-for="i in management.instances" :key="i.id"><td><b>{{ i.name }}</b><div class="tiny muted">{{ i.strategy_id }}</div></td><td>{{ i.active_version || "未发布" }}</td><td><span class="badge" :class="i.enabled ? 'ok' : 'muted'">{{ i.enabled ? "启用" : "停用" }}</span></td><td><button class="btn sm ghost" @click="editInstance(i)">编辑</button> <button class="btn sm ghost" @click="toggle(i)">{{ i.enabled ? "停用" : "启用" }}</button></td></tr>
            <tr v-if="!management.instances?.length"><td colspan="4" class="muted">暂无实例；请从左侧已注册策略创建</td></tr>
          </tbody></table>
        </div>
      </div>
    </div>

    <!-- ================= 策略目录：系统到底有哪些策略、规则是什么 ================= -->
    <div class="card">
      <h3>📚 策略目录 <span class="sub">自动选股与交易的完整决策链 —— 点击任意策略查看规则详情</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="loadCatalog">刷新</button>
      </h3>

      <h4 style="margin:12px 0 6px">🎯 选股策略（5 级漏斗：全市场 → 最终精选）</h4>
      <table>
        <thead><tr><th style="width:180px">阶段</th><th style="width:280px">策略</th><th>说明</th><th style="width:70px">详情</th></tr></thead>
        <tbody>
          <tr v-for="s in (catalog?.selection || [])" :key="s.id" class="clickable" @click="openStrategy(s)">
            <td class="pill tiny">{{ s.stage }}</td>
            <td><b>{{ s.name }}</b></td>
            <td class="tiny muted">{{ s.summary }}</td>
            <td><button class="btn sm ghost" @click.stop="openStrategy(s)">规则</button></td>
          </tr>
          <tr v-if="!catalog?.selection?.length"><td colspan="4" class="muted">加载中…</td></tr>
        </tbody>
      </table>

      <h4 style="margin:14px 0 6px">💼 交易策略（买入 → 持仓 → 报单 → 资金调权）</h4>
      <table>
        <thead><tr><th style="width:180px">阶段</th><th style="width:280px">策略</th><th>说明</th><th style="width:70px">详情</th></tr></thead>
        <tbody>
          <tr v-for="s in (catalog?.trading || [])" :key="s.id" class="clickable" @click="openStrategy(s)">
            <td class="pill tiny">{{ s.stage }}</td>
            <td><b>{{ s.name }}</b></td>
            <td class="tiny muted">{{ s.summary }}</td>
            <td><button class="btn sm ghost" @click.stop="openStrategy(s)">规则</button></td>
          </tr>
          <tr v-if="!catalog?.trading?.length"><td colspan="4" class="muted">加载中…</td></tr>
        </tbody>
      </table>
      <div class="tiny muted" style="margin-top:8px">
        规则中的阈值实时读取当前配置（settings.yaml），页面展示的与真实运行的永远一致；修改配置后点「刷新」即可看到最新规则。
      </div>
    </div>

    <!-- ================= 策略池：只管资金权重分配 ================= -->
    <div class="card">
      <h3>🧩 策略池 <span class="sub">达尔文式进化：表现差的策略降权→留校察看→退休</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="load">刷新</button>
        <button class="btn sm" style="margin-left:6px" @click="rebalance">立即调权</button>
      </h3>
      <div class="tiny muted" style="margin-bottom:8px">
        策略池只负责<b>资金权重分配与淘汰</b>（对应策略目录里的「策略池资金调权」）；每个策略具体的选股/交易逻辑请在上方策略目录中查看。
        新接入的策略先以 SHADOW 影子状态记账，跑满样本且得分为正才转正获得资金。
      </div>
      <table>
        <thead><tr><th>策略</th><th>状态</th><th>权重</th><th>样本数</th><th>连续失败</th><th>参数</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="r in rows" :key="r.name">
            <td><b>{{ r.name }}</b></td>
            <td><span class="badge" :class="poolBadge(r.status)">{{ r.status }}</span></td>
            <td>
              <div class="pill">{{ (Number(r.weight) * 100).toFixed(1) }}%</div>
              <div style="height:4px;background:var(--bg-2);border-radius:2px;margin-top:3px">
                <div :style="{ width: (Number(r.weight) * 100) + '%', height: '4px', background: 'var(--primary)', borderRadius: '2px' }"></div>
              </div>
            </td>
            <td class="pill">{{ r.n }}</td>
            <td class="pill">
              <span :class="r.strikes > 0 ? 'badge warn' : ''">{{ r.strikes }}</span>
            </td>
            <td class="tiny muted" style="max-width:220px">{{ JSON.stringify(r.params || {}) }}</td>
            <td class="tiny muted">{{ r.note || "-" }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7" class="muted">策略池为空</td></tr>
        </tbody>
      </table>
      <div class="tiny muted" style="margin-top:8px">
        权重合计 {{ (totalWeight * 100).toFixed(1) }}%（含现金档） · 总开关 {{ pool?.killswitch }}
      </div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h3>🧬 策略进化 <span class="sub">按近期绩效重算权重、淘汰劣质策略</span></h3>
        <div class="row">
          <div><label>基准日期（留空=今日）</label><input v-model="evolveDate" type="date" /></div>
          <div style="flex:0 0 auto"><label>&nbsp;</label><button @click="evolve">启动进化</button></div>
        </div>
        <div class="tiny muted" style="margin-top:8px">
          进化在后台线程运行，结果写回策略池并落库；不会阻塞页面，也不影响 CLI。
        </div>
      </div>

      <div class="card">
        <h3>📖 复盘总结 <span class="sub">当日成交归因 + 经验沉淀（可选 LLM 撰写）</span></h3>
        <div class="row">
          <div><label>复盘日期（留空=今日）</label><input v-model="reviewDate" type="date" /></div>
          <div style="flex:0 0 auto"><label>&nbsp;</label><button @click="review">生成复盘</button></div>
        </div>
        <div class="tiny muted" style="margin-top:8px">
          与 CLI `qmt review` 同一 JobRunner，复盘结论写入 lessons 供后续决策引用。
        </div>
      </div>
    </div>

    <div class="card">
      <h3>⏱️ 后台任务 <span class="sub">进化 / 复盘</span>
        <div class="spacer"></div><button class="btn sm ghost" @click="refreshJobs">刷新</button>
      </h3>
      <table>
        <thead><tr><th>任务 ID</th><th>类型</th><th>状态</th><th>提交</th><th>完成</th><th style="width:80px">结果</th></tr></thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td class="pill tiny">{{ j.id }}</td>
            <td>{{ j.kind }}</td>
            <td><span class="badge" :class="statusBadge(j.status)">{{ j.status }}</span></td>
            <td class="tiny muted">{{ ts(j.created) }}</td>
            <td class="tiny muted">{{ ts(j.finished) }}</td>
            <td><button class="btn sm ghost" :disabled="j.status === 'running' || j.status === 'pending'" @click="showJob(j)">查看</button></td>
          </tr>
          <tr v-if="!jobs.length"><td colspan="6" class="muted">暂无任务</td></tr>
        </tbody>
      </table>
    </div>

    <!-- ================= 策略详情弹窗 ================= -->
    <Modal v-if="instance" :title="`${instance.id ? '编辑' : '新建'}运行实例 · ${instance.strategy_id}`" @close="instance = null">
      <div class="tiny muted" style="margin-bottom:10px">此处编辑的是运行实例的独立草稿，不会修改策略默认配置。发布后才生成可回滚的版本。</div>
      <label>实例名称</label><input v-model="instanceName" placeholder="实例名称" />
      <label style="display:block;margin-top:10px">参数（JSON 对象）</label><textarea v-model="paramsText" rows="10" style="width:100%;font-family:monospace"></textarea>
      <label style="display:block;margin-top:10px">备注</label><input v-model="instanceNote" placeholder="本次配置说明" />
      <div v-if="instance.versions?.length" style="margin-top:14px"><h4>版本历史</h4><div v-for="v in instance.versions" :key="v.id" class="row" style="margin:5px 0"><span class="pill">{{ v.id }}</span><span class="tiny muted">{{ v.note || '无备注' }}</span><span class="spacer"></span><b v-if="v.id === instance.active_version" class="tiny">当前</b><button v-else class="btn sm ghost" @click="rollback(instance, v)">回滚</button></div></div>
      <template #actions><button class="btn ghost" @click="instance = null">关闭</button><button class="btn ghost" @click="saveDraft">保存草稿</button><button class="btn" @click="publish">发布版本</button></template>
    </Modal>

    <Modal v-if="stratDetail" :title="stratDetail.name" @close="stratDetail = null">
      <div class="tiny muted" style="margin-bottom:8px">
        <span class="badge">{{ stratDetail.stage }}</span>
        <span style="margin-left:8px">实现：{{ stratDetail.module }}</span>
      </div>
      <p style="margin:6px 0 12px">{{ stratDetail.summary }}</p>
      <h4 style="margin:0 0 6px">规则明细</h4>
      <table>
        <thead><tr><th style="width:130px">规则</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="r in stratDetail.rules" :key="r.name">
            <td><b>{{ r.name }}</b></td>
            <td class="tiny">{{ r.detail }}</td>
          </tr>
        </tbody>
      </table>
      <details style="margin-top:10px">
        <summary class="tiny muted" style="cursor:pointer">当前参数（取自 settings.yaml 实际值）</summary>
        <pre class="json">{{ JSON.stringify(stratDetail.params, null, 2) }}</pre>
      </details>
      <template #actions><button class="btn ghost" @click="stratDetail = null">关闭</button></template>
    </Modal>

    <Modal v-if="detail" :title="detail.title" @close="detail = null">
      <pre class="json">{{ detail.body }}</pre>
      <template #actions><button class="btn ghost" @click="detail = null">关闭</button></template>
    </Modal>
  </div>
</template>

<style scoped>
/* 策略目录行：整行可点击打开规则详情 */
tr.clickable { cursor: pointer; }
tr.clickable:hover { background: var(--bg-2); }
</style>
