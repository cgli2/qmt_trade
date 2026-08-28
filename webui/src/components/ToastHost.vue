<script setup lang="ts">
import { toasts } from "@/toast";
</script>

<template>
  <div class="toast">
    <div v-for="t in toasts" :key="t.id" class="t" :class="t.type">{{ t.text }}</div>
  </div>
</template>
