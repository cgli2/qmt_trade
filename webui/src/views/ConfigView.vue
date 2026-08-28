<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import api from "@/api";
import { pushToast, tryReq } from "@/toast";
import { hintOf, SECTION_TITLES } from "@/configHints";

type Leaf = { path: string; section: string; kind: string; value: any; origin: any };

const loading = ref(false);
const raw = ref<any>(null);
const leaves = ref<Leaf[]>([]);
const activeSection = ref<string>("");
const keyword = ref("");

function kindOf(v: any): string {
  if (typeof v === "boolean") return "bool";
  if (typeof v === "number") return "number";
  if (Array.isArray(v) || (v && typeof v === "object")) return "json";
  return "text";
}

function flatten(obj: any, prefix: string, section: string, out: Leaf[]) {
  for (const [k, v] of Object.entries(obj || {})) {
    const path = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      flatten(v, path, section, out);
    } else {
      const kind = kindOf(v);
      out.push({
        path, section, kind,
        value: kind === "json" ? JSON.stringify(v) : v,
        origin: kind === "json" ? JSON.stringify(v) : v,
      });
    }
  }
}

const sections = computed(() => Object.keys(raw.value || {}));
const visible = computed(() => {
  const kw = keyword.value.trim().toLowerCase();
  return leaves.value.filter((l) => {
    if (activeSection.value && l.section !== activeSection.value) return false;
    if (kw && !l.path.toLowerCase().includes(kw)) return false;
    return true;
  });
});
const dirty = computed(() => leaves.value.filter((l) => String(l.value) !== String(l.origin)));

async function load() {
  loading.value = true;
  raw.value = await tryReq(() => api.configAll());
  const out: Leaf[] = [];
  for (const [sec, val] of Object.entries(raw.value || {})) {
    if (val && typeof val === "object" && !Array.isArray(val)) flatten(val, sec, sec, out);
    else {
      const kind = kindOf(val);
      out.push({ path: sec, section: sec, kind,
                 value: kind === "json" ? JSON.stringify(val) : val,
                 origin: kind === "json" ? JSON.stringify(val) : val });
    }
  }
  leaves.value = out;
  if (!activeSection.value && sections.value.length) activeSection.value = sections.value[0];
  loading.value = false;
}

function coerce(l: Leaf): any {
  if (l.kind === "bool") return !!l.value;
  if (l.kind === "number") return Number(l.value);
  if (l.kind === "json") {
    try { return JSON.parse(String(l.value)); }
    catch { throw new Error(`${l.path} 不是合法 JSON`); }
  }
  return l.value;
}

async function save() {
  if (!dirty.value.length) { pushToast("没有改动", "info"); return; }
  let patches;
  try {
    patches = dirty.value.map((l) => ({ path: l.path, value: coerce(l) }));
  } catch (e: any) {
    pushToast(e.message, "err");
    return;
  }
  const r = await tryReq(() => api.configPatch(patches), `已保存 ${patches.length} 项到 settings.yaml`);
  if (r) load();
}

function reset() { leaves.value.forEach((l) => (l.value = l.origin)); }

onMounted(load);
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>⚙️ 参数配置 <span class="sub">直接编辑 config/settings.yaml，保存前自动备份 .bak；CLI 立刻生效</span></h3>
      <div class="row">
        <div style="flex:2">
          <label>搜索配置项</label>
          <input v-model="keyword" placeholder="如 max_positions / commission / top_n" />
        </div>
        <div style="flex:0 0 auto">
          <label>&nbsp;</label>
          <button class="btn ghost" @click="reset">撤销改动</button>
        </div>
        <div style="flex:0 0 auto">
          <label>&nbsp;</label>
          <button @click="save">保存（{{ dirty.length }} 项改动）</button>
        </div>
        <div style="flex:0 0 auto">
          <label>&nbsp;</label>
          <button class="btn ghost" @click="load">刷新</button>
        </div>
      </div>
      <div style="margin-top:12px; display:flex; gap:6px; flex-wrap:wrap">
        <button
          v-for="s in sections" :key="s"
          class="btn sm" :class="activeSection === s ? '' : 'ghost'"
          :title="SECTION_TITLES[s] || ''"
          @click="activeSection = s"
        >{{ s }}</button>
        <button class="btn sm" :class="activeSection === '' ? '' : 'ghost'" @click="activeSection = ''">全部</button>
      </div>
    </div>

    <div class="card">
      <h3>{{ activeSection || "全部配置项" }} <span class="sub">{{ SECTION_TITLES[activeSection] || "" }} · {{ visible.length }} 项</span></h3>
      <table>
        <thead><tr><th style="width:32%">配置路径</th><th>值</th><th style="width:60px">类型</th><th style="width:30%">说明</th></tr></thead>
        <tbody>
          <tr v-for="l in visible" :key="l.path">
            <td>
              <span class="pill" :title="hintOf(l.path)">{{ l.path }}</span>
              <span v-if="String(l.value) !== String(l.origin)" class="badge warn" style="margin-left:6px">改</span>
            </td>
            <td>
              <input v-if="l.kind === 'bool'" type="checkbox" v-model="l.value" style="width:auto" />
              <input v-else-if="l.kind === 'number'" type="number" step="any" v-model.number="l.value" />
              <textarea v-else-if="l.kind === 'json'" v-model="l.value" rows="2"></textarea>
              <input v-else v-model="l.value" />
            </td>
            <td class="tiny muted">{{ l.kind }}</td>
            <td class="tiny muted" style="line-height:1.5">{{ hintOf(l.path) }}</td>
          </tr>
          <tr v-if="!visible.length"><td colspan="4" class="muted">无匹配配置项</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
