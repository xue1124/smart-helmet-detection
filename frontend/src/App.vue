<script setup lang="ts">
import { UploadFilled, VideoCamera, Upload, Connection, Monitor, Document } from "@element-plus/icons-vue";
import type { UploadRequestOptions } from "element-plus";
import { ElMessage } from "element-plus";
import { computed, onMounted, onUnmounted, reactive, ref, watch, type Ref } from "vue";
import http, { fetchActiveRtspSessions, fetchCurrentUser, logout, type AuthUser, type IsapiChannel, type MonitorDevice, type TaskInfo } from "./api";
import DevicePanel from "./components/DevicePanel.vue";
import LoginPage from "./components/LoginPage.vue";
import MonitorLive from "./components/MonitorLive.vue";
import ResultPanel from "./components/ResultPanel.vue";
import SafetyRecordsPanel from "./components/SafetyRecordsPanel.vue";

type TabKey = "local" | "isapi" | "devices" | "rtsp" | "records";

interface RtspSession {
  deviceId: number;
  deviceName: string;
  taskId: string;
  task: TaskInfo | null;
}

const STORAGE = {
  tab: "sc_active_tab",
  boot: "sc_server_boot_id",
  local: "sc_task_local",
  isapi: "sc_task_isapi",
  rtspSessions: "sc_rtsp_sessions",
} as const;

const activeTab = ref<TabKey>("local");
const authChecking = ref(true);
const currentUser = ref<AuthUser | null>(null);
const frameStride = ref(25);
const enableReid = ref(true);
const reidThreshold = ref(0.7);
const ffmpegOk = ref(true);

const localTaskId = ref("");
const isapiTaskId = ref("");

const localTask = ref<TaskInfo | null>(null);
const isapiTask = ref<TaskInfo | null>(null);

const uploading = ref(false);
const uploadPercent = ref(0);
const stoppingLocal = ref(false);
const stoppingIsapi = ref(false);

const analysisActiveStatuses = new Set(["pending", "downloading", "running"]);

const probingChannels = ref(false);
const channelOptions = ref<IsapiChannel[]>([]);
const nvr = reactive({
  host: "",
  username: "",
  password: "",
  channel: null as number | null,
  startAt: "",
  endAt: "",
});

const monitorDevices = ref<MonitorDevice[]>([]);
const selectedDeviceIds = ref<number[]>([]);
const rtspSessions = ref<RtspSession[]>([]);
const rtspStarting = ref(false);
const rtspStopping = ref(false);
const rtspStoppingDeviceId = ref<number | null>(null);

const MAX_RTSP_CAMERAS = 9;

const streamingDeviceIds = computed(() => new Set(rtspSessions.value.map((s) => s.deviceId)));

const pendingDeviceIds = computed(() =>
  selectedDeviceIds.value.filter((id) => !streamingDeviceIds.value.has(id)),
);

const isRtspStreaming = computed(() => rtspSessions.value.length > 0);

const canStartRtsp = computed(() => {
  if (rtspStarting.value || pendingDeviceIds.value.length === 0) return false;
  return rtspSessions.value.length + pendingDeviceIds.value.length <= MAX_RTSP_CAMERAS;
});

let pollTimer: ReturnType<typeof setInterval> | null = null;

const menuItems = [
  { key: "local" as TabKey, label: "本地上传", icon: Upload },
  { key: "isapi" as TabKey, label: "录像回放", icon: Connection },
  { key: "devices" as TabKey, label: "我的设备", icon: Monitor },
  { key: "rtsp" as TabKey, label: "实时监控", icon: VideoCamera },
  { key: "records" as TabKey, label: "安全记录", icon: Document },
];

const canSubmitIsapi = computed(
  () =>
    ffmpegOk.value &&
    !!nvr.password.trim() &&
    !!nvr.startAt &&
    !!nvr.endAt &&
    nvr.channel != null,
);

const canStopLocal = computed(
  () => !!localTaskId.value && !!localTask.value && analysisActiveStatuses.has(localTask.value.status),
);

const canStopIsapi = computed(
  () => !!isapiTaskId.value && !!isapiTask.value && analysisActiveStatuses.has(isapiTask.value.status),
);

async function fetchTask(id: string): Promise<TaskInfo | null> {
  if (!id) return null;
  try {
    const { data } = await http.get<TaskInfo>(`/tasks/${encodeURIComponent(id)}`);
    return data;
  } catch {
    return null;
  }
}

