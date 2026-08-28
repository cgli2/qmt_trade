<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";
import { hintOf } from "@/configHints";

const app = useApp();
const loading = ref(false);
const data = ref<any>(null);
const form = ref<Record<string, any>>({});
const origin = ref<Record<string, any>>({});

const GATE_DESC: Record<string, string> = {
  gate1: "Gate1 组合层 · 持仓数 / 集中度 / 行业暴露 / 总仓位上限",
  gate2: "Gate2 个股层 · 单票权重 / 流动性 / 涨跌停 / 停牌与 ST 过滤",
  gate3: "Gate3 订单层 · 单笔金额 / 冲击成本 / 频次 / 追单限制",
};

function flat(prefix: string, obj: any, out: Record<string, any>) {
  for (const [k, v] of Object.entries(obj || {})) {
    const p = `${prefix}.${k}`;
    if (v && typeof v === "object" && !Array.isArray(v)) flat(p, v, out);
    else out[p] = Array.isArray(v) ? JSON.stringify(v) : v;
  }
}

async function load() {
  loading.value = true;
  data.value = await tryReq(() => api.riskGates(app.mode));
  const f: Record<string, any> = {};
  for (const g of ["gate1", "gate2", "gate3"]) flat(`risk.${g}`, data.value?.[g], f);
  form.value = f;
  origin.value = { ...f };
  loading.value = false;
}

const groups = computed(() => {
  const res: Record<string, string[]> = { gate1: [], gate2: [], gate3: [] };
  for (const k of Object.keys(form.value)) {
    const g = k.split(".")[1];
    if (res[g]) res[g].push(k);
  }
  return res;
});
const dirty = computed(() =>
  Object.keys(form.value).filter((k) => String(form.value[k]) !== String(origin.value[k]))
);

async function save() {
  if (!dirty.value.length) { pushToast("没有改动", "info"); return; }
  const body: Record<string, any> = {};
  for (const k of dirty.value) {
    let v = form.value[k];
    const ov = origin.value[k];
    if (typeof ov === "number") v = Number(v);
    else if (typeof ov === "boolean") v = !!v;
    else if (typeof ov === "string" && ov.trim().startsWith("[")) {
      try { v = JSON.parse(String(v)); } catch { pushToast(`${k} 不是合法 JSON 数组`, "err"); return; }
    }
    body[k] = v;
  }
  const r = await tryReq(() => api.riskSetGates(body, app.mode), `已保存 ${dirty.value.length} 项风控阈值`);
  if (r) load();
}

async function kill(action: string) {
  if (action !== "reset" && !confirm(`确认 ${action}？将立即改变交易权限。`)) return;
  const r = await tryReq(() => api.setKillswitch(app.mode, action), `总开关已 ${action}`);
  if (r) load();
}

function kindOf(k: string) {
  const ov = origin.value[k];
  if (typeof ov === "boolean") return "bool";
  if (typeof ov === "number") return "number";
  return "text";
}
function short(k: string) { return k.split(".").slice(2).join("."); }

function killBadge(m?: string) {
  if (m === "NORMAL") return "ok";
  if (m === "REDUCE_ONLY") return "warn";
  return "danger";
}

onMounted(load);
watch(() => app.mode, load);
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>🛡️ 交易总开关 <span class="sub">三态：NORMAL / REDUCE_ONLY（只减不加） / FLATTEN（强制清仓）</span></h3>
      <div class="row">
        <div style="flex:0 0 auto">
          <span class="badge" :class="killBadge(data?.kill_mode)" style="font-size:13px">{{ data?.kill_mode || "-" }}</span>
          <span class="tiny muted" style="margin-left:10px">{{ data?.kill_reason || "无降级原因" }}</span>
        </div>
        <div class="spacer"></div>
        <button class="btn warn" style="flex:0 0 auto" @click="kill('engage')">降级 REDUCE_ONLY</button>
        <button class="btn danger" style="flex:0 0 auto" @click="kill('flatten')">FLATTEN</button>
        <button class="btn ghost" style="flex:0 0 auto" @click="kill('reset')">恢复 NORMAL</button>
      </div>
    </div>

    <div class="card">
      <h3>三道风控闸门 <span class="sub">规则先行：任一 Gate 拒绝即拦截，LLM 无权覆盖</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="load">刷新</button>
        <button class="btn sm" style="margin-left:6px" @click="save">保存（{{ dirty.length }}）</button>
      </h3>
    </div>

    <div class="card" v-for="g in ['gate1', 'gate2', 'gate3']" :key="g">
      <h3>{{ GATE_DESC[g] }}</h3>
      <table>
        <thead><tr><th style="width:30%">参数</th><th>值</th><th style="width:34%">说明</th></tr></thead>
        <tbody>
          <tr v-for="k in groups[g]" :key="k">
            <td>
              <span class="pill" :title="hintOf(k)">{{ short(k) }}</span>
              <span v-if="String(form[k]) !== String(origin[k])" class="badge warn" style="margin-left:6px">改</span>
            </td>
            <td>
              <input v-if="kindOf(k) === 'bool'" type="checkbox" v-model="form[k]" style="width:auto" />
              <input v-else-if="kindOf(k) === 'number'" type="number" step="any" v-model.number="form[k]" />
              <input v-else v-model="form[k]" />
            </td>
            <td class="tiny muted" style="line-height:1.5">{{ hintOf(k) }}</td>
          </tr>
          <tr v-if="!groups[g].length"><td colspan="3" class="muted">未配置该闸门参数</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
