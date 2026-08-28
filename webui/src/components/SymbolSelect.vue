<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, nextTick } from "vue";

interface Opt { symbol: string; name?: string; industry?: string }

const props = defineProps<{
  modelValue: string;
  options: Opt[];
  placeholder?: string;
  maxRender?: number;
}>();
const emit = defineEmits<{
  (e: "update:modelValue", v: string): void;
  (e: "select", v: string): void;
}>();

const open = ref(false);
const query = ref("");
const active = ref(0);
const listRef = ref<HTMLElement | null>(null);
const rootRef = ref<HTMLElement | null>(null);

const maxRender = computed(() => props.maxRender || 300);

const filtered = computed<Opt[]>(() => {
  const q = query.value.trim().toLowerCase();
  let arr = props.options;
  if (q) {
    arr = arr.filter((s) =>
      `${s.symbol} ${s.name || ""} ${s.industry || ""}`.toLowerCase().includes(q)
    );
  }
  return arr.slice(0, maxRender.value);
});

const currentLabel = computed(() => {
  const s = props.options.find((o) => o.symbol === props.modelValue);
  return s ? `${s.symbol} ${s.name || ""}`.trim() : props.modelValue;
});

function openMenu() {
  open.value = true;
  active.value = 0;
  nextTick(scrollActive);
}
function choose(s: string) {
  emit("update:modelValue", s);
  emit("select", s);
  open.value = false;
}
function onKey(e: KeyboardEvent) {
  if (!open.value && (e.key === "ArrowDown" || e.key === "Enter")) {
    openMenu();
    return;
  }
  if (e.key === "ArrowDown") {
    active.value = Math.min(active.value + 1, filtered.value.length - 1);
    e.preventDefault();
    scrollActive();
  } else if (e.key === "ArrowUp") {
    active.value = Math.max(active.value - 1, 0);
    e.preventDefault();
    scrollActive();
  } else if (e.key === "Enter") {
    if (filtered.value[active.value]) choose(filtered.value[active.value].symbol);
    e.preventDefault();
  } else if (e.key === "Escape") {
    open.value = false;
  }
}
function scrollActive() {
  const el = listRef.value?.querySelector(".opt.active") as HTMLElement | null;
  el?.scrollIntoView({ block: "nearest" });
}
function onDocDown(e: MouseEvent) {
  if (rootRef.value && !rootRef.value.contains(e.target as Node)) open.value = false;
}
onMounted(() => document.addEventListener("mousedown", onDocDown));
onBeforeUnmount(() => document.removeEventListener("mousedown", onDocDown));
</script>

<template>
  <div class="sym-select" :class="{ open }" ref="rootRef">
    <div class="ss-control" tabindex="0" @click="openMenu" @keydown="onKey">
      <span v-if="modelValue" class="ss-value">{{ currentLabel }}</span>
      <span v-else class="ss-ph">{{ placeholder || "搜索代码 / 名称 / 行业…" }}</span>
      <span class="ss-caret">▾</span>
    </div>
    <div v-if="open" class="ss-pop">
      <input
        class="ss-search"
        v-model="query"
        placeholder="输入代码 / 名称 / 行业过滤"
        @keydown="onKey"
        ref="searchInput"
        autofocus
      />
      <div class="ss-list" ref="listRef">
        <div
          v-for="(s, i) in filtered"
          :key="s.symbol"
          class="opt"
          :class="{ active: i === active, sel: s.symbol === modelValue }"
          @mouseenter="active = i"
          @click="choose(s.symbol)"
        >
          <span class="sym">{{ s.symbol }}</span>
          <span class="nm">{{ s.name }}</span>
          <span class="ind" v-if="s.industry">{{ s.industry }}</span>
        </div>
        <div v-if="!filtered.length" class="ss-empty">无匹配标的</div>
      </div>
      <div class="ss-foot">
        共 {{ options.length }} 只 · 显示前 {{ filtered.length }} 只
      </div>
    </div>
  </div>
</template>

<style scoped>
.sym-select {
  position: relative;
  width: 100%;
}
.ss-control {
  display: flex;
  align-items: center;
  gap: 8px;
  min-height: 38px;
  padding: 7px 10px;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 9px;
  cursor: pointer;
  color: var(--text);
}
.ss-control:focus {
  outline: 2px solid var(--primary);
  outline-offset: 0;
}
.ss-value {
  flex: 1;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.ss-ph {
  flex: 1;
  color: var(--text-2);
}
.ss-caret {
  color: var(--text-2);
  font-size: 12px;
}
.ss-pop {
  position: absolute;
  z-index: 50;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  background: var(--bg-elev);
  border: 1px solid var(--border);
  border-radius: 11px;
  box-shadow: 0 12px 30px rgba(20, 24, 40, 0.18);
  overflow: hidden;
}
.ss-search {
  width: 100%;
  border: none;
  border-bottom: 1px solid var(--border);
  padding: 10px 12px;
  font-size: 13px;
  background: var(--bg-2);
  color: var(--text);
}
.ss-search:focus {
  outline: none;
}
.ss-list {
  max-height: 320px;
  overflow: auto;
  padding: 4px;
}
.opt {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 9px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
}
.opt:hover,
.opt.active {
  background: var(--bg-2);
}
.opt.sel {
  background: var(--primary);
  color: #fff;
}
.opt.sel .ind {
  color: rgba(255, 255, 255, 0.8);
}
.opt .sym {
  font-weight: 700;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}
.opt .nm {
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.opt .ind {
  color: var(--text-2);
  font-size: 11px;
  flex: 0 0 auto;
}
.ss-empty {
  padding: 16px;
  text-align: center;
  color: var(--text-2);
  font-size: 13px;
}
.ss-foot {
  padding: 7px 12px;
  border-top: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-2);
  background: var(--bg-2);
}
</style>