function persistTaskId(key: string, id: string) {
  if (id) localStorage.setItem(key, id);
  else localStorage.removeItem(key);
}

function persistRtspSessions() {
  const payload = rtspSessions.value.map((s) => ({
    deviceId: s.deviceId,
    deviceName: s.deviceName,
    taskId: s.taskId,
  }));
  if (payload.length) localStorage.setItem(STORAGE.rtspSessions, JSON.stringify(payload));
  else localStorage.removeItem(STORAGE.rtspSessions);
}

function clearTabTask(tab: TabKey) {
  if (tab === "local") {
    localTaskId.value = "";
    localTask.value = null;
  } else if (tab === "isapi") {
    isapiTaskId.value = "";
    isapiTask.value = null;
  } else if (tab === "rtsp") {
    rtspSessions.value = [];
    persistRtspSessions();
  }
}

function clearTaskSession() {
  clearTabTask("local");
  clearTabTask("isapi");
  clearTabTask("rtsp");
}

async function syncServerBoot(): Promise<void> {
  try {
    const { data } = await http.get<{ boot_id?: string; ffmpeg?: boolean }>("/health");
    ffmpegOk.value = data.ffmpeg !== false;
    const bootId = data.boot_id || "";
    if (!bootId) return;
    const prev = localStorage.getItem(STORAGE.boot);
    if (prev && prev !== bootId) {
      clearTaskSession();
    }
    localStorage.setItem(STORAGE.boot, bootId);
  } catch {
    ffmpegOk.value = false;
  }
}

async function restoreLatestTask(source: "local" | "isapi", idRef: Ref<string>) {
  if (idRef.value) return;
  try {
    const { data } = await http.get<{ tasks: TaskInfo[] }>("/tasks", {
      params: { source_type: source },
    });
    const latest = data.tasks?.[0];
    if (latest?.id) idRef.value = latest.id;
  } catch {
    /* ignore */
  }
}

async function syncRtspFromServer() {
  try {
    const active = await fetchActiveRtspSessions();
    const sessions: RtspSession[] = [];
    for (const item of active) {
      if (item.device_id == null) continue;
      sessions.push({
        deviceId: item.device_id,
        deviceName: item.device_name || `设备${item.device_id}`,
        taskId: item.task_id,
        task: await fetchTask(item.task_id),
      });
    }
    rtspSessions.value = sessions;
    if (sessions.length) {
      const ids = new Set([...selectedDeviceIds.value, ...sessions.map((s) => s.deviceId)]);
      selectedDeviceIds.value = [...ids];
    }
    persistRtspSessions();
  } catch {
    /* ignore */
  }
}

async function restoreRtspSessions() {
  await syncRtspFromServer();
  if (rtspSessions.value.length) return;
  const raw = localStorage.getItem(STORAGE.rtspSessions);
  if (!raw) return;
  try {
    const saved = JSON.parse(raw) as Array<{ deviceId: number; deviceName: string; taskId: string }>;
    const sessions: RtspSession[] = [];
    for (const item of saved) {
      const task = await fetchTask(item.taskId);
      if (task && (task.status === "streaming" || task.status === "pending")) {
        sessions.push({ ...item, task });
      }
    }
    rtspSessions.value = sessions;
    if (sessions.length) {
      selectedDeviceIds.value = sessions.map((s) => s.deviceId);
    }
    persistRtspSessions();
  } catch {
    localStorage.removeItem(STORAGE.rtspSessions);
  }
}

async function loadMonitorDevices() {
  try {
    const { data } = await http.get<{ devices: MonitorDevice[] }>("/devices", {
      params: { enabled_only: true },
    });
    monitorDevices.value = data.devices || [];
    const validIds = new Set(monitorDevices.value.map((d) => d.id));
    selectedDeviceIds.value = selectedDeviceIds.value.filter((id) => validIds.has(id));
    if (selectedDeviceIds.value.length === 0 && monitorDevices.value.length > 0) {
      selectedDeviceIds.value = [monitorDevices.value[0].id];
    }
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "加载设备列表失败");
  }
}

async function initAuth() {
  authChecking.value = true;
  currentUser.value = await fetchCurrentUser();
  authChecking.value = false;
}

async function onLoginSuccess() {
  currentUser.value = await fetchCurrentUser();
  if (currentUser.value) {
    await syncServerBoot();
    await restoreSession();
    startPollTimer();
  }
}

