<script setup lang="ts">
import { onMounted, ref } from "vue";
import api from "@/api";
import { useApp } from "@/store";
import { pushToast, tryReq } from "@/toast";
import Modal from "@/components/Modal.vue";

const app = useApp();
const loading = ref(false);
const channels = ref<any[]>([]);
const raw = ref<any>(null);
const secrets = ref<any[]>([]);
const chDlg = ref<any>(null);
const testDlg = ref<any>(null);
const secretEdit = ref<any>(null);

const PRESETS: Record<string, { label: string; env: string; hint: string }> = {
  feishu: { label: "飞书群机器人", env: "FEISHU_WEBHOOK", hint: "https://open.feishu.cn/open-apis/bot/v2/hook/xxx" },
  wecom: { label: "企业微信群机器人", env: "WECOM_WEBHOOK", hint: "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx" },
  dingtalk: { label: "钉钉群机器人", env: "DINGTALK_WEBHOOK", hint: "https://oapi.dingtalk.com/robot/send?access_token=xxx" },
  console: { label: "控制台输出", env: "", hint: "仅打印到日志，无需密钥" },
};

async function load() {
  loading.value = true;
  const [c, s] = await Promise.all([
    tryReq(() => api.notifyChannels()),
    tryReq(() => api.secrets()),
  ]);
  channels.value = c?.channels || [];
  raw.value = c?.raw || null;
  secrets.value = s || [];
  loading.value = false;
}

function newChannel() {
  chDlg.value = { _idx: -1, type: "feishu", webhook_env: "FEISHU_WEBHOOK", enabled: true, min_level: "INFO" };
}
function editChannel(ch: any, idx: number) {
  chDlg.value = { _idx: idx, type: ch.type, webhook_env: ch.webhook_env || ch.env || "",
                  enabled: ch.enabled !== false, min_level: ch.min_level || "INFO" };
}
function onTypeChange() {
  const p = PRESETS[chDlg.value.type];
  if (p) chDlg.value.webhook_env = p.env;
}

async function saveChannel() {
  const c = chDlg.value;
  const list = channels.value.map((x: any) => {
    const o: any = { type: x.type };
    if (x.webhook_env || x.env) o.webhook_env = x.webhook_env || x.env;
    if (x.enabled !== undefined) o.enabled = x.enabled;
    if (x.min_level) o.min_level = x.min_level;
    return o;
  });
  const item: any = { type: c.type, enabled: !!c.enabled, min_level: c.min_level };
  if (c.webhook_env) item.webhook_env = c.webhook_env;
  if (c._idx >= 0) list[c._idx] = item; else list.push(item);
  const r = await tryReq(() => api.notifySetChannels(list), "推送频道已保存到 settings.yaml");
  if (r) { chDlg.value = null; load(); }
}

async function delChannel(idx: number) {
  if (!confirm("删除该推送频道？")) return;
  const list = channels.value
    .filter((_: any, i: number) => i !== idx)
    .map((x: any) => {
      const o: any = { type: x.type };
      if (x.webhook_env || x.env) o.webhook_env = x.webhook_env || x.env;
      if (x.enabled !== undefined) o.enabled = x.enabled;
      if (x.min_level) o.min_level = x.min_level;
      return o;
    });
  const r = await tryReq(() => api.notifySetChannels(list), "频道已删除");
  if (r) load();
}

function openTest() {
  testDlg.value = { title: "WebUI 测试", body: "这是来自 Web 控制台的测试消息。", channel: "", result: null };
}
async function runTest() {
  loading.value = true;
  const t = testDlg.value;
  const r = await tryReq(() => api.notifyTest(
    { title: t.title, body: t.body, channel: t.channel || null }, app.mode));
  loading.value = false;
  if (r) {
    t.result = r;
    pushToast(r.ok ? "测试消息已发送" : `发送失败：${r.error || "未知"}`, r.ok ? "ok" : "err");
  }
}

async function saveSecret() {
  const s = secretEdit.value;
  if (!s?.value) { pushToast("值不能为空", "err"); return; }
  const r = await tryReq(() => api.setSecret(s.key, s.value), `${s.key} 已写入 config/.env`);
  if (r) { secretEdit.value = null; load(); }
}

function envOf(ch: any) { return ch.webhook_env || ch.env || ""; }

onMounted(load);
</script>

