import { createRouter, createWebHistory, type RouteRecordRaw } from "vue-router";

const routes: RouteRecordRaw[] = [
  { path: "/", name: "dashboard", component: () => import("@/views/DashboardView.vue"), meta: { title: "工作台" } },
  { path: "/selection", name: "selection", component: () => import("@/views/SelectionView.vue"), meta: { title: "选股研判" } },
  { path: "/market", name: "market", component: () => import("@/views/MarketView.vue"), meta: { title: "行情与事件" } },
  { path: "/trade/paper", name: "trade-paper", component: () => import("@/views/TradeView.vue"), props: { mode: "paper" }, meta: { title: "模拟盘" } },
  { path: "/trade/live", name: "trade-live", component: () => import("@/views/TradeView.vue"), props: { mode: "live" }, meta: { title: "实盘" } },
  { path: "/strategy", name: "strategy", component: () => import("@/views/StrategyView.vue"), meta: { title: "策略管理" } },
  { path: "/strategylab", name: "strategylab", component: () => import("@/views/StrategyLabView.vue"), meta: { title: "策略实验室" } },
  { path: "/tailpick", redirect: "/strategy" },
  { path: "/backtest", name: "backtest", component: () => import("@/views/BacktestView.vue"), meta: { title: "回测管理" } },
  { path: "/risk", name: "risk", component: () => import("@/views/RiskView.vue"), meta: { title: "风控管理" } },
  { path: "/reports", name: "reports", component: () => import("@/views/ReportView.vue"), meta: { title: "绩效报告" } },
  { path: "/settings", name: "settings", component: () => import("@/views/SettingsView.vue"), meta: { title: "系统设置" } },
  // 旧路径兼容重定向（页面已融合）
  { path: "/trade", redirect: "/trade/paper" },
  { path: "/llm", redirect: "/settings" },
  { path: "/datasource", redirect: "/settings" },
  { path: "/config", redirect: "/settings" },
  { path: "/notify", redirect: "/settings" },
  { path: "/event", redirect: "/market" },
];

export default createRouter({
  history: createWebHistory(),
  routes,
});

declare module "vue-router" {
  interface RouteMeta {
    title?: string;
  }
}