async function doLogout() {
  try {
    await logout();
  } catch {
    /* ignore */
  }
  currentUser.value = null;
  clearTaskSession();
}

function onUnauthorized() {
  currentUser.value = null;
}

async function restoreSession() {
  const savedTab = localStorage.getItem(STORAGE.tab);
  if (savedTab === "local" || savedTab === "isapi" || savedTab === "devices" || savedTab === "rtsp" || savedTab === "records") {
    activeTab.value = savedTab;
  }

  localTaskId.value = localStorage.getItem(STORAGE.local) || "";
  isapiTaskId.value = localStorage.getItem(STORAGE.isapi) || "";

  await Promise.all([restoreLatestTask("local", localTaskId), restoreLatestTask("isapi", isapiTaskId)]);
  await restoreRtspSessions();
  await loadMonitorDevices();
  await refreshTabTasks();
}

async function refreshTabTasks() {
  if (localTaskId.value) {
    const t = await fetchTask(localTaskId.value);
    if (t) localTask.value = t;
    else clearTabTask("local");
  }
  if (isapiTaskId.value) {
    const t = await fetchTask(isapiTaskId.value);
    if (t) isapiTask.value = t;
    else clearTabTask("isapi");
  }
  if (activeTab.value === "rtsp" && rtspSessions.value.length) {
    await syncRtspFromServer();
  }
}

function startPollTimer() {
  if (pollTimer) return;
  pollTimer = setInterval(async () => {
    await syncServerBoot();
    await refreshTabTasks();
  }, 2000);
}

async function stopAnalysisTask(which: "local" | "isapi") {
  const taskId = which === "local" ? localTaskId.value : isapiTaskId.value;
  if (!taskId) return;
  const loadingRef = which === "local" ? stoppingLocal : stoppingIsapi;
  loadingRef.value = true;
  try {
    const fd = new FormData();
    fd.append("task_id", taskId);
    await http.post("/tasks/stop", fd);
    const t = await fetchTask(taskId);
    if (which === "local") localTask.value = t;
    else isapiTask.value = t;
    ElMessage.success("分析已停止");
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "停止失败");
  } finally {
    loadingRef.value = false;
  }
}

async function doLocalUpload(opt: UploadRequestOptions) {
  const fd = new FormData();
  fd.append("file", opt.file as File);
  fd.append("frame_stride", String(frameStride.value));
  fd.append("enable_reid", String(enableReid.value));
  fd.append("reid_threshold", String(reidThreshold.value));
  uploading.value = true;
  uploadPercent.value = 0;
  try {
    const { data } = await http.post<{ task_id: string }>("/tasks/video", fd, {
      timeout: 600000,
      onUploadProgress: (evt) => {
        if (!evt.total) return;
        uploadPercent.value = Math.min(100, Math.round((evt.loaded * 100) / evt.total));
      },
    });
    localTaskId.value = data.task_id;
    localTask.value = await fetchTask(data.task_id);
    ElMessage.success(`上传成功，开始分析：${data.task_id}`);
    opt.onSuccess?.({});
  } catch (err: any) {
    opt.onError?.(err as Error);
    ElMessage.error(err?.response?.data?.detail || "上传失败");
  } finally {
    uploading.value = false;
  }
}

async function detectChannels() {
  probingChannels.value = true;
  try {
    const fd = new FormData();
    fd.append("host", nvr.host.trim());
    fd.append("username", nvr.username.trim());
    fd.append("password", nvr.password);
    const { data } = await http.post<{ channels: IsapiChannel[] }>("/isapi/channels", fd);
    channelOptions.value = data.channels || [];
    nvr.channel = channelOptions.value.length > 0 ? channelOptions.value[0].id : null;
    ElMessage.success(`已识别 ${channelOptions.value.length} 个摄像头`);
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "获取摄像头列表失败");
  } finally {
    probingChannels.value = false;
  }
}

