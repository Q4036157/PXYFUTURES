<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { client, saveLocalToken } from '@/api/client'

const router = useRouter()
const password = ref('')
const configured = ref(true)
const submitting = ref(false)

onMounted(async () => {
  const { data } = await client.get<{ mode: string; configured: boolean }>('/auth/status')
  if (data.mode === 'jwt') {
    await router.replace('/')
    return
  }
  configured.value = data.configured
})

async function submit(): Promise<void> {
  submitting.value = true
  try {
    if (configured.value) {
      const { data } = await client.post<{ access_token: string }>('/auth/login', { password: password.value })
      saveLocalToken(data.access_token)
    } else {
      await client.post('/auth/setup', { password: password.value })
      configured.value = true
      ElMessage.success('密码已设置，请登录')
      return
    }
    await router.replace('/')
  } catch (error: any) {
    ElMessage.error(error.response?.data?.detail || '操作失败')
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-panel">
      <div class="login-mark">期</div>
      <h1>智能期货</h1>
      <p>{{ configured ? '输入访问密码继续' : '首次使用，请设置访问密码' }}</p>
      <el-form @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="password" type="password" show-password placeholder="至少 6 位" @keyup.enter="submit" />
        </el-form-item>
        <el-button type="primary" class="login-submit" :loading="submitting" @click="submit">
          {{ configured ? '登录' : '设置密码' }}
        </el-button>
      </el-form>
    </section>
  </main>
</template>
