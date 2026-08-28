<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import api from "@/api";
import { pushToast, tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";

const loading = ref(false);
const cfg = ref<any>(null);

const provDlg = ref<any>(null);
const modelDlg = ref<any>(null);
const sceneDlg = ref<any>(null);
const testDlg = ref<any>(null);
const testResult = ref<any>(null);

const sceneList = computed(() =>
  Object.entries(cfg.value?.scenes || {}).map(([id, v]: any) => ({ id, ...v }))
);
const modelIds = computed<string[]>(() => (cfg.value?.models || []).map((m: any) => m.id));
const healthOf = (id: string) =>
  (cfg.value?.health || []).find((h: any) => h.model === id);

async function load() {
  loading.value = true;
  cfg.value = await tryReq(() => api.llmConfig());
  loading.value = false;
}

function apply(r: any, msg: string) {
  if (r) { cfg.value = r; pushToast(msg, "ok"); }
}

async function toggleEnabled() {
  const r = await tryReq(() => api.llmSetEnabled(!cfg.value?.enabled));
  apply(r, `LLM 已${cfg.value?.enabled ? "启用" : "停用"}`);
}

// ------- provider
function newProvider() {
  provDlg.value = { id: "", type: "openai_like", base_url: "", api_key_env: "",
                    timeout: 60, max_retries: 2, extra_headers: {} };
}
function editProvider(p: any) { provDlg.value = JSON.parse(JSON.stringify(p)); }
async function saveProvider() {
  const p = provDlg.value;
  if (!p.id) { pushToast("provider id 必填", "err"); return; }
  const r = await tryReq(() => api.llmAddProvider({
    ...p, timeout: Number(p.timeout), max_retries: Number(p.max_retries),
  }));
  apply(r, `provider ${p.id} 已保存`);
  if (r) provDlg.value = null;
}
async function delProvider(id: string) {
  if (!confirm(`删除 provider ${id}？依赖它的模型将失效。`)) return;
  apply(await tryReq(() => api.llmDelProvider(id)), `provider ${id} 已删除`);
}

// ------- model
function newModel() {
  modelDlg.value = { id: "", provider: cfg.value?.providers?.[0]?.id || "", name: "",
                     capabilities: "", context_window: 32000, price_in: 0, price_out: 0 };
}
function editModel(m: any) {
  modelDlg.value = {
    ...m,
    capabilities: (m.capabilities || []).join(","),
    price_in: m.price_per_1k_tokens?.input ?? 0,
    price_out: m.price_per_1k_tokens?.output ?? 0,
  };
}
async function saveModel() {
  const m = modelDlg.value;
  if (!m.id || !m.provider) { pushToast("模型 id / provider 必填", "err"); return; }
  const body = {
    id: m.id, provider: m.provider, name: m.name || m.id,
    capabilities: String(m.capabilities || "").split(",").map((s: string) => s.trim()).filter(Boolean),
    context_window: Number(m.context_window) || 32000,
    price_per_1k_tokens: { input: Number(m.price_in) || 0, output: Number(m.price_out) || 0 },
  };
  const r = await tryReq(() => api.llmAddModel(body));
  apply(r, `模型 ${m.id} 已保存`);
  if (r) modelDlg.value = null;
}
async function delModel(id: string) {
  if (!confirm(`删除模型 ${id}？`)) return;
  apply(await tryReq(() => api.llmDelModel(id)), `模型 ${id} 已删除`);
}

// ------- scene
function newScene() { sceneDlg.value = { id: "", prefer: "", candidates: "", description: "" }; }
function editScene(s: any) {
  sceneDlg.value = { id: s.id, prefer: (s.prefer || []).join(","),
                     candidates: (s.candidates || []).join(","), description: s.description || "" };
}
async function saveScene() {
  const s = sceneDlg.value;
  if (!s.id) { pushToast("场景 id 必填", "err"); return; }
  const body = {
    id: s.id,
    prefer: String(s.prefer || "").split(",").map((x: string) => x.trim()).filter(Boolean),
    candidates: String(s.candidates || "").split(",").map((x: string) => x.trim()).filter(Boolean),
    description: s.description || "",
  };
  const r = await tryReq(() => api.llmAddScene(body));
  apply(r, `场景 ${s.id} 已保存`);
  if (r) sceneDlg.value = null;
}
async function delScene(id: string) {
  if (!confirm(`删除场景 ${id}？`)) return;
  apply(await tryReq(() => api.llmDelScene(id)), `场景 ${id} 已删除`);
}

// ------- selection
const sel = ref<any>(null);
function editSelection() { sel.value = { ...(cfg.value?.selection || {}) }; }
async function saveSelection() {
  const s = sel.value;
  const sum = Number(s.capability_weight) + Number(s.health_weight) + Number(s.cost_weight);
  if (Math.abs(sum - 1) > 0.001 && !confirm(`三项权重之和为 ${sum.toFixed(2)}（非 1），仍要保存？`)) return;
  const r = await tryReq(() => api.llmSetSelection({
    strategy: s.strategy,
    capability_weight: Number(s.capability_weight),
    health_weight: Number(s.health_weight),
    cost_weight: Number(s.cost_weight),
    fallback_enabled: !!s.fallback_enabled,
  }));
  apply(r, "选模策略已保存");
  if (r) sel.value = null;
}

// ------- test
function openTest(model?: string) {
  testResult.value = null;
  testDlg.value = { prompt: "请用一句话介绍你自己。", model: model || "", scene: "" };
}
async function runTest() {
  loading.value = true;
  const t = testDlg.value;
  const r = await tryReq(() => api.llmTest(t.prompt, t.model || undefined, t.scene || undefined));
  loading.value = false;
  if (r) { testResult.value = r; if (r.ok) load(); }
}

onMounted(load);
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>
        🤖 LLM 总控 <span class="sub">配置落盘 config/llm.yaml，与 CLI 共用同一份</span>
      </h3>
      <div class="row">
        <div style="flex:0 0 auto">
          <span class="badge" :class="cfg?.enabled ? 'ok' : 'muted'">
            {{ cfg?.enabled ? "已启用" : "已停用（降级纯因子）" }}
          </span>
        </div>
        <div style="flex:0 0 auto" class="tiny muted">
          默认模型 <b>{{ cfg?.default_model }}</b> · 缓存 {{ cfg?.cache_enabled ? "开" : "关" }} ·
          预算 日 {{ cfg?.budget?.daily_cny }} 元 / 月 {{ cfg?.budget?.monthly_cny }} 元
          {{ cfg?.budget?.hard_stop ? "（超支硬熔断）" : "" }}
        </div>
        <div class="spacer"></div>
        <button class="btn" style="flex:0 0 auto" @click="toggleEnabled">
          {{ cfg?.enabled ? "停用 LLM" : "启用 LLM" }}
        </button>
        <button class="btn ghost" style="flex:0 0 auto" @click="openTest()">测试调用</button>
        <button class="btn ghost" style="flex:0 0 auto" @click="load">刷新</button>
      </div>
      <div v-if="cfg?.manager_error" class="tiny" style="margin-top:8px;color:var(--danger)">
        管理层装配失败：{{ cfg.manager_error }}
      </div>
    </div>

    <div class="card">
      <h3>🏭 平台 Provider <span class="sub">API Key 只存环境变量名，值在「总览-密钥管理」写入</span>
        <div class="spacer"></div>
        <button class="btn sm" @click="newProvider">+ 新增</button>
      </h3>
      <table>
        <thead><tr><th>ID</th><th>类型</th><th>Base URL</th><th>Key 环境变量</th><th>超时/重试</th><th style="width:130px">操作</th></tr></thead>
        <tbody>
          <tr v-for="p in cfg?.providers || []" :key="p.id">
            <td><b>{{ p.id }}</b></td>
            <td><span class="badge muted">{{ p.type }}</span></td>
            <td class="tiny">{{ p.base_url || "-" }}</td>
            <td class="tiny pill">{{ p.api_key_env || "-" }}</td>
            <td class="tiny muted">{{ p.timeout }}s / {{ p.max_retries }}</td>
            <td>
              <button class="btn sm ghost" @click="editProvider(p)">编辑</button>
              <button class="btn sm danger" style="margin-left:6px" @click="delProvider(p.id)">删除</button>
            </td>
          </tr>
          <tr v-if="!(cfg?.providers || []).length"><td colspan="6" class="muted">暂无 provider</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>🧠 模型 <span class="sub">能力标签用于场景选模；健康度实时熔断</span>
        <div class="spacer"></div>
        <button class="btn sm" @click="newModel">+ 新增</button>
      </h3>
      <table>
        <thead>
          <tr><th>ID</th><th>Provider</th><th>模型名</th><th>能力</th><th>上下文</th>
            <th>价格(入/出 每千 tokens)</th><th>健康</th><th style="width:170px">操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="m in cfg?.models || []" :key="m.id">
            <td><b>{{ m.id }}</b></td>
            <td class="tiny">{{ m.provider }}</td>
            <td class="tiny">{{ m.name }}</td>
            <td>
              <span v-for="c in m.capabilities || []" :key="c" class="badge info" style="margin-right:4px">{{ c }}</span>
            </td>
            <td class="tiny pill">{{ m.context_window }}</td>
            <td class="tiny pill">
              {{ m.price_per_1k_tokens?.input ?? 0 }} / {{ m.price_per_1k_tokens?.output ?? 0 }}
            </td>
            <td>
              <template v-if="healthOf(m.id)">
                <span class="badge" :class="healthOf(m.id).circuit_open ? 'danger' : 'ok'">
                  {{ healthOf(m.id).circuit_open ? "熔断" : (healthOf(m.id).success_rate * 100).toFixed(0) + "%" }}
                </span>
                <span class="tiny muted"> {{ healthOf(m.id).success }}/{{ healthOf(m.id).total }}</span>
              </template>
              <span v-else class="badge muted">未调用</span>
            </td>
            <td>
              <button class="btn sm ghost" @click="editModel(m)">编辑</button>
              <button class="btn sm ghost" style="margin-left:4px" @click="openTest(m.id)">测试</button>
              <button class="btn sm danger" style="margin-left:4px" @click="delModel(m.id)">删</button>
            </td>
          </tr>
          <tr v-if="!(cfg?.models || []).length"><td colspan="8" class="muted">暂无模型</td></tr>
        </tbody>
      </table>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h3>🎯 场景路由 <span class="sub">按任务场景智能选择最佳模型</span>
          <div class="spacer"></div>
          <button class="btn sm" @click="newScene">+ 新增</button>
        </h3>
        <table>
          <thead><tr><th>场景</th><th>优先</th><th>候选</th><th style="width:110px">操作</th></tr></thead>
          <tbody>
            <tr v-for="s in sceneList" :key="s.id">
              <td><b>{{ s.id }}</b><div class="tiny muted">{{ s.description }}</div></td>
              <td class="tiny">{{ (s.prefer || []).join(", ") || "-" }}</td>
              <td class="tiny muted">{{ (s.candidates || []).join(", ") || "-" }}</td>
              <td>
                <button class="btn sm ghost" @click="editScene(s)">编辑</button>
                <button class="btn sm danger" style="margin-left:4px" @click="delScene(s.id)">删</button>
              </td>
            </tr>
            <tr v-if="!sceneList.length"><td colspan="4" class="muted">暂无场景</td></tr>
          </tbody>
        </table>
      </div>

      <div class="card">
        <h3>⚖️ 选模策略 <span class="sub">能力 / 健康 / 成本加权打分</span>
          <div class="spacer"></div>
          <button class="btn sm ghost" @click="editSelection">编辑</button>
        </h3>
        <table>
          <tbody>
            <tr><th style="width:160px">策略</th><td>{{ cfg?.selection?.strategy }}</td></tr>
            <tr><th>能力权重</th><td class="pill">{{ cfg?.selection?.capability_weight }}</td></tr>
            <tr><th>健康权重</th><td class="pill">{{ cfg?.selection?.health_weight }}</td></tr>
            <tr><th>成本权重</th><td class="pill">{{ cfg?.selection?.cost_weight }}</td></tr>
            <tr><th>失败回退</th>
              <td><span class="badge" :class="cfg?.selection?.fallback_enabled ? 'ok' : 'muted'">
                {{ cfg?.selection?.fallback_enabled ? "开启" : "关闭" }}</span></td></tr>
          </tbody>
        </table>
        <div class="tiny muted" style="margin-top:10px">
          成本熔断触发或全部模型不可用时，系统自动降级为纯因子确定性闭环（P5）。
        </div>
      </div>
    </div>

    <!-- provider dialog -->
    <Modal v-if="provDlg" :title="`Provider - ${provDlg.id || '新增'}`" @close="provDlg = null">
      <div class="row">
        <div class="field"><label>ID *</label><input v-model="provDlg.id" placeholder="deepseek" /></div>
        <div class="field"><label>类型</label>
          <select v-model="provDlg.type"><option value="openai_like">openai_like</option><option value="mock">mock</option></select>
        </div>
      </div>
      <div class="field"><label>Base URL</label><input v-model="provDlg.base_url" placeholder="https://api.deepseek.com/v1" /></div>
      <div class="field"><label>API Key 环境变量名（只存名字，不存值）</label>
        <input v-model="provDlg.api_key_env" placeholder="DEEPSEEK_API_KEY" /></div>
      <div class="row">
        <div class="field"><label>超时(秒)</label><input v-model="provDlg.timeout" type="number" /></div>
        <div class="field"><label>最大重试</label><input v-model="provDlg.max_retries" type="number" /></div>
      </div>
      <template #actions>
        <button class="btn ghost" @click="provDlg = null">取消</button>
        <button @click="saveProvider">保存</button>
      </template>
    </Modal>

    <!-- model dialog -->
    <Modal v-if="modelDlg" :title="`模型 - ${modelDlg.id || '新增'}`" @close="modelDlg = null">
      <div class="row">
        <div class="field"><label>ID *</label><input v-model="modelDlg.id" placeholder="deepseek-chat" /></div>
        <div class="field"><label>Provider *</label>
          <select v-model="modelDlg.provider">
            <option v-for="p in cfg?.providers || []" :key="p.id" :value="p.id">{{ p.id }}</option>
          </select>
        </div>
      </div>
      <div class="field"><label>调用模型名</label><input v-model="modelDlg.name" placeholder="deepseek-chat" /></div>
      <div class="field"><label>能力标签（逗号分隔：reasoning,fast,cheap,long_context,json）</label>
        <input v-model="modelDlg.capabilities" placeholder="reasoning,json" /></div>
      <div class="row">
        <div class="field"><label>上下文窗口</label><input v-model="modelDlg.context_window" type="number" /></div>
        <div class="field"><label>输入价/千tokens</label><input v-model="modelDlg.price_in" type="number" step="0.0001" /></div>
        <div class="field"><label>输出价/千tokens</label><input v-model="modelDlg.price_out" type="number" step="0.0001" /></div>
      </div>
      <template #actions>
        <button class="btn ghost" @click="modelDlg = null">取消</button>
        <button @click="saveModel">保存</button>
      </template>
    </Modal>

    <!-- scene dialog -->
    <Modal v-if="sceneDlg" :title="`场景 - ${sceneDlg.id || '新增'}`" @close="sceneDlg = null">
      <div class="field"><label>场景 ID *（如 analyst / research / review / news）</label>
        <input v-model="sceneDlg.id" /></div>
      <div class="field"><label>优先模型（逗号分隔）</label><input v-model="sceneDlg.prefer" />
        <div class="tiny muted">可用：{{ modelIds.join(", ") || "无" }}</div></div>
      <div class="field"><label>候选模型（逗号分隔，用于回退）</label><input v-model="sceneDlg.candidates" /></div>
      <div class="field"><label>说明</label><input v-model="sceneDlg.description" /></div>
      <template #actions>
        <button class="btn ghost" @click="sceneDlg = null">取消</button>
        <button @click="saveScene">保存</button>
      </template>
    </Modal>

    <!-- selection dialog -->
    <Modal v-if="sel" title="选模策略" @close="sel = null">
      <div class="field"><label>策略</label>
        <select v-model="sel.strategy">
          <option value="weighted">weighted（加权打分）</option>
          <option value="prefer_first">prefer_first（优先列表顺序）</option>
        </select>
      </div>
      <div class="row">
        <div class="field"><label>能力权重</label><input v-model="sel.capability_weight" type="number" step="0.05" /></div>
        <div class="field"><label>健康权重</label><input v-model="sel.health_weight" type="number" step="0.05" /></div>
        <div class="field"><label>成本权重</label><input v-model="sel.cost_weight" type="number" step="0.05" /></div>
      </div>
      <div class="field"><label><input type="checkbox" v-model="sel.fallback_enabled" style="width:auto" />
        失败自动回退到候选模型</label></div>
      <template #actions>
        <button class="btn ghost" @click="sel = null">取消</button>
        <button @click="saveSelection">保存</button>
      </template>
    </Modal>

    <!-- test dialog -->
    <Modal v-if="testDlg" title="测试 LLM 调用" @close="testDlg = null">
      <div class="row">
        <div class="field"><label>指定模型（留空=按场景/默认选模）</label>
          <select v-model="testDlg.model">
            <option value="">（自动）</option>
            <option v-for="id in modelIds" :key="id" :value="id">{{ id }}</option>
          </select>
        </div>
        <div class="field"><label>场景（可选）</label>
          <select v-model="testDlg.scene">
            <option value="">（不指定）</option>
            <option v-for="s in sceneList" :key="s.id" :value="s.id">{{ s.id }}</option>
          </select>
        </div>
      </div>
      <div class="field"><label>Prompt</label><textarea v-model="testDlg.prompt" rows="3"></textarea></div>
      <div v-if="testResult" style="margin-top:10px">
        <span class="badge" :class="testResult.ok ? 'ok' : 'danger'">{{ testResult.ok ? "调用成功" : "调用失败" }}</span>
        <span v-if="testResult.ok" class="tiny muted" style="margin-left:8px">
          {{ testResult.model }} · {{ testResult.latency_ms }}ms · ¥{{ testResult.cost_cny }}
          · {{ testResult.cached ? "命中缓存" : "实时调用" }}
        </span>
        <pre class="json" style="margin-top:8px">{{ testResult.ok ? testResult.content : testResult.error }}</pre>
      </div>
      <template #actions>
        <button class="btn ghost" @click="testDlg = null">关闭</button>
        <button @click="runTest">发送</button>
      </template>
    </Modal>
  </div>
</template>
