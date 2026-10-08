<script setup lang="ts">
import { Lock, User } from "@element-plus/icons-vue";
import { ElMessage } from "element-plus";
import { reactive, ref } from "vue";
import { login } from "../api";

const emit = defineEmits<{ success: [] }>();

const form = reactive({
  username: "admin",
  password: "",
});
const loading = ref(false);

async function onSubmit() {
  if (!form.username.trim()) {
    ElMessage.warning("请输入用户名");
    return;
  }
  if (!form.password) {
    ElMessage.warning("请输入密码");
    return;
  }
  loading.value = true;
  try {
    await login(form.username.trim(), form.password);
    ElMessage.success("登录成功");
    emit("success");
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.detail || "登录失败");
  } finally {
    loading.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card">
      <div class="login-brand">
        <div class="logo">⛑</div>
        <h1>智慧工地安全监管系统</h1>
      </div>

      <el-form label-position="top" @submit.prevent="onSubmit">
        <el-form-item label="用户名">
          <el-input
            v-model="form.username"
            placeholder="admin"
            autocomplete="username"
            size="large"
            :prefix-icon="User"
          />
        </el-form-item>
        <el-form-item label="密码">
          <el-input
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            autocomplete="current-password"
            show-password
            size="large"
            :prefix-icon="Lock"
            @keyup.enter="onSubmit"
          />
        </el-form-item>
        <el-button type="primary" size="large" class="submit-btn" :loading="loading" @click="onSubmit">
          登录
        </el-button>
        <p class="support-text">技术支持：浙江炎科信息技术有限公司</p>
      </el-form>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 45%, #334155 100%);
  padding: 24px;
}

.login-card {
  width: 100%;
  max-width: 400px;
  background: #fff;
  border-radius: 16px;
  padding: 36px 32px 32px;
  box-shadow: 0 20px 50px rgba(15, 23, 42, 0.35);
}

.login-brand {
  text-align: center;
  margin-bottom: 28px;
}

.logo {
  font-size: 42px;
  line-height: 1;
  margin-bottom: 8px;
}

.login-brand h1 {
  margin: 0;
  font-size: 22px;
  color: #0f172a;
}

.submit-btn {
  width: 100%;
  margin-top: 8px;
}

.support-text {
  margin: 20px 0 0;
  text-align: center;
  font-size: 13px;
  color: #94a3b8;
  line-height: 1.5;
}
</style>