async function submitIsapi() {
  const start = nvr.startAt.replace(" ", "T") + ":00";
  const end = nvr.endAt.replace(" ", "T") + ":00";
  const fd = new FormData();
  fd.append("host", nvr.host.trim());
  fd.append("username", nvr.username.trim());
  fd.append("password", nvr.password);
  fd.append("isapi_start", start);
  fd.append("isapi_end", end);
  fd.append("frame_stride", String(frameStride.value));
  fd.append("enable_reid", String(enableReid.value));
  fd.append("reid_threshold", String(reidThreshold.value));
  if (nvr.channel == null) return;
  fd.append("channels", String(nvr.channel));
  try {
    const { data } = await http.post<{
      task_ids: string[];
      stopped_task_ids?: string[];
      count: number;
    }>("/tasks/isapi", fd, {
      timeout: 600000,
    });
    if (data.task_ids?.length) {
      isapiTaskId.value = data.task_ids[0];
      isapiTask.value = await fetchTask(data.task_ids[0]);
    }
    if (data.stopped_task_ids?.length) {
      ElMessage.success(`已开始分析，已停止 ${data.stopped_task_ids.length} 个未完成任务`);
    } else {
      ElMessage.success("已开始分析");
    }
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "提交失败");
  }
}

async function startRtspFromDevices() {
  const toStart = pendingDeviceIds.value;
  if (!canStartRtsp.value || toStart.length === 0) return;
  rtspStarting.value = true;
  const fd = new FormData();
  for (const id of toStart) fd.append("device_ids", String(id));
  fd.append("frame_stride", String(frameStride.value));
  fd.append("enable_reid", String(enableReid.value));
  fd.append("reid_threshold", String(reidThreshold.value));
  try {
    const { data } = await http.post<{ task_ids: string[]; device_ids: number[]; count: number }>(
      "/stream/live/start-by-device",
      fd,
    );
    const added: RtspSession[] = [];
    for (let i = 0; i < data.task_ids.length; i++) {
      const deviceId = data.device_ids[i];
      const device = monitorDevices.value.find((d) => d.id === deviceId);
      added.push({
        deviceId,
        deviceName: device?.name || `设备${deviceId}`,
        taskId: data.task_ids[i],
        task: await fetchTask(data.task_ids[i]),
      });
    }
    const prevCount = rtspSessions.value.length;
    rtspSessions.value = [...rtspSessions.value, ...added];
    persistRtspSessions();
    ElMessage.success(prevCount > 0 ? `已添加 ${data.count} 路监控` : `已启动 ${data.count} 路监控`);
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "启动失败");
  } finally {
    rtspStarting.value = false;
  }
}

async function stopRtspOne(session: RtspSession) {
  rtspStoppingDeviceId.value = session.deviceId;
  const fd = new FormData();
  fd.append("task_id", session.taskId);
  try {
    await http.post("/stream/live/stop", fd);
    rtspSessions.value = rtspSessions.value.filter((s) => s.taskId !== session.taskId);
    selectedDeviceIds.value = selectedDeviceIds.value.filter((id) => id !== session.deviceId);
    persistRtspSessions();
    ElMessage.info(`已停止：${session.deviceName}`);
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "停止失败");
  } finally {
    rtspStoppingDeviceId.value = null;
  }
}

async function stopRtspMonitor() {
  if (!rtspSessions.value.length) return;
  rtspStopping.value = true;
  try {
    for (const s of [...rtspSessions.value]) {
      const fd = new FormData();
      fd.append("task_id", s.taskId);
      await http.post("/stream/live/stop", fd);
    }
    rtspSessions.value = [];
    persistRtspSessions();
    ElMessage.info("已全部停止");
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "停止失败");
  } finally {
    rtspStopping.value = false;
  }
}