<template>
  <div :class="{ loading }">
    <div class="card">
      <h3>🔔 推送频道
        <span class="sub">Webhook 地址只存环境变量名，值落 config/.env，绝不进 YAML / 数据库</span>
        <div class="spacer"></div>
        <button class="btn sm ghost" @click="load">刷新</button>
        <button class="btn sm ghost" style="margin-left:6px" @click="openTest">发送测试</button>
        <button class="btn sm" style="margin-left:6px" @click="newChannel">+ 新增频道</button>
      </h3>
      <table>
        <thead>
          <tr><th style="width:150px">类型</th><th>Webhook 环境变量</th><th style="width:100px">密钥状态</th>
            <th style="width:90px">最低级别</th><th style="width:80px">启用</th><th style="width:130px">操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="(ch, i) in channels" :key="i">
            <td><b>{{ PRESETS[ch.type]?.label || ch.type }}</b><div class="tiny muted">{{ ch.type }}</div></td>
            <td class="pill tiny">{{ envOf(ch) || "—（无需密钥）" }}</td>
            <td>
              <span v-if="ch.secret_set === null" class="badge muted">N/A</span>
              <span v-else class="badge" :class="ch.secret_set ? 'ok' : 'danger'">
                {{ ch.secret_set ? "已配置" : "缺失" }}
              </span>
            </td>
            <td class="tiny">{{ ch.min_level || "INFO" }}</td>
            <td><span class="badge" :class="ch.enabled === false ? 'muted' : 'ok'">{{ ch.enabled === false ? "停用" : "启用" }}</span></td>
            <td>
              <button class="btn sm ghost" @click="editChannel(ch, i)">编辑</button>
              <button class="btn sm danger" style="margin-left:4px" @click="delChannel(i)">删</button>
            </td>
          </tr>
          <tr v-if="!channels.length"><td colspan="6" class="muted">尚未配置推送频道</td></tr>
        </tbody>
      </table>
    </div>

    <div class="card">
      <h3>🔑 Webhook 密钥 <span class="sub">写入后立即在本进程生效；CLI 下次启动自动读取</span></h3>
      <table>
        <thead><tr><th>环境变量</th><th>状态</th><th>掩码</th><th style="width:90px">操作</th></tr></thead>
        <tbody>
          <tr v-for="s in secrets.filter((x: any) => x.key.includes('WEBHOOK'))" :key="s.key">
            <td class="pill">{{ s.key }}</td>
            <td><span class="badge" :class="s.set ? 'ok' : 'muted'">{{ s.set ? "已配置" : "未配置" }}</span></td>
            <td class="tiny muted">{{ s.masked || "-" }}</td>
            <td><button class="btn sm ghost" @click="secretEdit = { key: s.key, value: '' }">设置</button></td>
          </tr>
        </tbody>
      </table>
      <div class="tiny muted" style="margin-top:8px">
        飞书变量名（FEISHU_WEBHOOK）若不在列表，请先在 config/.env 手动加一行 <code>FEISHU_WEBHOOK=""</code>，之后即可在此覆写。
      </div>
    </div>

    <div class="card" v-if="raw">
      <h3>原始配置 ops.notify</h3>
      <pre class="json">{{ JSON.stringify(raw, null, 2) }}</pre>
    </div>

    <Modal v-if="chDlg" :title="chDlg._idx >= 0 ? '编辑推送频道' : '新增推送频道'" @close="chDlg = null">
      <div class="row">
        <div class="field"><label>类型</label>
          <select v-model="chDlg.type" @change="onTypeChange">
            <option v-for="(p, k) in PRESETS" :key="k" :value="k">{{ p.label }}</option>
          </select>
        </div>
        <div class="field"><label>最低推送级别</label>
          <select v-model="chDlg.min_level">
            <option>INFO</option><option>WARN</option><option>ERROR</option><option>CRITICAL</option>
          </select>
        </div>
      </div>
      <div class="field"><label>Webhook 环境变量名</label>
        <input v-model="chDlg.webhook_env" :placeholder="PRESETS[chDlg.type]?.env" />
        <div class="tiny muted">{{ PRESETS[chDlg.type]?.hint }}</div>
      </div>
      <div class="field">
        <label><input type="checkbox" v-model="chDlg.enabled" style="width:auto" /> 启用该频道</label>
      </div>
      <template #actions>
        <button class="btn ghost" @click="chDlg = null">取消</button>
        <button @click="saveChannel">保存</button>
      </template>
    </Modal>

    <Modal v-if="testDlg" title="发送测试消息" @close="testDlg = null">
      <div class="field"><label>标题</label><input v-model="testDlg.title" /></div>
      <div class="field"><label>内容</label><textarea v-model="testDlg.body" rows="3"></textarea></div>
      <div class="field"><label>指定频道（留空=全部已启用）</label>
        <select v-model="testDlg.channel">
          <option value="">（全部）</option>
          <option v-for="(ch, i) in channels" :key="i" :value="ch.type">{{ ch.type }}</option>
        </select>
      </div>
      <div v-if="testDlg.result" style="margin-top:8px">
        <span class="badge" :class="testDlg.result.ok ? 'ok' : 'danger'">
          {{ testDlg.result.ok ? "发送成功" : "发送失败" }}
        </span>
        <span class="tiny muted" style="margin-left:8px">{{ testDlg.result.error || "" }}</span>
        <div v-for="r in testDlg.result.channels || []" :key="r.channel" class="tiny" style="margin-top:4px">
          <span class="badge" :class="r.ok ? 'ok' : 'danger'">{{ r.ok ? "成功" : "失败" }}</span>
          <b style="margin-left:6px">{{ r.channel }}</b>
          <span v-if="!r.ok" class="muted" style="margin-left:6px">{{ r.error }}</span>
        </div>
      </div>
      <template #actions>
        <button class="btn ghost" @click="testDlg = null">关闭</button>
        <button @click="runTest">发送</button>
      </template>
    </Modal>

    <Modal v-if="secretEdit" :title="`设置 ${secretEdit.key}`" @close="secretEdit = null">
      <div class="field"><label>Webhook 完整地址</label>
        <input v-model="secretEdit.value" type="password" placeholder="https://..." /></div>
      <template #actions>
        <button class="btn ghost" @click="secretEdit = null">取消</button>
        <button @click="saveSecret">保存</button>
      </template>
    </Modal>
  </div>
</template>
