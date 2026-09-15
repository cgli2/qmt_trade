<script setup lang="ts">
import {
  JOB_KIND, JOB_STATUS, cn,
} from "@/labels";
import { computed, onMounted, onUnmounted } from "vue";
import { useTasks } from "@/tasks";
import api from "@/api";
const tasks = useTasks();
const running = computed(() => tasks.jobs.filter(j => ["pending", "running"].includes(j.status)).length);
const unread = computed(() => tasks.notifications.filter(n => !n.read_at).length);
async function read(id: string) { await api.readNotification(id); await tasks.refresh(); }
onMounted(tasks.start); onUnmounted(tasks.stop);
</script>
<template>
  <details class="task-center">
    <summary aria-label="打开任务和通知">任务 {{ running }} · 未读 {{ unread }}</summary>
    <div class="task-panel card">
      <p v-if="tasks.error" role="status">{{ tasks.error }} <button class="btn sm" @click="tasks.refresh">重试</button></p>
      <p v-if="!tasks.jobs.length">暂无后台任务</p>
      <div v-for="job in tasks.jobs.slice(0, 6)" :key="job.id" class="task-row">
        <router-link :to="{path:'/backtest', query:{job:job.id}}">{{ cn(JOB_KIND, job.kind) }} · {{ cn(JOB_STATUS, job.state || job.status) }}</router-link>
        <small>{{ job.progress }}</small>
      </div>
      <hr />
      <div v-for="n in tasks.notifications.filter(n => !n.read_at).slice(0, 10)" :key="n.id" class="task-row">
        <router-link :to="{path:'/backtest', query:{job:n.job_id}}">任务 {{ cn(JOB_STATUS, n.status) }} · 查看结果</router-link>
        <button class="btn sm ghost" @click="read(n.id)">标记已读</button>
      </div>
    </div>
  </details>
</template>
<style scoped>
.task-center { position: relative; } summary { cursor: pointer; }
.task-panel { position: absolute; z-index: 50; top: 32px; right: 0; width: min(380px, 85vw); max-height: 65vh; overflow: auto; }
.task-row { display: grid; gap: 4px; padding: 8px 0; }
</style>