function initDefaultTimes() {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  nvr.endAt = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${pad(now.getHours())}:00`;
  const prev = new Date(now.getTime() - 3600_000);
  nvr.startAt = `${prev.getFullYear()}-${pad(prev.getMonth() + 1)}-${pad(prev.getDate())} ${pad(prev.getHours())}:00`;
}

watch(activeTab, (tab) => {
  localStorage.setItem(STORAGE.tab, tab);
  if (tab === "rtsp") {
    loadMonitorDevices();
    syncRtspFromServer();
  }
  refreshTabTasks();
});
watch(localTaskId, (id) => persistTaskId(STORAGE.local, id));
watch(isapiTaskId, (id) => persistTaskId(STORAGE.isapi, id));
watch(rtspSessions, persistRtspSessions, { deep: true });

onMounted(async () => {
  initDefaultTimes();
  window.addEventListener("sc-unauthorized", onUnauthorized);
  await initAuth();
  if (currentUser.value) {
    await syncServerBoot();
    await restoreSession();
    startPollTimer();
  }
});

onUnmounted(() => {
  window.removeEventListener("sc-unauthorized", onUnauthorized);
  if (pollTimer) clearInterval(pollTimer);
});
</script>

<template>
  <div v-if="authChecking" class="auth-loading">加载中...</div>
  <LoginPage v-else-if="!currentUser" @success="onLoginSuccess" />
  <div v-else class="layout">
    <aside class="sidebar">
      <div class="brand">智慧工地安全监管系统</div>
      <button
        v-for="item in menuItems"
        :key="item.key"
        class="nav-item"
        :class="{ active: activeTab === item.key }"
        @click="activeTab = item.key"
      >
        <el-icon><component :is="item.icon" /></el-icon>
        <span>{{ item.label }}</span>
      </button>
      <div class="sidebar-foot">
        <div class="user-line">{{ currentUser.username }}</div>
        <button class="nav-item logout-btn" @click="doLogout">退出登录</button>
      </div>
    </aside>

    <main class="main">
      <div v-show="activeTab === 'local'" class="tab-panel">
        <section class="panel">
          <h2>本地上传</h2>
          <div class="params">
            <span>分析间隔（每 {{ frameStride }} 帧）</span>
            <el-slider v-model="frameStride" :min="1" :max="120" show-input />
            <el-switch v-model="enableReid" active-text="同人合并" />
            <span v-if="enableReid" class="reid-threshold-label">合并阈值 {{ reidThreshold }}</span>
            <el-slider v-if="enableReid" v-model="reidThreshold" :min="0.5" :max="0.99" :step="0.01" />
          </div>
          <el-upload drag :http-request="doLocalUpload" :show-file-list="false" accept=".mp4,.avi,.mov,.mkv,.wmv">
            <el-icon class="upload-icon"><UploadFilled /></el-icon>
            <div>拖拽或点击上传</div>
          </el-upload>
          <el-progress v-if="uploading || uploadPercent > 0" :percentage="uploadPercent" class="mt" />
          <div class="action-row mt">
            <el-button
              v-if="canStopLocal"
              type="danger"
              plain
              :loading="stoppingLocal"
              @click="stopAnalysisTask('local')"
            >
              停止分析
            </el-button>
          </div>
        </section>
        <section class="panel">
          <ResultPanel title="分析结果" :task="localTask" :task-id="localTaskId" live />
        </section>
      </div>

      <div v-show="activeTab === 'isapi'" class="tab-panel">
        <section class="panel">
          <h2>录像回放</h2>
          <p class="hint">从录像机下载指定时间段的视频并分析（同一时间仅分析一路）</p>
          <el-alert v-if="!ffmpegOk" type="warning" show-icon :closable="false" title="当前环境无法下载录像，请检查服务器是否已安装 ffmpeg" class="mb" />
          <el-form label-position="top" class="panel-form">
            <el-row :gutter="8">
              <el-col :span="8"><el-form-item label="设备 IP"><el-input v-model="nvr.host" placeholder="192.168.1.64" /></el-form-item></el-col>
              <el-col :span="8"><el-form-item label="用户名"><el-input v-model="nvr.username" /></el-form-item></el-col>
              <el-col :span="8"><el-form-item label="密码"><el-input v-model="nvr.password" type="password" show-password /></el-form-item></el-col>
            </el-row>
            <el-form-item label="摄像头">
              <el-button :loading="probingChannels" @click="detectChannels">获取列表</el-button>
              <el-select v-model="nvr.channel" placeholder="请先获取摄像头列表" style="width:100%;margin-top:8px">
                <el-option v-for="c in channelOptions" :key="c.id" :label="`${c.name}（${c.id}）`" :value="c.id" />
              </el-select>
            </el-form-item>
            <el-row :gutter="8">
              <el-col :span="12"><el-form-item label="开始时间"><el-date-picker v-model="nvr.startAt" type="datetime" value-format="YYYY-MM-DD HH:mm" style="width:100%" /></el-form-item></el-col>
              <el-col :span="12"><el-form-item label="结束时间"><el-date-picker v-model="nvr.endAt" type="datetime" value-format="YYYY-MM-DD HH:mm" style="width:100%" /></el-form-item></el-col>
            </el-row>
            <el-form-item label="分析间隔（帧）">
              <el-slider v-model="frameStride" :min="1" :max="120" show-input />
            </el-form-item>
            <el-form-item label="同人合并">
              <el-switch v-model="enableReid" />
              <el-slider v-if="enableReid" v-model="reidThreshold" :min="0.5" :max="0.99" :step="0.01" show-input style="margin-top:8px" />
            </el-form-item>
            <div class="action-row">
              <el-button type="primary" :disabled="!canSubmitIsapi" @click="submitIsapi">开始分析</el-button>
              <el-button
                v-if="canStopIsapi"
                type="danger"
                plain
                :loading="stoppingIsapi"
                @click="stopAnalysisTask('isapi')"
              >
                停止分析
              </el-button>
            </div>
          </el-form>
        </section>
        <section class="panel">
          <ResultPanel title="分析结果" :task="isapiTask" :task-id="isapiTaskId" live />
        </section>
      </div>

      <div v-show="activeTab === 'devices'" class="tab-panel devices-tab">
        <DevicePanel />
      </div>

      <div v-show="activeTab === 'rtsp'" class="tab-panel rtsp-tab">
        <MonitorLive
          v-model:selected-device-ids="selectedDeviceIds"
          v-model:frame-stride="frameStride"
          v-model:enable-reid="enableReid"
          v-model:reid-threshold="reidThreshold"
          :active="activeTab === 'rtsp'"
          :devices="monitorDevices"
          :sessions="rtspSessions"
          :starting="rtspStarting"
          :stopping="rtspStopping"
          :stopping-device-id="rtspStoppingDeviceId"
          :can-start="canStartRtsp"
          @start="startRtspFromDevices"
          @stop="stopRtspMonitor"
          @stop-one="stopRtspOne"
          @refresh-devices="loadMonitorDevices"
        />
      </div>

      <div v-show="activeTab === 'records'" class="tab-panel records-tab">
        <SafetyRecordsPanel :active="activeTab === 'records'" />
      </div>
    </main>
  </div>
</template>

<style scoped>
.auth-loading {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #64748b;
  background: #eef1f6;
}
.layout { display: flex; min-height: 100vh; background: #eef1f6; }
.sidebar { width: 200px; background: #1e293b; color: #e2e8f0; padding: 20px 12px; flex-shrink: 0; display: flex; flex-direction: column; }
.brand { font-weight: 700; margin-bottom: 24px; padding: 0 8px; }
.nav-item { display: flex; align-items: center; gap: 10px; width: 100%; border: none; background: transparent; color: #94a3b8; padding: 12px; border-radius: 8px; cursor: pointer; font-size: 14px; margin-bottom: 4px; }
.nav-item:hover, .nav-item.active { background: #334155; color: #fff; }
.sidebar-foot { margin-top: auto; padding-top: 16px; border-top: 1px solid #334155; }
.user-line { font-size: 12px; color: #94a3b8; padding: 0 12px 8px; }
.logout-btn { color: #fca5a5 !important; }
.logout-btn:hover { background: #450a0a !important; color: #fecaca !important; }
.main { flex: 1; padding: 20px; overflow: auto; }
.tab-panel { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; align-items: start; }
.devices-tab { grid-template-columns: 1fr; }
.rtsp-tab { grid-template-columns: 1fr; display: block; }
.records-tab { grid-template-columns: 1fr; display: block; }
.panel { background: #fff; border-radius: 12px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
.panel h2 { margin: 0 0 12px; font-size: 18px; }
.hint { color: #64748b; font-size: 13px; margin: 0 0 12px; }
.params { margin-bottom: 12px; }
.reid-threshold-label { display: block; font-size: 13px; color: #64748b; margin: 8px 0 4px; }
.upload-icon { font-size: 40px; color: #409eff; }
.mb { margin-bottom: 12px; }
.mt { margin-top: 12px; }
.action-row { display: flex; gap: 10px; flex-wrap: wrap; margin-top: 12px; }
.rtsp-result { grid-column: span 1; }
.panel-form :deep(.el-form-item) { margin-bottom: 14px; }
.panel-form :deep(.el-form-item__label) { padding: 0 0 6px; line-height: 1.4; font-weight: 500; color: #334155; }
.panel-form :deep(.el-slider) { width: 100%; max-width: 100%; }
.panel-form :deep(.el-slider__runway) { margin-right: 0; }
@media (max-width: 1100px) {
  .tab-panel, .rtsp-tab { grid-template-columns: 1fr; }
  .rtsp-result { grid-column: span 1; }
}
</style>

<style>
body { margin: 0; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif; }
</style>
