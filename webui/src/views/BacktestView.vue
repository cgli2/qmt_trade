<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import api from "@/api";
import { useApp } from "@/store";
import { useTasks } from "@/tasks";
import { tryReq, pushToast } from "@/toast";
const app = useApp(), tasks = useTasks(), route = useRoute(), router = useRouter();
const strategies = ref<any[]>([]), instances = ref<any[]>([]);
const form = ref({strategy:"balanced", start: new Date(Date.now()-365*86400000).toISOString().slice(0,10), end:new Date().toISOString().slice(0,10), cash:1000000, version_id:""});
const submitting = ref(false), reportLoading = ref(false), error = ref("");
const report = ref<any>(null), trades = ref<any[]>([]), tradeTotal = ref(0), offset = ref(0);
const selected = computed(() => String(route.query.job || ""));
const history = computed(() => tasks.jobs.filter(j => j.kind === "backtest"));
const current = computed(() => history.value.find(j => j.id === selected.value));
const versions = computed(() => instances.value.filter(i => i.strategy_id === form.value.strategy && i.active_version));
let reportGeneration = 0;
let submitKey = "", submitBody = "";
const statusLabels: Record<string,string> = {pending:"排队中", running:"运行中", done:"已完成", error:"失败或中断",queued:"排队中",succeeded:"已完成",failed:"失败",cancelled:"已取消",interrupted:"已中断"};
const labels: Record<string,string> = {total_return:"总收益率", annual_return:"年化收益", sharpe:"夏普比率", max_drawdown:"最大回撤", win_rate:"胜率", profit_factor:"盈亏比", calmar:"卡玛比率", volatility:"年化波动", trade_count:"交易次数"};
const fractions = new Set(["total_return","annual_return","max_drawdown","win_rate","volatility","turnover"]);
function metric(key:string, value:any) {
  if (value === null || value === undefined) return "不适用";
  if (typeof value !== "number") return String(value);
  const unit=report.value?.metric_units?.[key];
  return (unit === "fraction" || (!unit && fractions.has(key))) ? (value*100).toFixed(2)+"%" : unit === "count" ? String(Math.round(value)) : value.toFixed(3);
}
const metrics = computed(() => Object.entries(report.value?.metrics || {}).map(([key,value]) => ({key,label:labels[key] || key,value:metric(key,value)})));
const curve = computed(() => {
  const points = report.value?.equity_curve || [];
  if (points.length < 2) return "";
  const values = points.map((p:any) => Number(p.equity));
  const lo = Math.min(...values), hi = Math.max(...values), span = hi-lo || 1;
  return values.map((v:number,i:number) => `${i/(values.length-1)*900},${210-(v-lo)/span*190}`).join(" ");
});
function period(months:number) { const end = new Date(); const start = new Date(end); start.setMonth(start.getMonth()-months); form.value.start=start.toISOString().slice(0,10); form.value.end=end.toISOString().slice(0,10); }
async function submit() {
  if (submitting.value) return;
  submitting.value = true; error.value = "";
  const body = JSON.stringify(form.value);
  if (body !== submitBody || !submitKey) { submitBody=body; submitKey=crypto.randomUUID(); }
  try {
    const result = await api.backtestRun({...form.value, version_id:form.value.version_id || null, idempotency_key:submitKey}, app.mode);
    submitKey="";
    await router.replace({query:{job:result.job_id}});
    await tasks.refresh();
    pushToast("回测已提交，离开页面后仍会继续运行", "ok");
  } catch (e:any) { error.value=e.message; }
  finally { submitting.value=false; }
}
async function loadReport() {
  const id=selected.value, generation=++reportGeneration;
  report.value=null; trades.value=[]; offset.value=0;
  if (!id) return;
  reportLoading.value=true; error.value="";
  try {
    const job=await api.job(id);
    if (generation !== reportGeneration) return;
    if (job.status === "done") {
      const [r,t]=await Promise.all([api.backtestReport(id),api.backtestTrades(id)]);
      if (generation !== reportGeneration) return;
      report.value=r; trades.value=t.items; tradeTotal.value=t.total;
    } else if (job.status === "error") error.value=job.error || "任务未完成，可重跑";
  } catch (e:any) { if(generation===reportGeneration) error.value=e.message; }
  finally { if(generation===reportGeneration) reportLoading.value=false; }
}
async function page(delta:number) {
  const id=selected.value, next=Math.max(0,offset.value+delta);
  const result=await tryReq(() => api.backtestTrades(id,next));
  if (result && id===selected.value) { offset.value=next; trades.value=result.items; }
}
async function cancel(id:string) { await tryReq(() => api.cancelJob(id), "已请求取消，等待后台确认停止"); await tasks.refresh(); }
async function retry(id:string) { const r=await tryReq(() => api.retryJob(id)); if(r) { await router.replace({query:{job:r.job_id}}); await tasks.refresh(); } }
watch(selected,loadReport);
watch(() => current.value?.status, status => { if(status === "done" || status === "error") void loadReport(); });
watch(() => form.value.strategy, () => { form.value.version_id=""; });
onMounted(async () => {
  const r=await tryReq(() => api.strategies()); strategies.value=r?.strategies || [];
  const m=await tryReq(() => api.strategyManagement(app.mode)); instances.value=m?.instances || [];
  await tasks.refresh(); await loadReport();
});
</script>
<template>
  <div>
    <section class="card">
      <h3>新建回测</h3>
      <p class="muted">选择策略与日期区间，结果将保存在任务历史中。</p>
      <div class="row">
        <label>策略<select v-model="form.strategy"><option v-for="s in strategies" :key="s.id" :value="s.id">{{s.name}}</option></select></label>
        <label>开始日期<input type="date" v-model="form.start" /></label>
        <label>结束日期<input type="date" v-model="form.end" /></label>
        <button :disabled="submitting" @click="submit">{{submitting ? '正在提交…' : '开始回测'}}</button>
      </div>
      <div class="row" style="margin-top:12px"><span>常用周期</span><button v-for="n in [1,3,6,12]" :key="n" class="btn sm ghost" @click="period(n)">近 {{n}} 个月</button></div>
      <details style="margin-top:14px"><summary>高级选项</summary><div class="row">
        <label>初始资金（元）<input type="number" min="1" v-model.number="form.cash" /></label>
        <label>参数版本<select v-model="form.version_id"><option value="">当前启用版本（无实例时用默认参数）</option><option v-for="i in versions" :key="i.id" :value="i.id+':'+i.active_version">{{i.name}} / {{i.active_version}}</option></select></label>
      </div><p class="muted">预热按策略需求准备；LLM 默认关闭，交易成本随提交时的配置一起保存。</p></details>
      <p v-if="error" role="alert" class="badge danger">{{error}}</p>
    </section>
    <section class="card">
      <h3>任务历史 <button class="btn sm ghost" @click="tasks.refresh">刷新</button></h3>
      <p v-if="tasks.error" role="alert">{{tasks.error}}</p>
      <div style="overflow:auto"><table><thead><tr><th>任务</th><th>状态 / 阶段</th><th>提交时间</th><th>操作</th></tr></thead>
        <tbody><tr v-for="job in history" :key="job.id">
          <td><router-link :to="{query:{job:job.id}}">{{job.id.slice(0,10)}}</router-link></td>
          <td>{{statusLabels[job.state || job.status] || job.status}}<br/><small>{{job.progress}}</small></td>
          <td>{{new Date(job.created*1000).toLocaleString()}}</td>
          <td><button v-if="['pending','running'].includes(job.status)" class="btn sm ghost" @click="cancel(job.id)">取消</button><button v-else-if="job.status==='error'" class="btn sm" @click="retry(job.id)">重跑</button><router-link v-else :to="{query:{job:job.id}}">查看报告</router-link></td>
        </tr><tr v-if="!history.length"><td colspan="4">暂无回测任务</td></tr></tbody>
      </table></div>
    </section>
    <p v-if="reportLoading" role="status">正在加载报告…</p>
    <section v-if="report" class="card">
      <h3>回测报告 · {{report.strategy}}</h3>
      <p>{{report.start}} 至 {{report.end}} · 初始资金 {{report.cash?.toLocaleString()}} 元 · {{report.version_id || '默认参数快照'}}</p>
      <p v-if="report.no_trades">本区间无成交；以下净值基于有效行情计算。</p>
      <div class="grid cols-4"><div v-for="m in metrics" :key="m.key" class="stat"><span class="label">{{m.label}}</span><strong class="value sm">{{m.value}}</strong></div></div>
      <svg v-if="curve" viewBox="0 0 900 220" style="width:100%;max-height:300px" role="img" aria-label="覆盖整个回测区间的净值曲线"><polyline :points="curve" fill="none" stroke="var(--primary)" stroke-width="2" /></svg>
      <p class="muted">{{report.equity_curve?.[0]?.date}} — {{report.equity_curve?.[report.equity_curve.length-1]?.date}}</p>
      <h4>成交明细（{{tradeTotal}} 笔）</h4>
      <div style="overflow:auto"><table><thead><tr><th>时间</th><th>标的</th><th>方向</th><th>价格</th><th>数量</th></tr></thead><tbody>
        <tr v-for="(t,i) in trades" :key="i"><td>{{t.time || t.filled_at || t.trade_time || t.date}}</td><td>{{t.symbol}}</td><td>{{t.side}}</td><td>{{t.price}}</td><td>{{t.quantity ?? t.volume ?? t.shares}}</td></tr>
        <tr v-if="!trades.length"><td colspan="5">本区间无成交</td></tr>
      </tbody></table></div>
      <button class="btn sm ghost" :disabled="offset===0" @click="page(-50)">上一页</button>
      <button class="btn sm ghost" :disabled="offset+50>=tradeTotal" @click="page(50)">下一页</button>
      <details><summary>数据来源和覆盖</summary><p>{{report.data_sources?.join('、')}}</p><p>数据版本：{{report.data_hash}}</p><p v-for="(c,i) in report.coverage" :key="i">{{c.frequency}} · {{c.rows}} 行 · {{c.first}} — {{c.last}}</p></details>
    </section>
  </div>
</template>
