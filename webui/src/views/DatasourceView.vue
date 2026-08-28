<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";

const app = useApp();
const loading = ref(false);
const data = ref<any>(null);
const editing = ref<Record<string, string>>({});

async function load() {
  loading.value = true;
  data.value = await tryReq(() => api.datasource(app.mode));
  editing.value = {};
  for (const [k, v] of Object.entries(data.value?.priority || {})) {
    editing.value[k] = (v as string[]).join(", ");
  }
  loading.value = false;
}

async function save() {
  const priority: Record<string, string[]> = {};
  for (const [k, v] of Object.entries(editing.value)) {
    priority[k] = String(v).split(",").map((s) => s.trim()).filter(Boolean);
  }
  if (!Object.keys(priority).length) { pushToast("没有可保存的优先级", "err"); return; }
  const r = await tryReq(() => api.setDatasourcePriority(priority, app.mode), "数据源优先级已落盘 settings.yaml");
  if (r) load();
}

function hb(h: any) {
  if (h.healthy === false || h.state === "open") return "danger";
  if (h.state === "half_open") return "warn";
  return "ok";
}

onMounted(load);
watch(() => app.mode, load);
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>🔌 数据源优先级 <span class="sub">按数据类型分层降级：前一个失败自动切下一个</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="load">刷新</button>
        <button class="btn sm" style="margin-left:6px" @click="save">保存</button>
      </h3>
      <table>
        <thead><tr><th style="width:180px">数据类型</th><th>优先级顺序（逗号分隔，从左到右依次尝试）</th></tr></thead>
        <tbody>
          <tr v-for="(_v, k) in editing" :key="k">
            <td><b>{{ k }}</b></td>
            <td><input v-model="editing[k]" placeholder="qmt, akshare, tushare, mock" /></td>
          </tr>
          <tr v-if="!Object.keys(editing).length"><td colspan="2" class="muted">未配置 datahub.priority</td></tr>
        </tbody>
      </table>
      <div class="tiny muted" style="margin-top:10px">
        sim 模式固定走 mock 源（可复现，P6）；paper/live 会按此顺序真实取数。
      </div>
    </div>

    <div class="grid cols-2">
      <div class="card">
        <h3>⚡ 熔断配置 <span class="sub">datahub.circuit_breaker</span></h3>
        <table>
          <tbody>
            <tr v-for="(v, k) in data?.circuit_breaker || {}" :key="k">
              <th style="width:200px">{{ k }}</th>
              <td class="pill">{{ v }}</td>
            </tr>
            <tr v-if="!Object.keys(data?.circuit_breaker || {}).length">
              <td colspan="2" class="muted">未配置</td>
            </tr>
          </tbody>
        </table>
        <div class="tiny muted" style="margin-top:8px">
          连续失败达阈值即打开熔断，冷却后半开试探；全部源熔断时取数抛错，触发 REDUCE_ONLY 失败安全（P4）。
        </div>
      </div>

      <div class="card">
        <h3>💚 运行时健康度</h3>
        <table>
          <thead><tr><th>数据源</th><th>状态</th><th>详情</th></tr></thead>
          <tbody>
            <tr v-for="(h, i) in data?.health || []" :key="i">
              <td><b>{{ h.name || h.source || "-" }}</b></td>
              <td><span class="badge" :class="hb(h)">{{ h.state || (h.healthy === false ? "FAIL" : "OK") }}</span></td>
              <td class="tiny muted">
                {{ h.error || h.last_error || "" }}
                <template v-if="h.failures !== undefined">失败 {{ h.failures }} 次</template>
              </td>
            </tr>
            <tr v-if="!(data?.health || []).length"><td colspan="3" class="muted">暂无健康数据（尚未取数）</td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <div class="card">
      <h3>📄 原始配置</h3>
      <pre class="json">{{ JSON.stringify(data, null, 2) }}</pre>
    </div>
  </div>
</template>
