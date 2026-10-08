<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus";
import { onMounted, reactive, ref } from "vue";
import http, { type MonitorDevice } from "../api";

const loading = ref(false);
const probingId = ref<number | null>(null);
const devices = ref<MonitorDevice[]>([]);
const dialogVisible = ref(false);
const saving = ref(false);
const editingId = ref<number | null>(null);

const filters = reactive({
  name: "",
  location: "",
  status: "" as "" | "0" | "1" | "2",
});

const form = reactive({
  name: "",
  location: "",
  rtsp_url: "",
  enabled: true,
  remark: "",
});

function statusType(status: number) {
  if (status === 1) return "success";
  if (status === 2) return "danger";
  return "info";
}

function resetForm() {
  form.name = "";
  form.location = "";
  form.rtsp_url = "";
  form.enabled = true;
  form.remark = "";
  editingId.value = null;
}

async function loadDevices() {
  loading.value = true;
  try {
    const params: Record<string, string | number> = {};
    if (filters.name.trim()) params.name = filters.name.trim();
    if (filters.location.trim()) params.location = filters.location.trim();
    if (filters.status !== "") params.status = Number(filters.status);
    const { data } = await http.get<{ devices: MonitorDevice[] }>("/devices", { params });
    devices.value = data.devices || [];
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "加载设备列表失败");
  } finally {
    loading.value = false;
  }
}

function openCreate() {
  resetForm();
  dialogVisible.value = true;
}

function openEdit(row: MonitorDevice) {
  editingId.value = row.id;
  form.name = row.name;
  form.location = row.location || "";
  form.rtsp_url = row.rtsp_url;
  form.enabled = row.enabled;
  form.remark = row.remark || "";
  dialogVisible.value = true;
}

async function saveDevice() {
  if (!form.name.trim()) {
    ElMessage.warning("请填写设备名称");
    return;
  }
  if (!form.rtsp_url.trim().startsWith("rtsp://")) {
    ElMessage.warning("RTSP 地址必须以 rtsp:// 开头");
    return;
  }
  saving.value = true;
  try {
    const payload = {
      name: form.name.trim(),
      location: form.location.trim(),
      rtsp_url: form.rtsp_url.trim(),
      enabled: form.enabled,
      remark: form.remark.trim(),
    };
    if (editingId.value) {
      await http.put(`/devices/${editingId.value}`, payload);
      ElMessage.success("设备已更新");
    } else {
      await http.post("/devices", payload);
      ElMessage.success("设备已添加");
    }
    dialogVisible.value = false;
    await loadDevices();
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "保存失败");
  } finally {
    saving.value = false;
  }
}

async function removeDevice(row: MonitorDevice) {
  try {
    await ElMessageBox.confirm(`确定删除设备「${row.name}」？`, "删除确认", {
      type: "warning",
    });
    await http.delete(`/devices/${row.id}`);
    ElMessage.success("已删除");
    await loadDevices();
  } catch (err: any) {
    if (err === "cancel" || err?.message === "cancel") return;
    ElMessage.error(err?.response?.data?.detail || "删除失败");
  }
}

async function probeOne(row: MonitorDevice) {
  probingId.value = row.id;
  try {
    const { data } = await http.post<MonitorDevice>(`/devices/${row.id}/probe`);
    const idx = devices.value.findIndex((d) => d.id === row.id);
    if (idx >= 0) devices.value[idx] = data;
    ElMessage.success(`${row.name}：${data.status_label}`);
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "检测失败");
  } finally {
    probingId.value = null;
  }
}

function resetFilters() {
  filters.name = "";
  filters.location = "";
  filters.status = "";
  loadDevices();
}

onMounted(loadDevices);

defineExpose({ loadDevices });
</script>

<template>
  <section class="panel device-panel">
    <div class="panel-head">
      <div>
        <h2>我的设备</h2>
        <p class="hint">预先配置 RTSP 摄像头，实时监控可直接选用</p>
      </div>
      <el-button type="primary" @click="openCreate">新增设备</el-button>
    </div>

    <el-form inline class="filter-form">
      <el-form-item label="设备名称">
        <el-input v-model="filters.name" clearable @keyup.enter="loadDevices" />
      </el-form-item>
      <el-form-item label="安装位置">
        <el-input v-model="filters.location" clearable @keyup.enter="loadDevices" />
      </el-form-item>
      <el-form-item label="状态">
        <el-select v-model="filters.status" placeholder="全部" clearable style="width: 120px">
          <el-option label="未知" value="0" />
          <el-option label="在线" value="1" />
          <el-option label="离线" value="2" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="loadDevices">查询</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </el-form-item>
    </el-form>

    <el-table v-loading="loading" :data="devices" stripe style="width: 100%">
      <el-table-column type="index" label="序号" width="60" />
      <el-table-column prop="name" label="设备名称" min-width="140" />
      <el-table-column prop="location" label="安装位置" min-width="140" show-overflow-tooltip />
      <el-table-column prop="rtsp_url" label="RTSP 地址" min-width="220" show-overflow-tooltip />
      <el-table-column prop="created_by" label="创建者" width="90" />
      <el-table-column label="状态" width="80">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ row.status_label }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用" width="70">
        <template #default="{ row }">
          <el-tag size="small" :type="row.enabled ? 'success' : 'info'">
            {{ row.enabled ? "是" : "否" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" width="170" />
      <el-table-column label="操作" width="200" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" :loading="probingId === row.id" @click="probeOne(row)">
            检测
          </el-button>
          <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" @click="removeDevice(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-empty v-if="!loading && devices.length === 0" description="暂无设备，请点击「新增设备」" />

    <el-dialog
      v-model="dialogVisible"
      :title="editingId ? '编辑设备' : '新增设备'"
      width="520px"
      destroy-on-close
      @closed="resetForm"
    >
      <el-form label-position="top">
        <el-form-item label="设备名称" required>
          <el-input v-model="form.name" placeholder="例如：正门监控摄像头" />
        </el-form-item>
        <el-form-item label="安装位置">
          <el-input v-model="form.location" placeholder="例如：1号门入口" />
        </el-form-item>
        <el-form-item label="RTSP 地址" required>
          <el-input v-model="form.rtsp_url" placeholder="rtsp://user:pass@192.168.1.64:554/..." />
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="启用">
          <el-switch v-model="form.enabled" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveDevice">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.device-panel { grid-column: 1 / -1; }
.panel-head { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; }
.panel-head h2 { margin: 0 0 4px; font-size: 18px; }
.hint { color: #64748b; font-size: 13px; margin: 0; }
.filter-form { margin-bottom: 12px; }
</style>
